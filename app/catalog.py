from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auction import franchise_budget
from app.consensus import build_consensus
from app.models import (
    Franchise,
    KeeperSelection,
    League,
    Player,
    PlayerIdentity,
    RosterAssignment,
    SourcePlayerValue,
)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


POSITION_ORDER = {
    position: index for index, position in enumerate(("QB", "RB", "WR", "TE", "PK", "DEF"))
}
POSITION_ALIASES = {
    "D": "DEF",
    "DEF": "DEF",
    "DEFENSE": "DEF",
    "DST": "DEF",
    "D/ST": "DEF",
    "K": "PK",
    "PK": "PK",
}


def draftable_positions(db: Session, league_id: str) -> set[str]:
    league = db.scalar(select(League).where(League.id == league_id).order_by(League.season.desc()))
    lineup = league.lineup_json if league and isinstance(league.lineup_json, dict) else {}
    allowed: set[str] = set()
    for raw_position, count in lineup.items():
        try:
            if int(count) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        position = str(raw_position).strip().upper()
        if position == "FLEX":
            allowed.update({"RB", "WR", "TE"})
            continue
        if position == "SUPERFLEX":
            allowed.update({"QB", "RB", "WR", "TE"})
            continue
        if position in POSITION_ALIASES:
            allowed.add(POSITION_ALIASES[position])
            continue
        for item in re.split(r"[,+|]", position):
            normalized = POSITION_ALIASES.get(item.strip(), item.strip())
            if normalized:
                allowed.add(normalized)
    return allowed or {"QB", "RB", "WR", "TE"}


def _is_draftable(row: dict[str, Any], allowed: set[str]) -> bool:
    positions = {
        POSITION_ALIASES.get(str(position).upper(), str(position).upper())
        for position in row["fantasy_positions"]
    }
    return bool(positions & allowed)


def draftable_consensus(db: Session, league_id: str) -> list[dict[str, Any]]:
    allowed = draftable_positions(db, league_id)
    rows = [row for row in build_consensus(db, league_id) if _is_draftable(row, allowed)]
    league_ranked = sorted(
        (row for row in rows if row["league_adjusted_rank"] is not None),
        key=lambda row: (row["league_adjusted_rank"], row["player_id"]),
    )
    for index, row in enumerate(league_ranked, 1):
        row["league_adjusted_rank"] = index
    for index, row in enumerate(rows, 1):
        row["consensus_rank"] = index
    return rows


def player_filters(db: Session, league_id: str) -> dict[str, Any]:
    rows = draftable_consensus(db, league_id)
    owners = list(
        db.execute(
            select(Franchise.id, Franchise.name)
            .where(Franchise.league_id == league_id)
            .order_by(Franchise.name, Franchise.id)
        )
    )
    return {
        "positions": sorted(
            {
                POSITION_ALIASES.get(str(position).upper(), str(position).upper())
                for row in rows
                for position in row["fantasy_positions"]
                if position
            },
            key=lambda item: (POSITION_ORDER.get(item, 99), item),
        ),
        "nfl_teams": sorted({row["nfl_team"] for row in rows if row["nfl_team"]}),
        "statuses": sorted({row["status"] for row in rows if row["status"]}),
        "injury_statuses": sorted({row["injury_status"] for row in rows if row["injury_status"]}),
        "tiers": sorted({row["tier"] for row in rows if row["tier"] is not None}),
        "franchises": [{"id": item.id, "name": item.name} for item in owners],
    }


def query_players(
    db: Session,
    league_id: str,
    *,
    page: int = 1,
    per_page: int = 100,
    search: str | None = None,
    position: str | None = None,
    nfl_team: str | None = None,
    availability: str = "all",
    owner: str | None = None,
    rookie: bool | None = None,
    injury_status: str | None = None,
    status: str | None = None,
    tier: int | None = None,
    bye_week: int | None = None,
    min_source_count: int | None = None,
    min_adp: float | None = None,
    max_adp: float | None = None,
    tag: str | None = None,
    sort: str = "consensus_rank",
    direction: str = "asc",
) -> dict[str, Any]:
    rows = draftable_consensus(db, league_id)
    needle = (search or "").casefold().strip()
    selected_position = (position or "").upper()
    selected_team = (nfl_team or "").upper()

    def includes(row: dict[str, Any]) -> bool:
        preference = row["preference"]
        if needle and needle not in f"{row['player_name']} {row['nfl_team'] or ''}".casefold():
            return False
        if selected_position and selected_position not in row["fantasy_positions"]:
            return False
        if selected_team and (row["nfl_team"] or "").upper() != selected_team:
            return False
        if owner and row["rostered_by"] != owner:
            return False
        if rookie is not None and bool(row["rookie"]) != rookie:
            return False
        if injury_status and row["injury_status"] != injury_status:
            return False
        if status and row["status"] != status:
            return False
        if tier is not None and row["tier"] != tier:
            return False
        if bye_week is not None and row["bye_week"] != bye_week:
            return False
        if min_source_count is not None and row["source_count"] < min_source_count:
            return False
        adp = _number(row["adp"])
        if min_adp is not None and (adp is None or adp < min_adp):
            return False
        if max_adp is not None and (adp is None or adp > max_adp):
            return False
        if availability == "available" and not row["available"]:
            return False
        if availability == "rostered" and not row["rostered_by"]:
            return False
        if availability == "keeper" and not row["keeper"]:
            return False
        if availability == "drafted" and not row["drafted"]:
            return False
        if tag == "target" and not preference["target"]:
            return False
        if tag == "fade" and not preference["fade"]:
            return False
        if tag == "queued" and preference["queue_order"] is None:
            return False
        if tag == "do_not_draft" and not preference["do_not_draft"]:
            return False
        return True

    filtered = [row for row in rows if includes(row)]
    numeric_desc = {
        "custom_score",
        "value_over_replacement",
        "live_auction_value",
        "source_count",
    }
    allowed = {
        "consensus_rank",
        "league_adjusted_rank",
        "player_name",
        "position",
        "nfl_team",
        "tier",
        "adp",
        "mfl_aav",
        *numeric_desc,
    }
    sort = sort if sort in allowed else "consensus_rank"

    def key(row: dict[str, Any]) -> tuple[Any, ...]:
        value = row.get(sort)
        if sort in numeric_desc or sort in {
            "consensus_rank",
            "league_adjusted_rank",
            "tier",
            "adp",
            "mfl_aav",
        }:
            number = _number(value)
            return (
                number is None,
                number if number is not None else float("inf"),
                row["player_id"],
            )
        return (value is None, str(value or "").casefold(), row["player_id"])

    filtered.sort(key=key, reverse=direction == "desc")
    total = len(filtered)
    start = (page - 1) * per_page
    return {
        "items": filtered[start : start + per_page],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": max(1, (total + per_page - 1) // per_page),
        },
        "freshness": {
            "latest": max(
                (row["data_updated_at"] for row in filtered if row["data_updated_at"]),
                default=None,
            )
        },
    }


