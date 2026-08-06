from __future__ import annotations

import math
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auction import franchise_budget
from app.consensus import build_consensus
from app.models import (
    Franchise,
    KeeperSelection,
    League,
    LeagueType,
    Player,
    PlayerIdentity,
    RosterAssignment,
    SourcePlayerValue,
)
from app.users import effective_auction_strategy


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
FANTASYPROS_DEFENSE_SLUGS = {
    "ARI": "arizona-defense",
    "ATL": "atlanta-defense",
    "BAL": "baltimore-defense",
    "BUF": "buffalo-defense",
    "CAR": "carolina-defense",
    "CHI": "chicago-defense",
    "CIN": "cincinnati-defense",
    "CLE": "cleveland-defense",
    "DAL": "dallas-defense",
    "DEN": "denver-defense",
    "DET": "detroit-defense",
    "GBP": "green-bay-defense",
    "HOU": "houston-defense",
    "IND": "indianapolis-defense",
    "JAC": "jacksonville-defense",
    "KCC": "kansas-city-defense",
    "LAC": "los-angeles-chargers-defense",
    "LAR": "los-angeles-rams-defense",
    "LVR": "las-vegas-defense",
    "MIA": "miami-defense",
    "MIN": "minnesota-defense",
    "NEP": "new-england-defense",
    "NOS": "new-orleans-defense",
    "NYG": "new-york-giants-defense",
    "NYJ": "new-york-jets-defense",
    "PHI": "philadelphia-defense",
    "PIT": "pittsburgh-defense",
    "SEA": "seattle-defense",
    "SFO": "san-francisco-defense",
    "TBB": "tampa-bay-defense",
    "TEN": "tennessee-defense",
    "WAS": "washington-defense",
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


def draftable_consensus(
    db: Session,
    league_id: str,
    source_overrides: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    allowed = draftable_positions(db, league_id)
    rows = [
        row
        for row in build_consensus(db, league_id, source_overrides)
        if _is_draftable(row, allowed)
    ]
    league_ranked = sorted(
        (row for row in rows if row["league_adjusted_rank"] is not None),
        key=lambda row: (row["league_adjusted_rank"], row["player_id"]),
    )
    for index, row in enumerate(league_ranked, 1):
        row["league_adjusted_rank"] = index
    for index, row in enumerate(rows, 1):
        row["consensus_rank"] = index
    position_counts: dict[str, int] = {}
    for row in rows:
        position = row["position"].upper()
        position_counts[position] = position_counts.get(position, 0) + 1
        row["weekly_model_position_rank"] = row.get("position_rank")
        row["consensus_position_rank"] = position_counts[position]
    defense_index = 0
    for row in rows:
        rank = int(row["consensus_rank"])
        calculated_tier = (
            1 if rank <= 24 else 2 if rank <= 60 else 3 if rank <= 120 else 4 if rank <= 200 else 5
        )
        fantasypros_tier = _number(row.get("fantasypros_tier"))
        if fantasypros_tier is not None:
            calculated_tier = min(calculated_tier, max(1, int(fantasypros_tier)))
        manual_tier = row["preference"].get("manual_tier")
        tier_source = "manual" if manual_tier is not None else "consensus"
        tier = int(manual_tier) if manual_tier is not None else calculated_tier
        if row["position"].upper() == "DEF":
            defense_index += 1
            tier = max(6, tier, 6 + min(3, (defense_index - 1) // 8))
            tier_source = "defense policy" if manual_tier is None else "manual + defense floor"
        row["tier"] = tier
        row["tier_source"] = tier_source
    _apply_auction_values(db, league_id, rows)
    return rows


def _round_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value.quantize(Decimal("0.01"), ROUND_HALF_UP)
    units = (value / increment).quantize(Decimal("1"), ROUND_HALF_UP)
    return (units * increment).quantize(Decimal("0.01"), ROUND_HALF_UP)


def _allocate_auction_pool(
    candidates: list[dict[str, Any]],
    *,
    minimum: Decimal,
    spending_pool: Decimal,
    roster_slots: int,
    legal_ceiling: Decimal,
) -> dict[str, Decimal]:
    selected = candidates[:roster_slots]
    allocatable = max(Decimal("0"), spending_pool - minimum * Decimal(roster_slots))
    weights = [Decimal(str(math.exp(-0.0275 * index))) for index in range(len(selected))]
    total_weight = sum(weights, Decimal("0"))
    values: dict[str, Decimal] = {}
    for row, weight in zip(selected, weights, strict=True):
        extra = allocatable * weight / total_weight if total_weight else Decimal("0")
        values[row["player_id"]] = min(legal_ceiling, _round_to_increment(minimum + extra, minimum))
    return values


def _apply_auction_values(db: Session, league_id: str, rows: list[dict[str, Any]]) -> None:
    league = db.scalar(select(League).where(League.id == league_id).order_by(League.season.desc()))
    if league is None or league.starting_budget is None:
        return
    franchises = list(db.scalars(select(Franchise).where(Franchise.league_id == league_id)))
    budgets = [franchise_budget(db, league, franchise) for franchise in franchises]
    if not budgets:
        return
    minimum = Decimal(league.minimum_bid)
    remaining_budget = sum((Decimal(item["remaining"]) for item in budgets), Decimal("0"))
    open_slots = sum(int(item["slots_remaining"]) for item in budgets)
    legal_ceiling = max((Decimal(item["maximum_bid"]) for item in budgets), default=minimum)
    baseline_budget = sum((Decimal(item.starting_budget) for item in franchises), Decimal("0"))
    baseline_slots = sum(item.roster_slots for item in franchises)
    baseline_ceiling = max(
        (
            Decimal(item.starting_budget) - minimum * Decimal(max(0, item.roster_slots - 1))
            for item in franchises
        ),
        default=minimum,
    )
    strategy = effective_auction_strategy(db, league_id)
    priorities = {position: index for index, position in enumerate(strategy["priority_order"])}

    def strategy_key(row: dict[str, Any]) -> tuple[float, int, int]:
        rank = int(row.get("consensus_rank") or 99999)
        star_bonus = (
            float(strategy["star_emphasis"]) if rank <= 36 else float(strategy["depth_emphasis"])
        )
        position_priority = priorities.get(row["position"].upper(), 99)
        position_bonus = max(0.78, 1.12 - min(position_priority, 5) * 0.07)
        return (rank / max(star_bonus * position_bonus, 0.01), position_priority, rank)

    all_candidates = sorted(
        (row for row in rows if row["position"].upper() != "DEF"), key=strategy_key
    )
    available_candidates = [row for row in all_candidates if row["available"]]
    baseline_values = _allocate_auction_pool(
        all_candidates,
        minimum=minimum,
        spending_pool=baseline_budget,
        roster_slots=baseline_slots,
        legal_ceiling=baseline_ceiling,
    )
    dynamic_values = _allocate_auction_pool(
        available_candidates,
        minimum=minimum,
        spending_pool=remaining_budget,
        roster_slots=open_slots,
        legal_ceiling=legal_ceiling,
    )
    for row in rows:
        baseline = baseline_values.get(row["player_id"], minimum)
        dynamic = dynamic_values.get(row["player_id"]) if row["available"] else None
        if row["position"].upper() == "DEF" and row["available"]:
            dynamic = minimum
        max_bid = None
        if dynamic is not None:
            max_bid = min(
                legal_ceiling,
                _round_to_increment(max(baseline, dynamic) * Decimal("1.15") + minimum, minimum),
            )
        row["baseline_auction_value"] = str(baseline.quantize(Decimal("0.01")))
        row["suggested_auction_value"] = str(baseline.quantize(Decimal("0.01")))
        row["dynamic_bid"] = str(dynamic.quantize(Decimal("0.01"))) if dynamic is not None else None
        row["live_auction_value"] = row["dynamic_bid"]
        row["max_recommended_bid"] = (
            str(max_bid.quantize(Decimal("0.01"))) if max_bid is not None else None
        )
        row["league_maximum_bid"] = str(legal_ceiling.quantize(Decimal("0.01")))


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
        if tag == "sleeper" and "sleeper" not in preference.get("tags", []):
            return False
        return True

    filtered = [row for row in rows if includes(row)]
    numeric_desc = {
        "custom_score",
        "value_over_replacement",
        "live_auction_value",
        "suggested_auction_value",
        "max_recommended_bid",
        "dynamic_bid",
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
        team = {
            **budget,
            "players": players,
            "position_counts": position_counts,
            "needs": needs,
            "roster_strength": str(strength.quantize(Decimal("0.01"))),
        }
        if league.league_type == LeagueType.KEEPER:
            team.pop("maximum_bid", None)
        teams.append(team)
    return {
        "league_id": league_id,
        "league_type": league.league_type,
        "teams": teams,
        "table": table,
        "synced_at": league.synced_at,
    }


def player_detail(db: Session, league_id: str, player_id: str) -> dict[str, Any] | None:
    player = db.get(Player, player_id)
    if player is None:
        return None
    board = {row["player_id"]: row for row in draftable_consensus(db, league_id)}
    row = board.get(player_id, {})
    identity = db.scalar(select(PlayerIdentity).where(PlayerIdentity.player_id == player_id))
    raw_values = list(
        db.scalars(
            select(SourcePlayerValue)
            .where(
                SourcePlayerValue.player_id == player_id,
                or_(
                    SourcePlayerValue.league_id == league_id,
                    SourcePlayerValue.league_id.is_(None),
                ),
            )
            .order_by(SourcePlayerValue.fetched_at.desc())
            .limit(100)
        )
    )
    latest_values: list[SourcePlayerValue] = []
    seen_values: set[tuple[str, str]] = set()
    for value in raw_values:
        key = (value.source_id, value.value_type)
        if key not in seen_values:
            seen_values.add(key)
            latest_values.append(value)
    fantasypros = next(
        (value.raw_value_json or {} for value in latest_values if value.source_id == "fantasypros"),
        {},
    )
    gng = next(
        (value.raw_value_json or {} for value in latest_values if value.source_id == "gng"),
        {},
    )
    schedule = next(
        (
            value.raw_value_json or {}
            for value in latest_values
            if value.source_id == "nflverse" and value.value_type == "schedule"
        ),
        {},
    )
    league = db.scalar(select(League).where(League.id == league_id).order_by(League.season.desc()))
    metadata = player.metadata_json or {}
    is_team_defense = player.position.upper() in {"DEF", "DST", "D/ST"}
    profile_url = fantasypros.get("player_page_url")
    guessed_profile = not bool(profile_url)
    if is_team_defense:
        defense_slug = FANTASYPROS_DEFENSE_SLUGS.get(str(player.nfl_team or "").upper())
        if defense_slug:
            profile_url = f"https://www.fantasypros.com/nfl/news/{defense_slug}.php"
            guessed_profile = False
    elif not profile_url:
        profile_url = f"https://www.fantasypros.com/nfl/players/{_profile_slug(player.name)}.php"
    external_links = [
        {
            "label": "FantasyPros defense news" if is_team_defense else "FantasyPros profile",
            "url": profile_url,
            "guessed": guessed_profile,
        }
    ]
    if isinstance(gng.get("source_url"), str):
        external_links.append(
            {"label": "The GNG ranking", "url": gng["source_url"], "guessed": False}
        )
    if identity and identity.espn_id:
        external_links.append(
            {
                "label": "ESPN profile",
                "url": f"https://www.espn.com/nfl/player/_/id/{identity.espn_id}",
                "guessed": False,
            }
        )
    if league:
        base_url = str(
            (league.settings_json or {}).get("baseURL", "https://www.myfantasyleague.com")
        )
        external_links.append(
            {
                "label": "MFL player page",
                "url": f"{base_url}/{league.season}/player?L={league_id}&P={player.id}",
                "guessed": False,
            }
        )
    flags = []
    if row.get("rookie"):
        flags.append("Rookie")
    if row.get("injury_status"):
        flags.append(str(row["injury_status"]))
    if row.get("high_disagreement"):
        flags.append("Rank disagreement")
    if row.get("source_confidence") == "low":
        flags.append("Limited source coverage")
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
        "metadata": metadata,
        "profile": {
            "headshot_url": (metadata.get("nflverse") or {}).get("headshot"),
            "flags": flags,
            "external_links": external_links,
            "fantasypros": {
                key: fantasypros.get(key)
                for key in (
                    "rank_ecr",
                    "rank_min",
                    "rank_max",
                    "pos_rank",
                    "tier",
                    "player_owned_avg",
                    "scoring",
                )
                if fantasypros.get(key) is not None
            },
            "gng": {
                key: gng.get(key)
                for key in (
                    "rank",
                    "position_rank",
                    "tier",
                    "projected_ppg",
                    "verdict",
                )
                if gng.get(key) is not None
            },
            "schedule": schedule,
        },
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
            for value in latest_values
        ],
    }


def _profile_slug(name: str) -> str:
    value = name.strip()
    if "," in value:
        last, first = value.split(",", 1)
        value = f"{first.strip()} {last.strip()}"
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
