from __future__ import annotations

import math
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auction import franchise_budget
from app.consensus import build_consensus
from app.models import (
    DataSource,
    Franchise,
    KeeperSelection,
    League,
    LeagueType,
    Player,
    PlayerIdentity,
    RosterAssignment,
    SourcePlayerValue,
)
from app.projections import build_projection_board
from app.users import effective_auction_strategy


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _visible_stat_value(value: Any) -> bool:
    if value in (None, ""):
        return False
    try:
        return Decimal(str(value).replace(",", "")) != 0
    except Exception:  # noqa: BLE001 - textual nflverse fields should remain visible
        return True


def _without_zero_stats(snapshot: dict[str, Any]) -> dict[str, Any]:
    stats = snapshot.get("stats")
    if not isinstance(stats, dict):
        return snapshot
    return {
        **snapshot,
        "stats": {key: value for key, value in stats.items() if _visible_stat_value(value)},
    }


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


def _fantasypros_profile_slug(name: str) -> str:
    value = name.strip()
    if "," in value:
        last, first = value.split(",", 1)
        value = f"{first.strip()} {last.strip()}"
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


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
    league = db.scalar(select(League).where(League.id == league_id).order_by(League.season.desc()))
    stable_keeper_tiers = bool(league and league.league_type == LeagueType.KEEPER)
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
        source_tier = _number(row.get("source_tier"))
        if source_tier is not None:
            calculated_tier = min(calculated_tier, max(1, int(source_tier)))
        manual_tier = row["preference"].get("manual_tier")
        initial_tier = _number(row.get("tier"))
        if manual_tier is not None:
            tier = int(manual_tier)
            tier_source = "manual"
        elif stable_keeper_tiers and initial_tier is not None:
            tier = int(initial_tier)
            tier_source = "initial synced board"
        else:
            tier = calculated_tier
            tier_source = "consensus"
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
        if owner and row["owner_id"] != owner:
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
    source_ids = {value.source_id for value in latest_values} | set(row.get("source_ranks") or {})
    sources = {
        source.id: source
        for source in db.scalars(select(DataSource).where(DataSource.id.in_(source_ids)))
    }
    gng = next(
        (value.raw_value_json or {} for value in latest_values if value.source_id == "gng"),
        {},
    )
    fantasysharks_value = db.scalar(
        select(SourcePlayerValue)
        .where(
            SourcePlayerValue.player_id == player_id,
            SourcePlayerValue.source_id == "fantasysharks_dynasty_csv",
            SourcePlayerValue.value_type == "rank",
        )
        .order_by(SourcePlayerValue.fetched_at.desc(), SourcePlayerValue.id.desc())
        .limit(1)
    )
    fantasysharks = fantasysharks_value.raw_value_json or {} if fantasysharks_value else {}
    schedule = next(
        (
            value.raw_value_json or {}
            for value in latest_values
            if value.source_id == "nflverse" and value.value_type == "schedule"
        ),
        {},
    )
    player_stats = next(
        (
            value.raw_value_json or {}
            for value in latest_values
            if value.source_id == "nflverse" and value.value_type == "player_stats"
        ),
        {},
    )
    player_stats = _without_zero_stats(player_stats)
    depth_chart = next(
        (
            value.raw_value_json or {}
            for value in latest_values
            if value.value_type == "depth_chart"
        ),
        {},
    )
    league = db.scalar(select(League).where(League.id == league_id).order_by(League.season.desc()))
    season_projection = (
        build_projection_board(db, league, list(board.values())).get(player_id, {})
        if league
        else {}
    )
    metadata = player.metadata_json or {}
    external_links: list[dict[str, Any]] = []
    is_team_defense = player.position.upper() in {"DEF", "DST", "D/ST"}
    defense_slug = FANTASYPROS_DEFENSE_SLUGS.get(str(player.nfl_team or "").upper())
    if is_team_defense and defense_slug:
        external_links.append(
            {
                "label": "FantasyPros defense news",
                "url": f"https://www.fantasypros.com/nfl/news/{defense_slug}.php",
                "guessed": False,
            }
        )
    elif not is_team_defense:
        external_links.append(
            {
                "label": "FantasyPros profile",
                "url": (
                    "https://www.fantasypros.com/nfl/players/"
                    f"{_fantasypros_profile_slug(player.name)}.php"
                ),
                "guessed": True,
            }
        )
    if isinstance(gng.get("source_url"), str):
        external_links.append(
            {"label": "The GNG ranking", "url": gng["source_url"], "guessed": False}
        )
    fantasysharks_id = str(
        fantasysharks.get("FantasySharks PID") or fantasysharks.get("fantasysharks_pid") or ""
    ).strip()
    fantasysharks_url = str(
        fantasysharks.get("Player Page") or fantasysharks.get("player_page") or ""
    ).strip()
    if not fantasysharks_url and fantasysharks_id.isdigit():
        fantasysharks_url = (
            f"https://www.fantasysharks.com/players/playerpage.php?PID={fantasysharks_id}"
        )
    parsed_fantasysharks = urlparse(fantasysharks_url)
    if parsed_fantasysharks.scheme == "https" and parsed_fantasysharks.hostname in {
        "fantasysharks.com",
        "www.fantasysharks.com",
    }:
        external_links.append(
            {
                "label": "FantasySharks profile",
                "url": fantasysharks_url,
                "guessed": False,
            }
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
    preference = row.get("preference") or {}
    if preference.get("target"):
        flags.append("Target")
    if preference.get("fade"):
        flags.append("Fade")
    if "sleeper" in (preference.get("tags") or []):
        flags.append("Sleeper")
    source_rank_details = []
    latest_lookup = {(value.source_id, value.value_type): value for value in latest_values}
    signal_descriptions = {
        "league_model": (
            "League model rank",
            "Overall rank from this league's scoring, lineup, and value-over-replacement model.",
        ),
        "mfl_rank": ("MFL rank", "Overall player rank supplied by MyFantasyLeague."),
        "mfl_adp": ("MFL ADP", "Average MFL draft position; lower means selected earlier."),
        "mfl_projection": (
            "Projection rank",
            "Rank derived from MFL's current weekly projected points for this league.",
        ),
        "mfl_aav": ("Auction-value rank", "Rank derived from MFL average auction values."),
        "sleeper": (
            "Add-trend rank",
            "Recent Sleeper add-trend rank; lower means the player is being added more often.",
        ),
        "nflverse": (
            "Schedule rank",
            "Small supporting schedule signal; 1 is the easiest schedule.",
        ),
    }
    for source_id, rank in (row.get("source_ranks") or {}).items():
        source = sources.get(source_id)
        rank_value = latest_lookup.get((source_id, "rank"))
        signal_label, meaning = signal_descriptions.get(
            source_id,
            ("Published overall rank", "Published overall player rank; lower is better."),
        )
        source_rank_details.append(
            {
                "source_id": source_id,
                "source_name": source.name if source else source_id,
                "rank": str(rank),
                "signal_label": signal_label,
                "meaning": meaning,
                "source_updated_at": rank_value.source_updated_at if rank_value else None,
                "fetched_at": rank_value.fetched_at if rank_value else None,
                "attribution": source.attribution if source else None,
            }
        )
    source_rank_details.sort(key=lambda item: float(item["rank"]))
    sleeper_depth = metadata.get("sleeper") or {}
    if not depth_chart and (
        sleeper_depth.get("depth_chart_position") or sleeper_depth.get("depth_chart_order")
    ):
        depth_chart = {
            "depth_position": sleeper_depth.get("depth_chart_position"),
            "depth_team": sleeper_depth.get("depth_chart_order"),
            "source": "Sleeper",
        }
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
            "fantasysharks": {
                "player_id": fantasysharks_id or None,
                "player_page": fantasysharks_url or None,
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
            "depth_chart": depth_chart,
            "nerdy_stats": player_stats,
            "fantasy_value": {
                "vorp": row.get("value_over_replacement"),
                "projection_signal": row.get("projected_points"),
                "replacement_signal": row.get("replacement_points"),
                "league_value_score": row.get("custom_score"),
                "projection_note": row.get("projection_note"),
            },
            "season_projection": season_projection,
            "source_rank_details": source_rank_details,
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
