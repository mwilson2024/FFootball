from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auction import (
    AuctionValidationError,
    add_purchase,
    advance_nomination,
    franchise_budget,
    nomination_state,
)
from app.catalog import draftable_consensus
from app.draft import DraftValidationError, add_mock_pick, mock_draft_state, mock_pick_json
from app.models import (
    AuctionPurchase,
    Franchise,
    League,
    MockDraftPick,
    Player,
    RosterAssignment,
    RosterStatus,
)
from app.schemas import DraftPickCreate, PurchaseCreate


def _position_counts(db: Session, player_ids: set[str]) -> Counter[str]:
    if not player_ids:
        return Counter()
    return Counter(
        str(position or "").upper()
        for position in db.scalars(select(Player.position).where(Player.id.in_(player_ids)))
    )


def _lineup_needs(league: League, counts: Counter[str]) -> dict[str, int]:
    needs: dict[str, int] = {}
    for raw_position, raw_required in (league.lineup_json or {}).items():
        position = str(raw_position or "").upper()
        if position in {"FLEX", "SUPERFLEX"}:
            continue
        try:
            required = max(0, int(raw_required or 0))
        except (TypeError, ValueError):
            continue
        needs[position] = max(0, required - counts.get(position, 0))
    return needs


def _selection_score(row: dict[str, Any], needs: dict[str, int]) -> float:
    rank = int(row.get("consensus_rank") or 99999)
    position = str(row.get("position") or "").upper()
    need = min(2, needs.get(position, 0))
    preference = row.get("preference") or {}
    score = 10_000.0 - rank * 10.0 + need * 25.0
    if preference.get("target"):
        score += 18.0
    if "sleeper" in (preference.get("tags") or []):
        score += 5.0
    if preference.get("fade"):
        score -= 20.0
    return score


def _best_player(
    rows: list[dict[str, Any]],
    needs: dict[str, int],
    *,
    excluded: set[str] | None = None,
) -> dict[str, Any]:
    skipped = excluded or set()
    candidates = [
        row
        for row in rows
        if row.get("available")
        and str(row["player_id"]) not in skipped
        and not (row.get("preference") or {}).get("do_not_draft")
    ]
    if not candidates:
        raise DraftValidationError("No eligible players remain on the CPU board")
    return max(
        candidates,
        key=lambda row: (
            _selection_score(row, needs),
            -int(row.get("consensus_rank") or 99999),
            str(row.get("player_id") or ""),
        ),
    )


def _reason(row: dict[str, Any], needs: dict[str, int]) -> str:
    position = str(row.get("position") or "").upper()
    rank = int(row.get("consensus_rank") or 0)
    tier = row.get("tier")
    parts = [f"#{rank} on the current consensus board"]
    if needs.get(position, 0):
        parts.append(f"fills an open {position} starter need")
    if tier is not None:
        parts.append(f"Tier {tier}")
    preference = row.get("preference") or {}
    if preference.get("target"):
        parts.append("marked as a target")
    elif "sleeper" in (preference.get("tags") or []):
        parts.append("marked as a sleeper")
    return "; ".join(parts)


def make_cpu_mock_pick(db: Session, league_id: str, *, actor: str) -> dict[str, Any]:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    state = mock_draft_state(db, league_id, include_intelligence=False)
    if not state["mock"]["enabled"]:
        raise DraftValidationError("Shared mock draft is not enabled")
    current = state.get("current_drafter")
    if current is None:
        raise DraftValidationError("The shared mock draft is complete")
    franchise_id = str(current.get("franchise_id") or "")
    if not franchise_id:
        raise DraftValidationError("The current mock draft slot has no MFL franchise")

    owned_ids = set(
        db.scalars(
            select(RosterAssignment.player_id).where(
                RosterAssignment.league_id == league_id,
                RosterAssignment.franchise_id == franchise_id,
            )
        )
    )
    owned_ids.update(
        db.scalars(
            select(MockDraftPick.player_id).where(
                MockDraftPick.session_id == state["session"]["id"],
                MockDraftPick.franchise_id == franchise_id,
            )
        )
    )
    selected_ids = {str(item["player_id"]) for item in state.get("picks", [])}
    needs = _lineup_needs(league, _position_counts(db, owned_ids))
    choice = _best_player(draftable_consensus(db, league_id), needs, excluded=selected_ids)
    payload = DraftPickCreate(
        league_id=league_id,
        player_id=str(choice["player_id"]),
        franchise_id=franchise_id,
        round=current.get("round"),
        pick=current.get("pick"),
        overall_pick=current.get("overall_pick"),
        is_mock=True,
    )
    pick = add_mock_pick(db, payload, actor=f"cpu:{actor}")
    result = mock_pick_json(db, pick)
    after = mock_draft_state(db, league_id, include_intelligence=False)
    result.update(
        {
            "cpu": True,
            "reason": _reason(choice, needs),
            "next_drafter": after.get("current_drafter"),
        }
    )
    return result


def make_cpu_auction_purchase(db: Session, league_id: str, *, actor: str) -> dict[str, Any]:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise AuctionValidationError("League does not exist")
    nomination = nomination_state(db, league_id)
    franchise_id = nomination.get("current_franchise_id")
    if not franchise_id:
        raise AuctionValidationError("The auction is complete; no team is left to nominate")
    franchise = db.scalar(
        select(Franchise).where(
            Franchise.league_id == league_id,
            Franchise.id == str(franchise_id),
        )
    )
    if franchise is None:
        raise AuctionValidationError("The current nominating franchise does not exist")
    budget = franchise_budget(db, league, franchise)
    if int(budget["slots_remaining"]) <= 0:
        raise AuctionValidationError("The current nominating team has no open roster slot")

    owned_ids = set(
        db.scalars(
            select(RosterAssignment.player_id).where(
                RosterAssignment.league_id == league_id,
                RosterAssignment.franchise_id == franchise.id,
            )
        )
    )
    owned_ids.update(
        db.scalars(
            select(AuctionPurchase.player_id).where(
                AuctionPurchase.league_id == league_id,
                AuctionPurchase.franchise_id == franchise.id,
                AuctionPurchase.active.is_(True),
            )
        )
    )
    needs = _lineup_needs(league, _position_counts(db, owned_ids))
    choice = _best_player(draftable_consensus(db, league_id), needs)
    minimum = Decimal(league.minimum_bid)
    maximum = Decimal(budget["maximum_bid"])
    recommended = Decimal(
        str(
            choice.get("dynamic_bid")
            or choice.get("suggested_auction_value")
            or minimum
        )
    )
    amount = max(minimum, min(recommended, maximum))
    purchase = add_purchase(
        db,
        PurchaseCreate(
            league_id=league_id,
            franchise_id=franchise.id,
            player_id=str(choice["player_id"]),
            amount=amount,
            status=RosterStatus.ROSTER,
        ),
    )
    advance_nomination(db, league_id, actor=f"cpu:{actor}")
    return {
        "purchase": purchase,
        "reason": _reason(choice, needs),
        "price_basis": "current dynamic bid capped by the team's legal maximum",
        "next_nomination": nomination_state(db, league_id),
    }
