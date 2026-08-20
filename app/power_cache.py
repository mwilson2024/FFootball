from __future__ import annotations

import copy
import logging
from collections import defaultdict
from datetime import UTC, datetime
from threading import Lock
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.draft import DraftValidationError, draft_state
from app.draft_analysis import build_draft_analysis
from app.models import AuctionPurchase, Franchise, League, PowerRankingSnapshot
from app.power_rankings import build_power_rankings
from app.realtime import league_events

LOGGER = logging.getLogger("uvicorn.error")
_refresh_lock = Lock()


def _completed_draft_round(db: Session, league_id: str) -> int:
    try:
        order = draft_state(db, league_id, include_intelligence=False).get("draft_order") or []
    except DraftValidationError:
        return 0
    by_round: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for slot in order:
        try:
            round_number = int(slot.get("round") or 0)
        except (TypeError, ValueError):
            continue
        if round_number > 0:
            by_round[round_number].append(slot)
    completed = 0
    for round_number in sorted(by_round):
        slots = by_round[round_number]
        if (
            round_number != completed + 1
            or not slots
            or not all(slot.get("completed") for slot in slots)
        ):
            break
        completed = round_number
    return completed


def _completed_auction_round(db: Session, league_id: str) -> int:
    team_count = int(
        db.scalar(select(func.count(Franchise.pk)).where(Franchise.league_id == league_id)) or 0
    )
    if team_count <= 0:
        return 0
    purchase_count = int(
        db.scalar(
            select(func.count(AuctionPurchase.id)).where(
                AuctionPurchase.league_id == league_id,
                AuctionPurchase.active.is_(True),
            )
        )
        or 0
    )
    return purchase_count // team_count


def refresh_power_snapshot(
    db: Session, league_id: str, *, trigger: str = "manual"
) -> PowerRankingSnapshot:
    """Compute one shared snapshot containing league and per-team analysis."""
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise ValueError("League not found")
    power = build_power_rankings(db, league_id)
    overview = build_draft_analysis(db, league_id, None)
    teams: dict[str, dict[str, Any]] = {}
    for standing in overview.get("projected_standings") or []:
        franchise_id = str(standing["franchise_id"])
        detail = build_draft_analysis(db, league_id, franchise_id)
        teams[franchise_id] = {
            "selected_team": detail.get("selected_team"),
            "eligible_picks": (detail.get("what_if") or {}).get("eligible_picks") or [],
        }
    snapshot = db.get(PowerRankingSnapshot, league_id)
    if snapshot is None:
        snapshot = PowerRankingSnapshot(league_id=league_id)
        db.add(snapshot)
    snapshot.payload_json = {"power": power, "analysis": overview, "teams": teams}
    snapshot.draft_round = _completed_draft_round(db, league_id)
    snapshot.auction_round = _completed_auction_round(db, league_id)
    snapshot.trigger = trigger[:80]
    snapshot.computed_at = datetime.now(UTC)
    db.commit()
    league_events.publish(
        league_id,
        "power-rankings-cache",
        {
            "draft_round": snapshot.draft_round,
            "auction_round": snapshot.auction_round,
            "computed_at": snapshot.computed_at.isoformat(),
        },
    )
    return snapshot


def refresh_power_snapshot_job(league_id: str, trigger: str) -> None:
    """Run a refresh in an independent session after a request has committed."""
    with _refresh_lock, SessionLocal() as db:
        try:
            refresh_power_snapshot(db, league_id, trigger=trigger)
        except Exception:
            LOGGER.exception("Power Rankings cache refresh failed for league %s", league_id)


def refresh_all_power_snapshots_job(trigger: str = "startup") -> None:
    with SessionLocal() as db:
        league_ids = list(db.scalars(select(League.id).order_by(League.id)))
    for league_id in league_ids:
        refresh_power_snapshot_job(str(league_id), trigger)


def round_refresh_due(db: Session, league_id: str, mode: Literal["draft", "auction"]) -> bool:
    snapshot = db.get(PowerRankingSnapshot, league_id)
    if snapshot is None:
        return False
    if mode == "draft":
        return _completed_draft_round(db, league_id) > snapshot.draft_round
    return _completed_auction_round(db, league_id) > snapshot.auction_round


def power_snapshot_exists(db: Session, league_id: str) -> bool:
    return db.get(PowerRankingSnapshot, league_id) is not None


def _cache_meta(snapshot: PowerRankingSnapshot) -> dict[str, Any]:
    return {
        "stored": True,
        "computed_at": snapshot.computed_at.isoformat(),
        "draft_round": snapshot.draft_round,
        "auction_round": snapshot.auction_round,
        "trigger": snapshot.trigger,
    }


def cached_power_rankings(db: Session, league_id: str) -> dict[str, Any]:
    snapshot = db.get(PowerRankingSnapshot, league_id)
    if snapshot is None:
        snapshot = refresh_power_snapshot(db, league_id, trigger="first-read")
    result = copy.deepcopy(snapshot.payload_json.get("power") or {})
    result["cache"] = _cache_meta(snapshot)
    return result


def cached_draft_analysis(
    db: Session,
    league_id: str,
    franchise_id: str | None,
    *,
    what_if_overall_pick: int | None = None,
    alternative_player_id: str | None = None,
) -> dict[str, Any]:
    if what_if_overall_pick and alternative_player_id and franchise_id:
        result = build_draft_analysis(
            db,
            league_id,
            franchise_id,
            what_if_overall_pick=what_if_overall_pick,
            alternative_player_id=alternative_player_id,
        )
        snapshot = db.get(PowerRankingSnapshot, league_id)
        if snapshot is not None:
            result["cache"] = _cache_meta(snapshot)
        return result
    snapshot = db.get(PowerRankingSnapshot, league_id)
    if snapshot is None:
        snapshot = refresh_power_snapshot(db, league_id, trigger="first-read")
    result = copy.deepcopy(snapshot.payload_json.get("analysis") or {})
    if franchise_id:
        team = (snapshot.payload_json.get("teams") or {}).get(franchise_id) or {}
        result["selected_team"] = copy.deepcopy(team.get("selected_team"))
        result.setdefault("what_if", {})["eligible_picks"] = copy.deepcopy(
            team.get("eligible_picks") or []
        )
    else:
        result["selected_team"] = None
        result.setdefault("what_if", {})["eligible_picks"] = []
    result["cache"] = _cache_meta(snapshot)
    return result