def roster_overview(db: Session, league_id: str) -> dict[str, Any]:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise ValueError("League not found")
    board = {row["player_id"]: row for row in build_consensus(db, league_id)}
    keeper_map = {
        item.player_id: item
        for item in db.scalars(
            select(KeeperSelection).where(KeeperSelection.league_id == league_id)
        )
    }
    assignments = list(
        db.scalars(
            select(RosterAssignment)
            .where(RosterAssignment.league_id == league_id)
            .order_by(
                RosterAssignment.franchise_id, RosterAssignment.status, RosterAssignment.player_id
            )
        )
    )
    by_franchise: dict[str, list[RosterAssignment]] = {}
    for assignment in assignments:
        by_franchise.setdefault(assignment.franchise_id, []).append(assignment)
    teams: list[dict[str, Any]] = []
    table: list[dict[str, Any]] = []
    for franchise in db.scalars(
        select(Franchise).where(Franchise.league_id == league_id).order_by(Franchise.name)
    ):
        position_counts: dict[str, int] = {}
        players: list[dict[str, Any]] = []
        strength = Decimal("0")
        for assignment in by_franchise.get(franchise.id, []):
            row = board.get(assignment.player_id)
            player = db.get(Player, assignment.player_id)
            if player is None:
                continue
            position_counts[player.position] = position_counts.get(player.position, 0) + 1
            keeper = keeper_map.get(player.id)
            strength += Decimal(str((row or {}).get("custom_score") or 0))
            item = {
                "franchise_id": franchise.id,
                "franchise_name": franchise.name,
                "player_id": player.id,
                "player_name": player.name,
                "position": player.position,
                "nfl_team": player.nfl_team,
                "status": assignment.status,
                "salary": str(assignment.salary) if assignment.salary is not None else None,
                "contract_info": assignment.contract_info,
                "keeper": keeper is not None,
                "keeper_cost": str(keeper.keeper_cost)
                if keeper and keeper.keeper_cost is not None
                else None,
                "league_adjusted_rank": (row or {}).get("league_adjusted_rank"),
            }
            players.append(item)
            table.append(item)
        needs = {
            position: max(0, int(required or 0) - position_counts.get(position, 0))
            for position, required in league.lineup_json.items()
            if position not in {"FLEX", "SUPERFLEX"}
        }
        budget = franchise_budget(db, league, franchise)
        teams.append(
            {
                **budget,
                "players": players,
                "position_counts": position_counts,
                "needs": needs,
                "roster_strength": str(strength.quantize(Decimal("0.01"))),
            }
        )
    return {"league_id": league_id, "teams": teams, "table": table, "synced_at": league.synced_at}


def player_detail(db: Session, league_id: str, player_id: str) -> dict[str, Any] | None:
    player = db.get(Player, player_id)
    if player is None:
        return None
    board = {row["player_id"]: row for row in build_consensus(db, league_id)}
    row = board.get(player_id, {})
    identity = db.scalar(select(PlayerIdentity).where(PlayerIdentity.player_id == player_id))
    raw_values = list(
        db.scalars(
            select(SourcePlayerValue)
            .where(SourcePlayerValue.player_id == player_id)
            .order_by(SourcePlayerValue.fetched_at.desc())
            .limit(100)
        )
    )
    return {
        **row,
        "identity": {
            "mfl_id": identity.mfl_id if identity else player.id,
            "gsis_id": identity.gsis_id if identity else None,
            "sleeper_id": identity.sleeper_id if identity else None,
            "espn_id": identity.espn_id if identity else None,
            "match_method": identity.match_method if identity else None,
            "match_confidence": str(identity.match_confidence) if identity else None,
            "verified": identity.verified if identity else False,
        },
        "metadata": player.metadata_json or {},
        "source_values": [
            {
                "source_id": value.source_id,
                "value_type": value.value_type,
                "raw_value": value.raw_value_json,
                "normalized_value": str(value.normalized_value)
                if value.normalized_value is not None
                else None,
                "source_updated_at": value.source_updated_at,
                "fetched_at": value.fetched_at,
                "snapshot_id": value.snapshot_id,
            }
            for value in raw_values
        ],
    }
