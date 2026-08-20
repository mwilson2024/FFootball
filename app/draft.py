import csv
import io
import math
import os
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog import draftable_consensus
from app.consensus import availability_maps
from app.models import (
    AuctionPurchase,
    DraftAuditEvent,
    DraftPick,
    DraftSession,
    Franchise,
    League,
    MFLSnapshot,
    MockDraftPick,
    MockDraftSession,
    Player,
    RosterAssignment,
)
from app.projections import build_projection_board, lineup_projection
from app.schemas import DraftPickCreate, DraftPickUpdate


class DraftValidationError(ValueError):
    pass


def franchise_position_needs(db: Session, league_id: str, franchise_id: str) -> dict[str, int]:
    """Return open primary starter slots using synced and locally recorded players."""
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    owned_player_ids = set(
        db.scalars(
            select(RosterAssignment.player_id).where(
                RosterAssignment.league_id == league_id,
                RosterAssignment.franchise_id == franchise_id,
            )
        )
    )
    owned_player_ids.update(
        db.scalars(
            select(DraftPick.player_id).where(
                DraftPick.league_id == league_id,
                DraftPick.franchise_id == franchise_id,
            )
        )
    )
    owned_player_ids.update(
        db.scalars(
            select(AuctionPurchase.player_id).where(
                AuctionPurchase.league_id == league_id,
                AuctionPurchase.franchise_id == franchise_id,
                AuctionPurchase.active.is_(True),
            )
        )
    )
    position_counts: dict[str, int] = {}
    if owned_player_ids:
        for position in db.scalars(select(Player.position).where(Player.id.in_(owned_player_ids))):
            normalized = str(position or "").upper()
            position_counts[normalized] = position_counts.get(normalized, 0) + 1
    return {
        str(position).upper(): max(
            0, int(required or 0) - position_counts.get(str(position).upper(), 0)
        )
        for position, required in league.lineup_json.items()
        if str(position).upper() not in {"FLEX", "SUPERFLEX"}
    }


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _number(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _draft_order(
    db: Session, league_id: str, local_picks: list[DraftPick]
) -> tuple[list[dict[str, Any]], str | None]:
    snapshot = db.scalar(
        select(MFLSnapshot)
        .where(
            MFLSnapshot.league_id == league_id,
            MFLSnapshot.export_type == "draftResults",
        )
        .order_by(MFLSnapshot.fetched_at.desc())
    )
    if snapshot is None or not isinstance(snapshot.payload_json, dict):
        return _local_recorded_order(db, league_id, local_picks), None
    root = snapshot.payload_json.get("draftResults", snapshot.payload_json)
    if not isinstance(root, dict):
        return _local_recorded_order(db, league_id, local_picks), snapshot.fetched_at.isoformat()
    units = [item for item in _as_list(root.get("draftUnit")) if isinstance(item, dict)]
    unit = next((item for item in units if item.get("draftPick") is not None), None)
    if unit is None:
        return _local_recorded_order(db, league_id, local_picks), snapshot.fetched_at.isoformat()

    raw_picks = [item for item in _as_list(unit.get("draftPick")) if isinstance(item, dict)]
    if not raw_picks:
        return _local_recorded_order(db, league_id, local_picks), snapshot.fetched_at.isoformat()
    indexed_picks = list(enumerate(raw_picks, start=1))
    has_explicit_overall = all(_number(item.get("overallPick")) for item in raw_picks)
    if has_explicit_overall:
        indexed_picks.sort(key=lambda item: _number(item[1].get("overallPick")) or item[0])
        order_source = "MFL draft order"
    else:
        order_source = "MFL listed order"

    local_by_overall = {
        item.overall_pick: item for item in local_picks if item.overall_pick is not None
    }
    franchise_names = {
        item.id: item.name
        for item in db.scalars(select(Franchise).where(Franchise.league_id == league_id))
    }
    slots: list[dict[str, Any]] = []
    for index, (_, raw) in enumerate(indexed_picks, start=1):
        overall = _number(raw.get("overallPick")) or index
        local = local_by_overall.get(overall)
        franchise_id = str(raw.get("franchise") or (local.franchise_id if local else ""))
        remote_player = raw.get("player")
        remote_player_id = str(remote_player) if remote_player not in (None, "") else None
        player_id = local.player_id if local else remote_player_id
        player = db.get(Player, player_id) if player_id else None
        slots.append(
            {
                "overall_pick": overall,
                "round": _number(raw.get("round")) or (local.round if local else None),
                "pick": _number(raw.get("pick")) or (local.pick if local else None),
                "franchise_id": franchise_id or None,
                "franchise_name": franchise_names.get(franchise_id, franchise_id or "Unknown"),
                "player_id": player_id,
                "player_name": player.name if player else None,
                "position": player.position if player else None,
                "nfl_team": player.nfl_team if player else None,
                "completed": bool(player_id),
                "order_source": order_source,
            }
        )
    return slots, snapshot.fetched_at.isoformat()


def _local_recorded_order(
    db: Session, league_id: str, local_picks: list[DraftPick]
) -> list[dict[str, Any]]:
    franchise_names = {
        item.id: item.name
        for item in db.scalars(select(Franchise).where(Franchise.league_id == league_id))
    }
    ordered = sorted(
        local_picks,
        key=lambda item: (item.overall_pick is None, item.overall_pick or 0, item.selected_at),
    )
    slots: list[dict[str, Any]] = []
    for index, local in enumerate(ordered, start=1):
        player = db.get(Player, local.player_id)
        franchise_id = local.franchise_id or ""
        slots.append(
            {
                "overall_pick": local.overall_pick or index,
                "round": local.round,
                "pick": local.pick,
                "franchise_id": franchise_id or None,
                "franchise_name": franchise_names.get(franchise_id, franchise_id or "Unknown"),
                "player_id": local.player_id,
                "player_name": player.name if player else local.player_id,
                "position": player.position if player else None,
                "nfl_team": player.nfl_team if player else None,
                "completed": True,
                "order_source": "Locally recorded picks",
            }
        )
    return slots


def get_or_create_session(db: Session, league: League) -> DraftSession:
    session = db.scalar(
        select(DraftSession).where(
            DraftSession.league_id == league.id, DraftSession.season == league.season
        )
    )
    if session is None:
        session = DraftSession(league_id=league.id, season=league.season)
        db.add(session)
        db.commit()
        db.refresh(session)
    return session


def set_draft_live(db: Session, league_id: str, is_live: bool) -> dict[str, Any]:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    session = get_or_create_session(db, league)
    session.status = "live" if is_live else "paused"
    if is_live:
        mock_session = get_or_create_mock_session(db, league)
        mock_session.enabled = False
        mock_session.revision += 1
    db.commit()
    db.refresh(session)
    return {
        "is_live": session.status == "live",
        "status": session.status,
    }


def get_or_create_mock_session(db: Session, league: League) -> MockDraftSession:
    session = db.scalar(
        select(MockDraftSession).where(
            MockDraftSession.league_id == league.id,
            MockDraftSession.season == league.season,
        )
    )
    if session is None:
        session = MockDraftSession(league_id=league.id, season=league.season)
        db.add(session)
        db.commit()
        db.refresh(session)
    return session


def set_mock_draft_enabled(
    db: Session, league_id: str, enabled: bool, *, actor: str | None = None
) -> dict[str, Any]:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    mock_session = get_or_create_mock_session(db, league)
    mock_session.enabled = enabled
    mock_session.revision += 1
    mock_session.updated_by = actor
    mock_session.updated_at = datetime.now(UTC)
    if enabled:
        get_or_create_session(db, league).status = "paused"
    db.commit()
    return {
        "enabled": mock_session.enabled,
        "revision": mock_session.revision,
        "updated_by": mock_session.updated_by,
        "updated_at": mock_session.updated_at,
    }


def mock_draft_status(db: Session, league_id: str) -> dict[str, Any]:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    session = get_or_create_mock_session(db, league)
    pick_count = int(
        db.scalar(
            select(func.count(MockDraftPick.id)).where(MockDraftPick.session_id == session.id)
        )
        or 0
    )
    return {
        "enabled": session.enabled,
        "revision": session.revision,
        "pick_count": pick_count,
        "updated_by": session.updated_by,
        "updated_at": session.updated_at,
    }


def mock_pick_json(db: Session, pick: MockDraftPick) -> dict[str, Any]:
    player = db.get(Player, pick.player_id)
    franchise = (
        db.scalar(
            select(Franchise).where(
                Franchise.league_id == pick.league_id,
                Franchise.id == pick.franchise_id,
            )
        )
        if pick.franchise_id
        else None
    )
    return {
        "id": pick.id,
        "session_id": pick.session_id,
        "league_id": pick.league_id,
        "player_id": pick.player_id,
        "player_name": player.name if player else pick.player_id,
        "position": player.position if player else None,
        "nfl_team": player.nfl_team if player else None,
        "franchise_id": pick.franchise_id,
        "franchise_name": franchise.name if franchise else None,
        "round": pick.round,
        "pick": pick.pick,
        "overall_pick": pick.overall_pick,
        "source": "mock",
        "selected_by": pick.selected_by,
        "selected_at": pick.selected_at.isoformat(),
    }


def add_mock_pick(db: Session, payload: DraftPickCreate, *, actor: str) -> MockDraftPick:
    league = db.scalar(select(League).where(League.id == payload.league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    if db.get(Player, payload.player_id) is None:
        raise DraftValidationError("Player does not exist")
    if payload.franchise_id and not db.scalar(
        select(Franchise).where(
            Franchise.league_id == payload.league_id,
            Franchise.id == payload.franchise_id,
        )
    ):
        raise DraftValidationError("Franchise does not exist")
    session = get_or_create_mock_session(db, league)
    if not session.enabled:
        raise DraftValidationError("Shared mock draft is not enabled")
    overall = payload.overall_pick
    if overall is None:
        overall = (
            int(
                db.scalar(
                    select(func.coalesce(func.max(MockDraftPick.overall_pick), 0)).where(
                        MockDraftPick.session_id == session.id
                    )
                )
                or 0
            )
            + 1
        )
    pick = MockDraftPick(
        session_id=session.id,
        league_id=payload.league_id,
        player_id=payload.player_id,
        franchise_id=payload.franchise_id,
        round=payload.round,
        pick=payload.pick,
        overall_pick=overall,
        selected_by=actor,
    )
    try:
        db.add(pick)
        db.flush()
        session.revision += 1
        session.updated_by = actor
        session.updated_at = datetime.now(UTC)
        db.commit()
        db.refresh(pick)
    except IntegrityError as exc:
        db.rollback()
        raise DraftValidationError(
            "That player or mock draft slot was selected by another participant"
        ) from exc
    return pick


def reset_mock_draft(db: Session, league_id: str, *, actor: str) -> dict[str, Any]:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    session = get_or_create_mock_session(db, league)
    removed = int(
        db.scalar(
            select(func.count(MockDraftPick.id)).where(MockDraftPick.session_id == session.id)
        )
        or 0
    )
    db.execute(delete(MockDraftPick).where(MockDraftPick.session_id == session.id))
    session.revision += 1
    session.updated_by = actor
    session.updated_at = datetime.now(UTC)
    db.commit()
    return {"removed": removed, **mock_draft_status(db, league_id)}


def pick_json(db: Session, pick: DraftPick) -> dict[str, Any]:
    player = db.get(Player, pick.player_id)
    franchise = (
        db.scalar(
            select(Franchise).where(
                Franchise.league_id == pick.league_id, Franchise.id == pick.franchise_id
            )
        )
        if pick.franchise_id
        else None
    )
    return {
        "id": pick.id,
        "session_id": pick.session_id,
        "league_id": pick.league_id,
        "player_id": pick.player_id,
        "player_name": player.name if player else pick.player_id,
        "position": player.position if player else None,
        "nfl_team": player.nfl_team if player else None,
        "franchise_id": pick.franchise_id,
        "franchise_name": franchise.name if franchise else None,
        "round": pick.round,
        "pick": pick.pick,
        "overall_pick": pick.overall_pick,
        "source": pick.source,
        "selected_at": pick.selected_at.isoformat(),
        "version": pick.version,
    }


def _snapshot(pick: DraftPick) -> dict[str, Any]:
    return {
        "id": pick.id,
        "session_id": pick.session_id,
        "league_id": pick.league_id,
        "player_id": pick.player_id,
        "franchise_id": pick.franchise_id,
        "round": pick.round,
        "pick": pick.pick,
        "overall_pick": pick.overall_pick,
        "source": pick.source,
        "selected_at": pick.selected_at.isoformat(),
        "version": pick.version,
    }


def _restore(db: Session, data: dict[str, Any]) -> DraftPick:
    pick = DraftPick(
        id=str(data["id"]),
        session_id=str(data["session_id"]),
        league_id=str(data["league_id"]),
        player_id=str(data["player_id"]),
        franchise_id=str(data["franchise_id"]) if data.get("franchise_id") else None,
        round=int(data["round"]) if data.get("round") else None,
        pick=int(data["pick"]) if data.get("pick") else None,
        overall_pick=int(data["overall_pick"]) if data.get("overall_pick") else None,
        source=str(data.get("source", "local")),
        selected_at=datetime.fromisoformat(str(data["selected_at"])),
        version=int(data.get("version", 1)),
    )
    db.add(pick)
    return pick


def add_pick(db: Session, payload: DraftPickCreate, *, source: str = "local") -> DraftPick:
    league = db.scalar(select(League).where(League.id == payload.league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    if db.get(Player, payload.player_id) is None:
        raise DraftValidationError("Player does not exist")
    if payload.franchise_id and not db.scalar(
        select(Franchise).where(
            Franchise.league_id == payload.league_id,
            Franchise.id == payload.franchise_id,
        )
    ):
        raise DraftValidationError("Franchise does not exist")
    availability = availability_maps(db, payload.league_id)
    if payload.player_id in availability["unavailable"]:
        raise DraftValidationError("Player is already rostered, kept, purchased, or drafted")
    session = get_or_create_session(db, league)
    overall = payload.overall_pick
    if overall is None:
        current = db.scalar(
            select(func.coalesce(func.max(DraftPick.overall_pick), 0)).where(
                DraftPick.session_id == session.id
            )
        )
        overall = int(current or 0) + 1
    pick = DraftPick(
        session_id=session.id,
        league_id=payload.league_id,
        player_id=payload.player_id,
        franchise_id=payload.franchise_id,
        round=payload.round,
        pick=payload.pick,
        overall_pick=overall,
        source=source,
    )
    try:
        db.add(pick)
        db.flush()
        db.add(
            DraftAuditEvent(
                league_id=payload.league_id,
                action="create",
                entity_id=pick.id,
                after_json=_snapshot(pick),
            )
        )
        if session.status not in {"live", "paused"}:
            session.status = "in_progress"
        current_picks = list(
            db.scalars(
                select(DraftPick)
                .where(DraftPick.session_id == session.id)
                .order_by(DraftPick.overall_pick, DraftPick.selected_at)
            )
        )
        order, _ = _draft_order(db, payload.league_id, current_picks)
        next_slot = next((slot for slot in order if not slot["completed"]), None)
        session.current_round = next_slot["round"] if next_slot else None
        session.current_pick = next_slot["pick"] if next_slot else None
        db.commit()
        db.refresh(pick)
        return pick
    except IntegrityError as exc:
        db.rollback()
        raise DraftValidationError("Player or pick slot was already recorded") from exc


def update_pick(db: Session, pick_id: str, payload: DraftPickUpdate) -> DraftPick:
    pick = db.scalar(select(DraftPick).where(DraftPick.id == pick_id).with_for_update())
    if pick is None:
        raise DraftValidationError("Draft pick does not exist")
    if pick.version != payload.version:
        raise DraftValidationError("Draft pick changed in another session; refresh first")
    if payload.player_id is not None:
        if db.get(Player, payload.player_id) is None:
            raise DraftValidationError("Player does not exist")
        availability = availability_maps(db, pick.league_id)
        if payload.player_id != pick.player_id and payload.player_id in availability["unavailable"]:
            raise DraftValidationError(
                "Replacement player is already rostered, kept, purchased, or drafted"
            )
    if payload.franchise_id and not db.scalar(
        select(Franchise).where(
            Franchise.league_id == pick.league_id,
            Franchise.id == payload.franchise_id,
        )
    ):
        raise DraftValidationError("Franchise does not exist")
    before = _snapshot(pick)
    if payload.player_id is not None:
        pick.player_id = payload.player_id
    pick.franchise_id = payload.franchise_id
    pick.round = payload.round
    pick.pick = payload.pick
    pick.overall_pick = payload.overall_pick
    pick.version += 1
    db.add(
        DraftAuditEvent(
            league_id=pick.league_id,
            action="update",
            entity_id=pick.id,
            before_json=before,
            after_json=_snapshot(pick),
        )
    )
    try:
        db.commit()
        db.refresh(pick)
        return pick
    except IntegrityError as exc:
        db.rollback()
        raise DraftValidationError("Player or pick slot was already recorded") from exc


def remove_pick(db: Session, pick_id: str) -> None:
    pick = db.get(DraftPick, pick_id)
    if pick is None:
        raise DraftValidationError("Draft pick does not exist")
    before = _snapshot(pick)
    league_id = pick.league_id
    db.delete(pick)
    db.add(
        DraftAuditEvent(
            league_id=league_id,
            action="delete",
            entity_id=pick_id,
            before_json=before,
        )
    )
    db.commit()


def undo_draft(db: Session, league_id: str) -> None:
    event = db.scalar(
        select(DraftAuditEvent)
        .where(DraftAuditEvent.league_id == league_id, DraftAuditEvent.undone.is_(False))
        .order_by(DraftAuditEvent.id.desc())
    )
    if event is None:
        raise DraftValidationError("Nothing to undo")
    if event.action == "create" and event.entity_id:
        db.execute(delete(DraftPick).where(DraftPick.id == event.entity_id))
    elif event.action == "delete" and event.before_json:
        _restore(db, event.before_json)
    elif event.action == "update" and event.before_json and event.entity_id:
        current = db.get(DraftPick, event.entity_id)
        if current:
            previous = event.before_json
            current.player_id = str(previous["player_id"])
            current.franchise_id = previous.get("franchise_id")
            current.round = previous.get("round")
            current.pick = previous.get("pick")
            current.overall_pick = previous.get("overall_pick")
            current.version = int(previous.get("version", 1))
    event.undone = True
    db.commit()


def draft_state(
    db: Session,
    league_id: str,
    franchise_id: str | None = None,
    *,
    include_intelligence: bool = False,
) -> dict[str, Any]:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    session = get_or_create_session(db, league)
    picks = list(
        db.scalars(
            select(DraftPick)
            .where(DraftPick.session_id == session.id)
            .order_by(DraftPick.overall_pick, DraftPick.selected_at)
        )
    )
    board = draftable_consensus(db, league_id)
    queue = sorted(
        (
            row
            for row in board
            if row["preference"]["queue_order"] is not None or row["preference"]["target"]
        ),
        key=lambda row: (
            row["preference"]["queue_order"] is None,
            row["preference"]["queue_order"] or 99999,
            row["consensus_rank"],
        ),
    )
    tiers: dict[str, int] = {}
    for row in board:
        if row["available"] and row["tier"] is not None:
            key = f"{row['position']}:T{row['tier']}"
            tiers[key] = tiers.get(key, 0) + 1
    order, order_fetched_at = _draft_order(db, league_id, picks)
    current_drafter = next((slot for slot in order if not slot["completed"]), None)
    result = {
        "session": {
            "id": session.id,
            "league_id": session.league_id,
            "season": session.season,
            "status": session.status,
            "current_round": current_drafter["round"] if current_drafter else None,
            "current_pick": current_drafter["pick"] if current_drafter else None,
            "source": session.source,
            "synced_at": session.synced_at.isoformat() if session.synced_at else None,
        },
        "live": {
            "is_live": session.status == "live",
            "status": session.status,
        },
        "picks": [pick_json(db, item) for item in picks],
        "draft_order": order,
        "current_drafter": current_drafter,
        "order_source": order[0]["order_source"] if order else "Unavailable",
        "order_fetched_at": order_fetched_at,
        "queue": [
            {
                "player_id": row["player_id"],
                "player_name": row["player_name"],
                "position": row["position"],
                "queue_order": row["preference"]["queue_order"],
                "target": row["preference"]["target"],
            }
            for row in queue
        ],
        "tier_counts": tiers,
    }
    if include_intelligence:
        result.update(
            _draft_intelligence_payload(
                db,
                league,
                board,
                order,
                franchise_id,
            )
        )
    return result


def mock_draft_state(
    db: Session,
    league_id: str,
    franchise_id: str | None = None,
    *,
    include_intelligence: bool = True,
) -> dict[str, Any]:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    session = get_or_create_mock_session(db, league)
    base = draft_state(
        db,
        league_id,
        franchise_id,
        include_intelligence=include_intelligence,
    )
    picks = list(
        db.scalars(
            select(MockDraftPick)
            .where(MockDraftPick.session_id == session.id)
            .order_by(MockDraftPick.overall_pick, MockDraftPick.selected_at)
        )
    )
    picks_by_overall = {pick.overall_pick: pick for pick in picks}
    order: list[dict[str, Any]] = []
    for raw_slot in base.get("draft_order", []):
        slot = dict(raw_slot)
        mock_pick = picks_by_overall.get(int(slot["overall_pick"]))
        slot.update(
            {
                "player_id": None,
                "player_name": None,
                "position": None,
                "nfl_team": None,
                "completed": False,
                "order_source": "MFL order · shared mock results",
            }
        )
        if mock_pick is not None:
            player = db.get(Player, mock_pick.player_id)
            slot.update(
                {
                    "player_id": mock_pick.player_id,
                    "player_name": player.name if player else mock_pick.player_id,
                    "position": player.position if player else None,
                    "nfl_team": player.nfl_team if player else None,
                    "completed": True,
                    "selected_by": mock_pick.selected_by,
                }
            )
        order.append(slot)
    current_drafter = next((slot for slot in order if not slot["completed"]), None)
    selected_ids = {pick.player_id for pick in picks}
    if include_intelligence and base.get("intelligence"):
        intelligence = base["intelligence"]
        intelligence["recommendations"] = [
            row
            for row in intelligence.get("recommendations", [])
            if row["player_id"] not in selected_ids
        ]
    base.update(
        {
            "mode": "mock",
            "session": {
                "id": session.id,
                "league_id": league_id,
                "season": league.season,
                "status": "mock_live" if session.enabled else "mock_paused",
                "current_round": current_drafter["round"] if current_drafter else None,
                "current_pick": current_drafter["pick"] if current_drafter else None,
                "source": "mock",
                "synced_at": None,
            },
            "live": {"is_live": False, "status": "paused"},
            "mock": {
                "enabled": session.enabled,
                "revision": session.revision,
                "pick_count": len(picks),
                "updated_by": session.updated_by,
                "updated_at": session.updated_at,
            },
            "permissions": {
                "can_make_pick": session.enabled,
                "locked_reason": None if session.enabled else "Shared mock draft is not enabled",
            },
            "picks": [mock_pick_json(db, pick) for pick in picks],
            "draft_order": order,
            "current_drafter": current_drafter,
            "order_source": "MFL order · shared mock results" if order else "Unavailable",
        }
    )
    return base


def recommendations(
    db: Session, league_id: str, franchise_id: str | None = None, limit: int = 12
) -> list[dict[str, Any]]:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    needs = franchise_position_needs(db, league_id, franchise_id) if franchise_id else {}
    rows = [row for row in draftable_consensus(db, league_id) if row["available"]]
    for row in rows:
        need = needs.get(str(row["position"]).upper(), 0)
        base = float(row["consensus_score"])
        row["recommendation_score"] = round(base + need * 0.12, 4)
        reasons = []
        if need:
            reasons.append(f"roster needs {need} more {row['position']} starter(s)")
        if row["tier"] is not None:
            reasons.append(f"Tier {row['tier']}")
        if row["adp"] and row["consensus_rank"] < float(row["adp"]):
            reasons.append("value ahead of market ADP")
        if row["preference"]["target"]:
            reasons.append("marked as a target")
        row["recommendation_reason"] = "; ".join(reasons) or "best remaining league value"
    rows.sort(
        key=lambda row: (
            -row["recommendation_score"],
            row["league_adjusted_rank"] or 99999,
            row["player_id"],
        )
    )
    return rows[:limit]


def _normalized_position(value: Any) -> str:
    position = str(value or "").strip().upper()
    return {"K": "PK", "DST": "DEF", "D/ST": "DEF", "D": "DEF"}.get(position, position)


def _lineup_requirements(league: League) -> dict[str, int]:
    requirements: dict[str, int] = {}
    for raw_position, raw_count in (league.lineup_json or {}).items():
        try:
            count = max(0, int(raw_count or 0))
        except (TypeError, ValueError):
            continue
        position = _normalized_position(raw_position)
        if count:
            requirements[position] = requirements.get(position, 0) + count
    return requirements


def _eligible_positions(slot: str) -> tuple[str, ...]:
    if slot == "FLEX":
        return ("RB", "WR", "TE")
    if slot == "SUPERFLEX":
        return ("QB", "RB", "WR", "TE")
    tokens = slot.replace("|", "+").replace(",", "+").split("+")
    return tuple(dict.fromkeys(_normalized_position(token) for token in tokens if token.strip()))


def _position_plan(
    requirements: dict[str, int], position_counts: dict[str, int]
) -> list[dict[str, Any]]:
    order = ("QB", "RB", "WR", "TE", "FLEX", "SUPERFLEX", "PK", "DEF")
    simple = [item for item in requirements if len(_eligible_positions(item)) == 1]
    combined = [item for item in requirements if len(_eligible_positions(item)) > 1]
    simple.sort(key=lambda item: (order.index(item) if item in order else 99, item))
    combined.sort(
        key=lambda item: (
            len(_eligible_positions(item)),
            order.index(item) if item in order else 50,
            item,
        )
    )
    result: list[dict[str, Any]] = []
    remaining = dict(position_counts)
    for position in simple:
        required = requirements[position]
        owned = position_counts.get(position, 0)
        remaining[position] = max(0, owned - required)
        result.append(
            {
                "position": position,
                "eligible_positions": [position],
                "owned": owned,
                "required": required,
                "still_needed": max(0, required - owned),
            }
        )
    for position in combined:
        required = requirements[position]
        eligible = _eligible_positions(position)
        available = sum(remaining.get(item, 0) for item in eligible)
        covered = min(required, available)
        to_consume = covered
        for item in sorted(eligible, key=lambda key: remaining.get(key, 0), reverse=True):
            used = min(to_consume, remaining.get(item, 0))
            remaining[item] = max(0, remaining.get(item, 0) - used)
            to_consume -= used
            if not to_consume:
                break
        result.append(
            {
                "position": position,
                "eligible_positions": list(eligible),
                "owned": covered,
                "required": required,
                "still_needed": max(0, required - covered),
            }
        )
    return result


def _survival_estimate(row: dict[str, Any], target_pick: int | None) -> dict[str, Any] | None:
    if target_pick is None:
        return None
    adp = row.get("adp")
    try:
        raw_expected = adp if adp not in (None, "") else row["consensus_rank"]
        expected = float(str(raw_expected))
    except (TypeError, ValueError, KeyError):
        return None
    probability = 100 / (1 + math.exp((target_pick - expected) / 7.5))
    chance = max(5, min(95, int(round(probability))))
    return {
        "chance": chance,
        "target_pick": target_pick,
        "basis": "market ADP" if adp not in (None, "") else "live board rank",
        "basis_rank": round(expected, 1),
        "method": (
            "Heuristic based on the player's market ADP or live board rank and the picks remaining."
        ),
    }


def _draft_intelligence_payload(
    db: Session,
    league: League,
    board: list[dict[str, Any]],
    order: list[dict[str, Any]],
    franchise_id: str | None,
) -> dict[str, Any]:
    ordered = sorted(order, key=lambda item: int(item.get("overall_pick") or 0))
    completed = [item for item in ordered if item.get("completed")]
    completed_player_ids = {str(item["player_id"]) for item in completed if item.get("player_id")}
    board_by_id = {row["player_id"]: row for row in board}
    projections = build_projection_board(db, league, board)
    available = [
        row
        for row in board
        if row.get("available") and row["player_id"] not in completed_player_ids
    ]

    franchises = {
        item.id: item
        for item in db.scalars(select(Franchise).where(Franchise.league_id == league.id))
    }
    selected_franchise = franchises.get(franchise_id or "")
    owned_by_franchise: dict[str, set[str]] = {item: set() for item in franchises}
    for owner_id, player_id in db.execute(
        select(RosterAssignment.franchise_id, RosterAssignment.player_id).where(
            RosterAssignment.league_id == league.id
        )
    ):
        owned_by_franchise.setdefault(str(owner_id), set()).add(str(player_id))
    for slot in completed:
        if slot.get("franchise_id") and slot.get("player_id"):
            owned_by_franchise.setdefault(str(slot["franchise_id"]), set()).add(
                str(slot["player_id"])
            )

    all_owned_ids = set().union(*owned_by_franchise.values()) if owned_by_franchise else set()
    players_by_id = (
        {item.id: item for item in db.scalars(select(Player).where(Player.id.in_(all_owned_ids)))}
        if all_owned_ids
        else {}
    )
    position_counts_by_franchise: dict[str, dict[str, int]] = {}
    for owner_id, player_ids in owned_by_franchise.items():
        counts: dict[str, int] = {}
        for player_id in player_ids:
            player = players_by_id.get(player_id)
            if player is None:
                continue
            position = _normalized_position(player.position)
            counts[position] = counts.get(position, 0) + 1
        position_counts_by_franchise[owner_id] = counts

    ranked_pool_size = max(
        (int(row["consensus_rank"]) for row in board if row.get("consensus_rank") is not None),
        default=0,
    )
    roster_strength_by_franchise: dict[str, dict[str, Any]] = {}
    for owner_id, player_ids in owned_by_franchise.items():
        ranks = sorted(
            int(board_by_id[player_id]["consensus_rank"])
            for player_id in player_ids
            if player_id in board_by_id and board_by_id[player_id].get("consensus_rank") is not None
        )
        score = (
            sum((ranked_pool_size + 1 - rank) / ranked_pool_size * 100 for rank in ranks)
            if ranked_pool_size
            else 0
        )
        roster_strength_by_franchise[owner_id] = {
            "roster_strength_score": round(score, 1),
            "roster_strength_average_rank": round(sum(ranks) / len(ranks), 1) if ranks else None,
            "roster_strength_ranked_players": len(ranks),
        }
    strength_order = sorted(
        roster_strength_by_franchise,
        key=lambda owner_id: (
            -roster_strength_by_franchise[owner_id]["roster_strength_score"],
            roster_strength_by_franchise[owner_id]["roster_strength_average_rank"] or 999999,
            owner_id,
        ),
    )
    previous_score: float | None = None
    displayed_rank = 0
    for index, owner_id in enumerate(strength_order, 1):
        score = roster_strength_by_franchise[owner_id]["roster_strength_score"]
        if previous_score is None or score != previous_score:
            displayed_rank = index
            previous_score = score
        roster_strength_by_franchise[owner_id].update(
            {
                "roster_strength_rank": displayed_rank,
                "roster_strength_team_count": len(franchises),
            }
        )

    requirements = _lineup_requirements(league)
    selected_counts = position_counts_by_franchise.get(franchise_id or "", {})
    position_plan = _position_plan(requirements, selected_counts)
    primary_needs: dict[str, int] = {}
    for item in position_plan:
        if not item["still_needed"]:
            continue
        for position in item["eligible_positions"]:
            primary_needs[position] = primary_needs.get(position, 0) + item["still_needed"]

    remaining_slots = [item for item in ordered if not item.get("completed")]
    my_indexes = [
        index
        for index, item in enumerate(remaining_slots)
        if franchise_id and item.get("franchise_id") == franchise_id
    ]
    next_slot = remaining_slots[my_indexes[0]] if my_indexes else None
    following_slot = remaining_slots[my_indexes[1]] if len(my_indexes) > 1 else None
    on_clock = bool(my_indexes and my_indexes[0] == 0)
    if on_clock and len(my_indexes) > 1 and following_slot is not None:
        survival_target_pick = int(following_slot["overall_pick"])
        opponent_window = remaining_slots[1 : my_indexes[1]]
    elif my_indexes and next_slot is not None:
        survival_target_pick = int(next_slot["overall_pick"])
        opponent_window = remaining_slots[: my_indexes[0]]
    else:
        survival_target_pick = None
        opponent_window = []

    seen_opponents: set[str] = set()
    opponent_needs: list[dict[str, Any]] = []
    for slot in opponent_window:
        owner_id = str(slot.get("franchise_id") or "")
        if not owner_id or owner_id == franchise_id or owner_id in seen_opponents:
            continue
        seen_opponents.add(owner_id)
        counts = position_counts_by_franchise.get(owner_id, {})
        plan = _position_plan(requirements, counts)
        needs = [
            {"position": item["position"], "count": item["still_needed"]}
            for item in plan
            if item["still_needed"] and item["position"] not in {"FLEX", "SUPERFLEX"}
        ]
        opponent = franchises.get(owner_id)
        opponent_needs.append(
            {
                "franchise_id": owner_id,
                "franchise_name": opponent.name
                if opponent is not None
                else slot.get("franchise_name") or owner_id,
                "next_pick": slot.get("overall_pick"),
                "needs": needs[:3],
            }
        )
        if len(opponent_needs) >= 6:
            break

    opponent_insights: list[dict[str, Any]] = []
    for owner_id, opponent in franchises.items():
        if owner_id == franchise_id:
            continue
        counts = position_counts_by_franchise.get(owner_id, {})
        plan = _position_plan(requirements, counts)
        needs = [
            {"position": item["position"], "count": item["still_needed"]}
            for item in plan
            if item["still_needed"] and item["position"] not in {"FLEX", "SUPERFLEX"}
        ]
        future_picks = [
            int(item["overall_pick"])
            for item in remaining_slots
            if item.get("franchise_id") == owner_id and item.get("overall_pick")
        ]
        recent_owner_picks = [
            {
                "overall_pick": item.get("overall_pick"),
                "player_name": item.get("player_name"),
                "position": item.get("position"),
            }
            for item in completed
            if item.get("franchise_id") == owner_id
        ][-3:][::-1]
        opponent_insights.append(
            {
                "franchise_id": owner_id,
                "franchise_name": opponent.name,
                "next_pick": future_picks[0] if future_picks else None,
                "picks_remaining": len(future_picks),
                "roster_count": len(owned_by_franchise.get(owner_id, set())),
                "position_counts": counts,
                "needs": needs[:4],
                "recent_picks": recent_owner_picks,
                "on_clock": bool(
                    remaining_slots and remaining_slots[0].get("franchise_id") == owner_id
                ),
                **roster_strength_by_franchise.get(owner_id, {}),
            }
        )
    opponent_insights.sort(
        key=lambda item: (
            item["next_pick"] is None,
            item["next_pick"] or 999999,
            item["franchise_name"],
        )
    )

    recent_positions = [
        _normalized_position(item.get("position"))
        for item in completed[-6:]
        if item.get("position")
    ]
    run_counts = Counter(recent_positions)
    position_runs = [
        {
            "position": position,
            "count": count,
            "window": len(recent_positions),
            "label": f"{count} {position}s in the last {len(recent_positions)} picks",
        }
        for position, count in run_counts.most_common()
        if count >= 3
    ]

    tier_cliffs: list[dict[str, Any]] = []
    positions = sorted({_normalized_position(row.get("position")) for row in available})
    for position in positions:
        if position == "DEF":
            continue
        rows = sorted(
            (row for row in available if _normalized_position(row.get("position")) == position),
            key=lambda item: int(item.get("consensus_rank") or 99999),
        )
        ranked_tiers = [row for row in rows if row.get("tier") is not None]
        if not ranked_tiers:
            continue
        top_tier = int(ranked_tiers[0]["tier"])
        current_tier = [row for row in ranked_tiers if int(row["tier"]) == top_tier]
        if len(current_tier) > 3:
            continue
        next_tier_row = next((row for row in ranked_tiers if int(row["tier"]) > top_tier), None)
        last_current_rank = max(int(row["consensus_rank"]) for row in current_tier)
        next_rank = int(next_tier_row["consensus_rank"]) if next_tier_row else None
        tier_cliffs.append(
            {
                "position": position,
                "tier": top_tier,
                "remaining": len(current_tier),
                "next_tier": int(next_tier_row["tier"]) if next_tier_row else None,
                "rank_gap": max(0, next_rank - last_current_rank) if next_rank else None,
                "players": [row["player_name"] for row in current_tier[:3]],
            }
        )
    tier_cliffs.sort(key=lambda item: (item["remaining"], item["tier"], item["position"]))

    pick_values: list[dict[str, Any]] = []
    for slot in completed[-6:][::-1]:
        row = board_by_id.get(str(slot.get("player_id") or ""))
        if row is None:
            continue
        raw_basis = row.get("adp")
        basis_name = "ADP"
        try:
            basis_rank = float(str(raw_basis))
        except (TypeError, ValueError):
            basis_rank = float(row.get("consensus_rank") or 0)
            basis_name = "live board"
        if basis_rank <= 0:
            continue
        delta = float(slot.get("overall_pick") or 0) - basis_rank
        if delta >= 8:
            label = "Strong value"
        elif delta >= 3:
            label = "Value"
        elif delta <= -8:
            label = "Reach"
        elif delta <= -3:
            label = "Slight reach"
        else:
            label = "Market range"
        pick_values.append(
            {
                "overall_pick": slot.get("overall_pick"),
                "player_name": slot.get("player_name"),
                "position": slot.get("position"),
                "franchise_name": slot.get("franchise_name"),
                "basis": basis_name,
                "basis_rank": round(basis_rank, 1),
                "delta": round(delta, 1),
                "label": label,
            }
        )

    tier_remaining = Counter(
        (_normalized_position(row.get("position")), int(row["tier"]))
        for row in available
        if row.get("tier") is not None
    )
    selected_player_ids = owned_by_franchise.get(franchise_id or "", set())
    selected_byes = Counter(
        int(players_by_id[player_id].bye_week or 0)
        for player_id in selected_player_ids
        if player_id in players_by_id and players_by_id[player_id].bye_week
    )
    future_my_picks = [
        int(item["overall_pick"])
        for item in remaining_slots
        if franchise_id and item.get("franchise_id") == franchise_id and item.get("overall_pick")
    ]
    recommended: list[dict[str, Any]] = []
    for row in available:
        position = _normalized_position(row.get("position"))
        need_slots = primary_needs.get(position, 0)
        remaining_in_tier = tier_remaining.get((position, int(row.get("tier") or 0)), 0)
        preference = row.get("preference") or {}
        score = float(row.get("consensus_score") or 0) + min(need_slots, 2) * 0.12
        if remaining_in_tier <= 2:
            score += 0.06
        if preference.get("target"):
            score += 0.12
        if preference.get("fade"):
            score -= 0.12
        if preference.get("do_not_draft"):
            score -= 100
        survival = _survival_estimate(row, survival_target_pick)
        projection = projections.get(row["player_id"], {})
        same_position_wait = [
            candidate
            for candidate in available
            if candidate["player_id"] != row["player_id"]
            and _normalized_position(candidate.get("position")) == position
            and (
                survival_target_pick is None
                or int(candidate.get("consensus_rank") or 99999) >= max(1, survival_target_pick - 8)
            )
        ]
        same_position_wait.sort(key=lambda item: int(item.get("consensus_rank") or 99999))
        wait_projection = (
            projections.get(same_position_wait[0]["player_id"], {}) if same_position_wait else {}
        )
        candidate_ids = set(selected_player_ids) | {str(row["player_id"])}
        used_ids = set(candidate_ids)
        for target_pick in future_my_picks[1:]:
            expected = next(
                (
                    candidate
                    for candidate in available
                    if candidate["player_id"] not in used_ids
                    and int(candidate.get("consensus_rank") or 99999) >= max(1, target_pick - 7)
                ),
                None,
            )
            if expected is not None:
                used_ids.add(str(expected["player_id"]))
        expected_roster = lineup_projection(used_ids, board_by_id, projections, league.lineup_json)
        alternatives = []
        if survival_target_pick is not None:
            alternative_pool = sorted(
                (
                    candidate
                    for candidate in available
                    if candidate["player_id"] != row["player_id"]
                ),
                key=lambda candidate: (
                    abs(int(candidate.get("consensus_rank") or 99999) - survival_target_pick),
                    0
                    if primary_needs.get(_normalized_position(candidate.get("position")), 0)
                    else 1,
                    int(candidate.get("consensus_rank") or 99999),
                ),
            )
            for alternative in alternative_pool[:3]:
                alternative_projection = projections.get(alternative["player_id"], {})
                alternatives.append(
                    {
                        "player_id": alternative["player_id"],
                        "player_name": alternative["player_name"],
                        "position": _normalized_position(alternative.get("position")),
                        "consensus_rank": alternative.get("consensus_rank"),
                        "median": alternative_projection.get("median"),
                        "survival": _survival_estimate(alternative, survival_target_pick),
                    }
                )
        disappearance = 100 - int((survival or {}).get("chance", 50))
        if remaining_in_tier <= 2:
            cliff_probability = min(95, disappearance + 18)
        elif remaining_in_tier <= 4:
            cliff_probability = int(round(disappearance * 0.65))
        elif remaining_in_tier <= 8:
            cliff_probability = int(round(disappearance * 0.35))
        else:
            cliff_probability = int(round(disappearance * min(0.2, 4 / max(remaining_in_tier, 1))))
        cliff_probability = max(2, cliff_probability)
        player = players_by_id.get(str(row["player_id"])) or db.get(Player, str(row["player_id"]))
        bye_week = int(player.bye_week) if player is not None and player.bye_week else None
        bye_overlap = selected_byes.get(bye_week, 0) if bye_week else 0
        median = float(projection.get("median") or 0)
        wait_median = float(wait_projection.get("median") or 0)
        confidence = int(
            round(
                (int(projection.get("confidence") or 35) * 0.75) + (75 if survival else 45) * 0.25
            )
        )
        reasons = [f"#{row.get('consensus_rank')} on your live board"]
        if need_slots:
            suffix = "s" if need_slots != 1 else ""
            reasons.append(f"fills {need_slots} open {position} starter slot{suffix}")
        if row.get("tier") is not None and remaining_in_tier <= 3:
            reasons.append(
                f"{remaining_in_tier} player"
                f"{'s' if remaining_in_tier != 1 else ''} left in Tier {row['tier']}"
            )
        try:
            if row.get("adp") and float(row["adp"]) > float(row["consensus_rank"]):
                reasons.append("priced below your board by ADP")
        except (TypeError, ValueError):
            pass
        if preference.get("target"):
            reasons.append("marked as your target")
        if preference.get("fade"):
            reasons.append("marked as a fade")
        recommended.append(
            {
                "player_id": row["player_id"],
                "player_name": row["player_name"],
                "position": position,
                "nfl_team": row.get("nfl_team"),
                "consensus_rank": row.get("consensus_rank"),
                "tier": row.get("tier"),
                "adp": row.get("adp"),
                "value_over_replacement": row.get("value_over_replacement"),
                "need_slots": need_slots,
                "remaining_in_tier": remaining_in_tier,
                "survival": survival,
                "recommendation_score": round(score, 4),
                "recommendation_reason": "; ".join(reasons),
                "projection": projection,
                "scenario": {
                    "expected_final_roster_strength": expected_roster["roster_strength"],
                    "projected_starter_points": expected_roster["projected_starter_points"],
                    "survival_chance": (survival or {}).get("chance"),
                    "next_pick": survival_target_pick,
                    "best_likely_alternatives": alternatives,
                    "position_cliff_probability": cliff_probability,
                    "bye_week": bye_week,
                    "bye_overlap": bye_overlap,
                    "bye_consequence": (
                        f"Adds a third Week {bye_week} bye to this roster"
                        if bye_week and bye_overlap >= 2
                        else (
                            f"Overlaps with {bye_overlap} current "
                            f"player{'s' if bye_overlap != 1 else ''} in Week {bye_week}"
                        )
                        if bye_week and bye_overlap
                        else f"No current Week {bye_week} overlap"
                        if bye_week
                        else "Bye week is not available"
                    ),
                    "lineup_consequence": (
                        f"Fills an open {position} starter slot"
                        if need_slots
                        else "Adds depth rather than filling an open primary starter"
                    ),
                    "expected_value_gained_vs_waiting": round(median - wait_median, 1),
                    "waiting_comparison_player": same_position_wait[0]["player_name"]
                    if same_position_wait
                    else None,
                    "confidence": confidence,
                    "confidence_label": "High"
                    if confidence >= 75
                    else "Medium"
                    if confidence >= 55
                    else "Low",
                    "sources": projection.get("sources", []),
                    "method": (
                        "Deterministic real-draft forecast from the current MFL order; "
                        "no mock opponents or bot drafting."
                    ),
                },
            }
        )
    recommended.sort(
        key=lambda item: (
            -item["recommendation_score"],
            item["consensus_rank"] or 99999,
            item["player_id"],
        )
    )

    bye_groups: dict[int, list[str]] = {}
    roster_players: list[dict[str, Any]] = []
    for player_id in selected_player_ids:
        player = players_by_id.get(player_id)
        if player is None:
            continue
        row = board_by_id.get(player_id, {})
        if player.bye_week:
            bye_groups.setdefault(int(player.bye_week), []).append(player.name)
        roster_players.append(
            {
                "player_id": player.id,
                "player_name": player.name,
                "position": _normalized_position(player.position),
                "nfl_team": player.nfl_team,
                "bye_week": player.bye_week,
                "consensus_rank": row.get("consensus_rank"),
            }
        )
    roster_players.sort(
        key=lambda item: (
            item["consensus_rank"] or 99999,
            item["position"],
            item["player_name"],
        )
    )
    bye_warnings = [
        {"week": week, "count": len(names), "players": sorted(names)}
        for week, names in bye_groups.items()
        if len(names) >= 2
    ]
    bye_warnings.sort(key=lambda item: (-int(str(item["count"])), int(str(item["week"]))))

    war_room = {
        "configured": selected_franchise is not None,
        "franchise_id": selected_franchise.id if selected_franchise else None,
        "franchise_name": selected_franchise.name if selected_franchise else None,
        "roster_count": len(selected_player_ids),
        "roster_size": league.roster_size,
        "open_roster_slots": max(0, league.roster_size - len(selected_player_ids)),
        "lineup_requirements": requirements,
        "position_counts": selected_counts,
        "position_plan": position_plan,
        "open_starter_slots": sum(item["still_needed"] for item in position_plan),
        "picks_remaining": len(my_indexes),
        "next_pick": next_slot.get("overall_pick") if next_slot else None,
        "following_pick": following_slot.get("overall_pick") if following_slot else None,
        "picks_until_next": my_indexes[0] if my_indexes else None,
        "on_clock": on_clock,
        "bye_warnings": bye_warnings,
        "roster": roster_players,
        **roster_strength_by_franchise.get(
            franchise_id or "",
            {
                "roster_strength_score": 0,
                "roster_strength_average_rank": None,
                "roster_strength_ranked_players": 0,
                "roster_strength_rank": None,
                "roster_strength_team_count": len(franchises),
            },
        ),
        "roster_strength_method": (
            "Each rostered or drafted player earns points from their place on your live "
            "consensus board; higher-ranked players earn more."
        ),
    }
    intelligence = {
        "position_runs": position_runs,
        "recent_position_counts": dict(run_counts),
        "tier_cliffs": tier_cliffs,
        "pick_values": pick_values,
        "opponent_needs": opponent_needs,
        "opponent_insights": opponent_insights,
        "survival_target_pick": survival_target_pick,
        "survival_method": (
            "Heuristic only; it uses market ADP when available, otherwise the live board rank."
        ),
        "recommendations": recommended[:8],
    }
    return {"war_room": war_room, "intelligence": intelligence}


def draft_intelligence(
    db: Session, league_id: str, franchise_id: str | None = None
) -> dict[str, Any]:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    session = get_or_create_session(db, league)
    picks = list(
        db.scalars(
            select(DraftPick)
            .where(DraftPick.session_id == session.id)
            .order_by(DraftPick.overall_pick, DraftPick.selected_at)
        )
    )
    order, _ = _draft_order(db, league_id, picks)
    board = draftable_consensus(db, league_id)
    return _draft_intelligence_payload(db, league, board, order, franchise_id)


def _walk_draft_rows(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if ("player" in value or "player_id" in value) and (
            "franchise" in value or "franchise_id" in value
        ):
            rows.append(value)
        for child in value.values():
            rows.extend(_walk_draft_rows(child))
    elif isinstance(value, list):
        for child in value:
            rows.extend(_walk_draft_rows(child))
    return rows


def reconcile_preview(db: Session, league_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    local = {
        item.player_id: item
        for item in db.scalars(select(DraftPick).where(DraftPick.league_id == league_id))
    }
    remote_rows = _walk_draft_rows(payload)
    additions: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for index, row in enumerate(remote_rows, 1):
        player_id = str(row.get("player", row.get("player_id", "")))
        franchise_id = str(row.get("franchise", row.get("franchise_id", "")))
        if not player_id:
            continue
        existing = local.get(player_id)
        normalized = {
            "player_id": player_id,
            "franchise_id": franchise_id or None,
            "round": int(row["round"]) if row.get("round") else None,
            "pick": int(row["pick"]) if row.get("pick") else None,
            "overall_pick": int(row.get("overallPick", row.get("overall_pick", index))),
        }
        if existing is None:
            additions.append(normalized)
        elif existing.franchise_id != normalized["franchise_id"]:
            conflicts.append(
                {
                    "player_id": player_id,
                    "local_franchise_id": existing.franchise_id,
                    "mfl_franchise_id": normalized["franchise_id"],
                }
            )
    return {"additions": additions, "conflicts": conflicts, "remote_count": len(remote_rows)}


def apply_reconciliation(db: Session, league_id: str, preview: dict[str, Any]) -> int:
    if preview["conflicts"]:
        raise DraftValidationError("Resolve MFL conflicts before applying reconciliation")
    applied = 0
    for row in preview["additions"]:
        try:
            add_pick(
                db,
                DraftPickCreate(league_id=league_id, **row),
                source="mfl",
            )
            applied += 1
        except DraftValidationError:
            continue
    league = db.scalar(select(League).where(League.id == league_id))
    if league:
        session = get_or_create_session(db, league)
        session.synced_at = datetime.now(UTC)
        session.source = "mfl"
        db.commit()
    return applied


def export_draft_csv(db: Session, league_id: str, directory: Path) -> Path:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    session = get_or_create_session(db, league)
    picks = list(
        db.scalars(
            select(DraftPick)
            .where(DraftPick.session_id == session.id)
            .order_by(DraftPick.overall_pick, DraftPick.selected_at)
        )
    )
    output = io.StringIO(newline="")
    headers = [
        "league_id",
        "season",
        "overall_pick",
        "round",
        "pick",
        "franchise_id",
        "player_id",
        "player_name",
        "position",
        "nfl_team",
        "source",
    ]
    writer = csv.DictWriter(output, fieldnames=headers, lineterminator="\n")
    writer.writeheader()
    for item in picks:
        row = pick_json(db, item)
        writer.writerow(
            {
                "league_id": league_id,
                "season": league.season,
                "overall_pick": item.overall_pick,
                "round": item.round,
                "pick": item.pick,
                "franchise_id": item.franchise_id or "",
                "player_id": item.player_id,
                "player_name": row["player_name"],
                "position": row["position"],
                "nfl_team": row["nfl_team"] or "",
                "source": item.source,
            }
        )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"mfl_draft_results_{league_id}_{league.season}.csv"
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(output.getvalue().encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path
