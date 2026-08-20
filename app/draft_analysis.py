from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import draftable_consensus
from app.draft import DraftValidationError, draft_state
from app.models import Franchise, League, Player, RosterAssignment
from app.projections import build_projection_board, lineup_projection


def _grade(rank: int, total: int) -> str:
    percentile = (rank - 1) / max(total, 1)
    return (
        "A"
        if percentile < 0.17
        else "B"
        if percentile < 0.38
        else "C"
        if percentile < 0.63
        else "D"
        if percentile < 0.84
        else "F"
    )


def _ownership(db: Session, league_id: str, order: list[dict[str, Any]]) -> dict[str, set[str]]:
    owned: dict[str, set[str]] = defaultdict(set)
    for franchise_id, player_id in db.execute(
        select(RosterAssignment.franchise_id, RosterAssignment.player_id).where(
            RosterAssignment.league_id == league_id
        )
    ):
        owned[str(franchise_id)].add(str(player_id))
    for slot in order:
        if slot.get("completed") and slot.get("franchise_id") and slot.get("player_id"):
            owned[str(slot["franchise_id"])].add(str(slot["player_id"]))
    return owned


def _standings(
    franchises: list[Franchise],
    ownership: dict[str, set[str]],
    board_by_id: dict[str, dict[str, Any]],
    projections: dict[str, dict[str, Any]],
    lineup: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for franchise in franchises:
        projection = lineup_projection(
            ownership.get(franchise.id, set()), board_by_id, projections, lineup
        )
        rows.append(
            {
                "franchise_id": franchise.id,
                "franchise_name": franchise.name,
                "roster_count": len(ownership.get(franchise.id, set())),
                **projection,
            }
        )
    rows.sort(key=lambda item: (-item["roster_strength"], item["franchise_name"].casefold()))
    if rows:
        low = min(item["roster_strength"] for item in rows)
        high = max(item["roster_strength"] for item in rows)
        spread = max(1.0, high - low)
        for index, row in enumerate(rows, 1):
            row["projected_rank"] = index
            row["projected_win_index"] = round(
                5.0 + (row["roster_strength"] - low) / spread * 5.0, 1
            )
            row["playoff_probability"] = int(
                round(25 + (row["roster_strength"] - low) / spread * 60)
            )
    return rows


def _position_grades(
    franchises: list[Franchise], standings: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    positions = sorted(
        {position for row in standings for position in (row.get("position_points") or {})}
    )
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_id = {row["franchise_id"]: row for row in standings}
    for position in positions:
        ordered = sorted(
            franchises,
            key=lambda team: (
                -float((by_id.get(team.id, {}).get("position_points") or {}).get(position, 0)),
                team.name.casefold(),
            ),
        )
        for rank, team in enumerate(ordered, 1):
            points = float((by_id.get(team.id, {}).get("position_points") or {}).get(position, 0))
            result[team.id].append(
                {
                    "position": position,
                    "grade": _grade(rank, len(franchises)),
                    "league_rank": rank,
                    "projected_points": round(points, 1),
                }
            )
    return result


def _roster_breakdown(
    db: Session,
    league_id: str,
    franchise_id: str,
    player_ids: set[str],
    board_by_id: dict[str, dict[str, Any]],
    projections: dict[str, dict[str, Any]],
    lineup: dict[str, Any],
    order: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return every player plus the lineup role used by the projection model."""
    aliases = {"K": "PK", "DST": "DEF", "D/ST": "DEF", "D": "DEF"}
    player_models = (
        {
            player.id: player
            for player in db.scalars(select(Player).where(Player.id.in_(player_ids)))
        }
        if player_ids
        else {}
    )
    assignment_status = {
        assignment.player_id: assignment.status
        for assignment in db.scalars(
            select(RosterAssignment).where(
                RosterAssignment.league_id == league_id,
                RosterAssignment.franchise_id == franchise_id,
            )
        )
    }
    draft_pick = {
        str(slot["player_id"]): int(slot["overall_pick"])
        for slot in order
        if slot.get("completed")
        and slot.get("franchise_id") == franchise_id
        and slot.get("player_id")
        and slot.get("overall_pick")
    }
    rows: list[dict[str, Any]] = []
    for player_id in player_ids:
        board = board_by_id.get(player_id, {})
        projection = projections.get(player_id, {})
        player = player_models.get(player_id)
        position = str(board.get("position") or (player.position if player else "—")).upper()
        rows.append(
            {
                "player_id": player_id,
                "player_name": board.get("player_name") or (player.name if player else player_id),
                "position": aliases.get(position, position),
                "nfl_team": board.get("nfl_team") or (player.nfl_team if player else None),
                "consensus_rank": board.get("consensus_rank"),
                "league_adjusted_rank": board.get("league_adjusted_rank"),
                "median": projection.get("median"),
                "floor": projection.get("floor"),
                "ceiling": projection.get("ceiling"),
                "confidence": projection.get("confidence"),
                "injury_risk": projection.get("injury_risk"),
                "injury_status": player.injury_status if player else None,
                "rookie": bool(player.rookie) if player else False,
                "roster_status": assignment_status.get(player_id, "DRAFTED"),
                "draft_pick": draft_pick.get(player_id),
            }
        )
    rows.sort(
        key=lambda row: (
            -float(row.get("median") or 0),
            int(row.get("consensus_rank") or 99999),
            str(row["player_name"]).casefold(),
        )
    )
    selected: set[str] = set()
    roles: dict[str, str] = {}
    for raw_position, raw_count in (lineup or {}).items():
        position = str(raw_position).upper()
        if position in {"FLEX", "SUPERFLEX", "QB_FLEX"}:
            continue
        try:
            count = max(0, int(raw_count or 0))
        except (TypeError, ValueError):
            continue
        eligible = {
            aliases.get(item.strip(), item.strip())
            for item in re.split(r"[,|+]", position)
            if item.strip()
        }
        label = aliases.get(position, position)
        candidates = [
            row for row in rows if row["player_id"] not in selected and row["position"] in eligible
        ]
        for row in candidates[:count]:
            selected.add(row["player_id"])
            roles[row["player_id"]] = label
    for label, eligible in (
        ("SUPERFLEX", {"QB", "RB", "WR", "TE"}),
        ("QB_FLEX", {"QB", "RB", "WR", "TE"}),
        ("FLEX", {"RB", "WR", "TE"}),
    ):
        try:
            count = max(0, int((lineup or {}).get(label, 0) or 0))
        except (TypeError, ValueError):
            count = 0
        candidates = [
            row for row in rows if row["player_id"] not in selected and row["position"] in eligible
        ]
        for row in candidates[:count]:
            selected.add(row["player_id"])
            roles[row["player_id"]] = label
    for row in rows:
        starter_role = roles.get(row["player_id"])
        row["lineup_role"] = f"Starter · {starter_role}" if starter_role else "Bench"
        row["is_starter"] = bool(starter_role)
    rows.sort(
        key=lambda row: (
            not row["is_starter"],
            str(row["lineup_role"]),
            -float(row.get("median") or 0),
            str(row["player_name"]).casefold(),
        )
    )
    return rows


def build_draft_analysis(
    db: Session,
    league_id: str,
    franchise_id: str | None = None,
    *,
    what_if_overall_pick: int | None = None,
    alternative_player_id: str | None = None,
) -> dict[str, Any]:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    state = draft_state(db, league_id, franchise_id, include_intelligence=False)
    order = list(state.get("draft_order") or [])
    board = draftable_consensus(db, league_id)
    board_by_id = {str(row["player_id"]): row for row in board}
    projections = build_projection_board(db, league, board)
    franchises = list(
        db.scalars(
            select(Franchise).where(Franchise.league_id == league_id).order_by(Franchise.name)
        )
    )
    ownership = _ownership(db, league_id, order)
    standings = _standings(franchises, ownership, board_by_id, projections, league.lineup_json)
    grades = _position_grades(franchises, standings)
    for row in standings:
        row["position_grades"] = grades.get(row["franchise_id"], [])
        weaknesses = [
            item["position"] for item in row["position_grades"] if item["grade"] in {"D", "F"}
        ]
        weaknesses.extend((row.get("missing_starters") or {}).keys())
        row["roster_weaknesses"] = list(dict.fromkeys(weaknesses))

    pick_values: list[dict[str, Any]] = []
    for slot in order:
        if not slot.get("completed") or not slot.get("player_id"):
            continue
        row = board_by_id.get(str(slot["player_id"]), {})
        basis = row.get("adp") or row.get("consensus_rank")
        if basis is None:
            continue
        try:
            delta = float(slot.get("overall_pick") or 0) - float(basis)
        except (TypeError, ValueError):
            continue
        label = (
            "Steal"
            if delta >= 8
            else "Value"
            if delta >= 3
            else "Reach"
            if delta <= -8
            else "Slight reach"
            if delta <= -3
            else "At market"
        )
        pick_values.append(
            {
                "overall_pick": slot.get("overall_pick"),
                "player_id": slot.get("player_id"),
                "player_name": slot.get("player_name"),
                "franchise_id": slot.get("franchise_id"),
                "franchise_name": slot.get("franchise_name"),
                "market_delta": round(delta, 1),
                "label": label,
            }
        )

    selected_team = next((row for row in standings if row["franchise_id"] == franchise_id), None)
    if selected_team is not None and franchise_id is not None:
        selected_team = dict(selected_team)
        selected_team["roster_players"] = _roster_breakdown(
            db,
            league_id,
            franchise_id,
            ownership.get(franchise_id, set()),
            board_by_id,
            projections,
            league.lineup_json,
            order,
        )
    selected_picks = [
        slot
        for slot in order
        if slot.get("completed")
        and slot.get("franchise_id") == franchise_id
        and slot.get("player_id")
    ]
    completed_at = {
        str(slot["player_id"]): int(slot["overall_pick"])
        for slot in order
        if slot.get("completed") and slot.get("player_id") and slot.get("overall_pick")
    }
    alternatives = [
        {
            "player_id": row["player_id"],
            "player_name": row["player_name"],
            "position": row["position"],
            "consensus_rank": row.get("consensus_rank"),
            "projection": projections.get(row["player_id"]),
        }
        for row in board[:120]
    ]
    counterfactual = None
    if what_if_overall_pick and alternative_player_id and franchise_id:
        original_slot = next(
            (
                slot
                for slot in selected_picks
                if int(slot.get("overall_pick") or 0) == what_if_overall_pick
            ),
            None,
        )
        alternative_pick = completed_at.get(alternative_player_id)
        if original_slot and (alternative_pick is None or alternative_pick > what_if_overall_pick):
            changed = {team_id: set(player_ids) for team_id, player_ids in ownership.items()}
            original_id = str(original_slot["player_id"])
            changed.setdefault(franchise_id, set()).discard(original_id)
            for team_id in changed:
                if team_id != franchise_id:
                    changed[team_id].discard(alternative_player_id)
            changed.setdefault(franchise_id, set()).add(alternative_player_id)
            replay = _standings(franchises, changed, board_by_id, projections, league.lineup_json)
            original_team = next(row for row in standings if row["franchise_id"] == franchise_id)
            replay_team = next(row for row in replay if row["franchise_id"] == franchise_id)
            alternative = board_by_id.get(alternative_player_id, {})
            counterfactual = {
                "original_player_id": original_id,
                "original_player_name": original_slot.get("player_name") or original_id,
                "alternative_player_id": alternative_player_id,
                "alternative_player_name": alternative.get("player_name") or alternative_player_id,
                "roster_strength_delta": round(
                    replay_team["roster_strength"] - original_team["roster_strength"], 1
                ),
                "projected_rank_before": original_team["projected_rank"],
                "projected_rank_after": replay_team["projected_rank"],
                "starter_points_delta": round(
                    replay_team["projected_starter_points"]
                    - original_team["projected_starter_points"],
                    1,
                ),
                "method": (
                    "Read-only replay; it does not alter the real draft or send anything to MFL."
                ),
            }

    return {
        "league_id": league_id,
        "methodology": (
            "League-scored season medians drive legal starting-lineup totals; bench depth "
            "contributes 12%. Win and playoff values are relative strength indexes, not "
            "sportsbook odds."
        ),
        "projection_model": "season-outcomes-v1",
        "projected_standings": standings,
        "selected_team": selected_team,
        "pick_values": pick_values,
        "what_if": {
            "eligible_picks": [
                {
                    "overall_pick": slot.get("overall_pick"),
                    "player_id": slot.get("player_id"),
                    "player_name": slot.get("player_name"),
                }
                for slot in selected_picks
            ],
            "alternatives": alternatives,
            "result": counterfactual,
        },
    }
