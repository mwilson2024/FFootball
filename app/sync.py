import hashlib
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.diagnostics import log_sync_warnings
from app.mfl import MFLClient, MFLError, MFLResponse
from app.models import (
    DataSource,
    Franchise,
    KeeperSelection,
    League,
    LeagueType,
    Player,
    RankingSnapshot,
    RosterAssignment,
    SyncWarning,
)
from app.rankings import RankingInput, rank_players, scoring_warnings
from app.sources import ensure_mfl_identities, initialize_sources, sync_local_ranking_sources

RULE_DESCRIPTIONS = {
    "FG": "Length of Field Goal Made",
    "FR": "Length of Offensive Fumble Recovery TD",
    "FC": "Fumble Recoveries (from Opponent)",
    "IC": "Interceptions Caught",
    "SK": "Sacked a QB",
    "SF": "Safeties",
    "TPA": "Total Points Allowed",
    "#T": "Number of Defensive & Special Teams TDs",
    "#P": "Number of Passing TDs",
    "PY": "Passing Yards",
    "IN": "Pass Interceptions Thrown",
    "P2": "Passing 2 Pointers",
    "#R": "Number of Rushing TDs",
    "RY": "Rushing Yards",
    "R2": "Rushing 2 Pointers",
    "#C": "Number of Receiving TDs",
    "CY": "Receiving Yards",
    "CC": "Receptions",
    "C2": "Receiving 2 Pointers",
    "EP": "Extra Points",
    "#UT": "Number of Punt Return TDs",
    "#KT": "Number of Kickoff Return TDs",
    "FL": "Fumbles Lost (to Opponent)",
    "#FR": "Number of Offensive Fumble Recovery TDs",
}

EXPECTED_UNAVAILABLE_EXPORTS = {
    "auctionResults": ("auction has not been setup yet",),
    "selectedKeepers": (
        "no select keepers event defined",
        "no selected keepers",
    ),
}


def _is_expected_unavailable(export_type: str, error: Exception) -> bool:
    message = str(error).lower()
    return any(
        expected in message for expected in EXPECTED_UNAVAILABLE_EXPORTS.get(export_type, ())
    )


def _warning_parts(message: str) -> tuple[str, str, str]:
    if message.startswith("Unmapped scoring rule"):
        source, category = "rules", "scoring_rule"
    elif "stale cached data" in message:
        source, category = message.split(":", 1)[0], "stale_data"
    elif ":" in message:
        source, category = message.split(":", 1)[0], "endpoint"
    else:
        source, category = "sync", "general"
    slug = re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")[:48]
    digest = hashlib.sha256(message.encode()).hexdigest()[:8]
    return source, category, f"{slug}_{digest}"


def record_sync_warnings(db: Session, league_id: str, warnings: list[str]) -> None:
    now = datetime.now(UTC)
    active = list(
        db.scalars(
            select(SyncWarning).where(
                SyncWarning.league_id == league_id,
                SyncWarning.resolved.is_(False),
            )
        )
    )
    current_messages = set(warnings)
    for item in active:
        if item.message not in current_messages:
            item.resolved = True
    for message in warnings:
        source, category, code = _warning_parts(message)
        warning_item = db.scalar(
            select(SyncWarning).where(
                SyncWarning.league_id == league_id,
                SyncWarning.source == source,
                SyncWarning.message == message,
            )
        )
        if warning_item is None:
            db.add(
                SyncWarning(
                    league_id=league_id,
                    source=source,
                    category=category,
                    code=code,
                    message=message,
                    details_json={"season": datetime.now(UTC).year},
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        else:
            warning_item.occurrences += 1
            warning_item.last_seen_at = now
            warning_item.resolved = False


def _list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _root(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key, payload)
    return value if isinstance(value, dict) else {}


def _nested_rows(
    payload: dict[str, Any], root: str, container: str, row: str
) -> list[dict[str, Any]]:
    data = _root(payload, root)
    nested = data.get(container, data)
    if isinstance(nested, dict):
        return _list(nested.get(row))
    return []


def _decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except Exception:
        return default


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("$t", value.get("value", "")))
    return "" if value is None else str(value)


def _lineup(league_data: dict[str, Any]) -> dict[str, int]:
    starters = league_data.get("starters")
    result: dict[str, int] = {}
    if isinstance(starters, dict):
        minimum_total = 0
        offensive_minimum = 0
        qb_flex = 0
        idp_positions = {"DT", "DE", "DL", "LB", "CB", "S", "DB", "DP"}
        for position_row in _list(starters.get("position")):
            name = str(position_row.get("name", "")).upper()
            bounds = str(position_row.get("limit", "0")).split("-", 1)
            minimum = _int(bounds[0])
            maximum = _int(bounds[-1], minimum)
            if name:
                result[name] = minimum
                minimum_total += minimum
                if name not in idp_positions:
                    offensive_minimum += minimum
                if name == "QB":
                    qb_flex = max(0, maximum - minimum)
        offensive_count = _int(starters.get("iop_starters"))
        flexible = max(
            0,
            offensive_count - offensive_minimum
            if offensive_count
            else _int(starters.get("count")) - minimum_total,
        )
        if qb_flex:
            result["SUPERFLEX"] = min(qb_flex, flexible)
            flexible -= result["SUPERFLEX"]
        if flexible:
            result["FLEX"] = flexible
        return result
    raw = str(starters or league_data.get("startingLineup", league_data.get("lineup", "")))
    for part in raw.replace("|", ",").split(","):
        if ":" in part:
            position, count = part.split(":", 1)
            result[position.strip().upper()] = _int(count)
    return result


def _rule_catalog(payload: dict[str, Any]) -> dict[str, str]:
    root = _root(payload, "allRules")
    return {
        _text(row.get("abbreviation")): _text(row.get("shortDescription"))
        for row in _list(root.get("rule"))
        if _text(row.get("abbreviation"))
    }


def _multiplier(value: Any) -> Decimal | None:
    return _decimal(_text(value).lstrip("*"))


def _rules(payload: dict[str, Any], catalog: dict[str, str]) -> dict[str, Any]:
    root = _root(payload, "rules")
    result: dict[str, Any] = {}
    reception_points: dict[str, Decimal] = {}
    for group in _list(root.get("positionRules")):
        positions = str(group.get("positions", "ALL"))
        for index, row in enumerate(_list(group.get("rule"))):
            event = _text(row.get("event"))
            base_key = f"{positions}:{event or 'unknown'}"
            key = (
                base_key
                if base_key not in result
                else f"{base_key}:{_text(row.get('range')) or index}"
            )
            result[key] = {
                "positions": positions,
                "event": event,
                "description": catalog.get(event) or RULE_DESCRIPTIONS.get(event),
                "points": _text(row.get("points")),
                "range": _text(row.get("range")),
            }
            value = _multiplier(row.get("points"))
            if event == "CC" and value is not None:
                reception_points[positions] = value
            elif event == "#P" and value is not None:
                result["passing_td"] = str(value)
    receiver_values = [
        value
        for positions, value in reception_points.items()
        if any(position in re.split(r"[,|+]", positions) for position in ("RB", "WR"))
    ]
    if receiver_values:
        result["receptions"] = str(receiver_values[0])
    te_values = [
        value
        for positions, value in reception_points.items()
        if "TE" in re.split(r"[,|+]", positions)
    ]
    if te_values:
        baseline = receiver_values[0] if receiver_values else Decimal("0")
        result["te_premium"] = str(max(Decimal("0"), te_values[0] - baseline))
    return result


async def sync_league(
    db: Session,
    client: MFLClient,
    settings: Settings,
    league_id: str,
    league_type_override: LeagueType | None = None,
) -> dict[str, Any]:
    league_type = league_type_override or (
        LeagueType.KEEPER if league_id == settings.mfl_keeper_league_id else LeagueType.AUCTION
    )
    export_types = [
        "league",
        "rules",
        "allRules",
        "players",
        "rosters",
        "selectedKeepers",
        "playerRanks",
        "adp",
        "aav",
        "projectedScores",
        "auctionResults",
        "draftResults",
        "transactions",
    ]
    initialize_sources(db)
    mfl_sources = list(db.scalars(select(DataSource).where(DataSource.id.like("mfl_%"))))
    attempted_at = datetime.now(UTC)
    for source in mfl_sources:
        source.last_attempt_at = attempted_at
    db.commit()
    responses: dict[str, MFLResponse] = {}
    warnings: list[str] = []
    for export_type in export_types:
        try:
            responses[export_type] = await client.export(export_type, league_id=league_id, db=db)
            if responses[export_type].stale:
                warnings.append(f"{export_type}: using stale cached data")
        except MFLError as exc:
            if not _is_expected_unavailable(export_type, exc):
                warnings.append(f"{export_type}: {exc}")
    if "league" not in responses:
        raise MFLError("League metadata is unavailable; synchronization cannot continue")
    league_data = _root(responses["league"].payload, "league")
    roster_size = _int(league_data.get("rosterSize", league_data.get("maxRosterSize")), 0)
    starting_budget = _decimal(
        league_data.get(
            "auctionStartingFunds",
            league_data.get("auctionFunds", league_data.get("salaryCapAmount")),
        ),
        settings.auction_default_budget if league_type == LeagueType.AUCTION else None,
    )
    minimum_bid = (
        _decimal(
            league_data.get("minimumAuctionBid", league_data.get("minBid")),
            settings.auction_min_bid,
        )
        or settings.auction_min_bid
    )
    catalog = _rule_catalog(responses["allRules"].payload) if "allRules" in responses else {}
    scoring = _rules(responses["rules"].payload, catalog) if "rules" in responses else {}
    warnings.extend(scoring_warnings(scoring))
    log_sync_warnings(str(league_id), settings.mfl_season, warnings)
    auction_payload = responses.get("auctionResults")
    if auction_payload:
        auction_root = _root(auction_payload.payload, "auctionResults")
        units = _list(auction_root.get("auctionUnit"))
        if units:
            league_data["auction_unit"] = str(units[0].get("unit", "LEAGUE"))
    league = db.scalar(
        select(League).where(League.id == str(league_id), League.season == settings.mfl_season)
    )
    if league is None:
        league = League(id=str(league_id), season=settings.mfl_season)
        db.add(league)
    league.league_type = league_type.value
    league.name = str(league_data.get("name", f"MFL League {league_id}"))
    league.roster_size = roster_size
    league.starting_budget = starting_budget
    league.minimum_bid = minimum_bid
    league.settings_json = league_data
    league.scoring_rules_json = scoring
    league.lineup_json = _lineup(league_data)
    league.warnings_json = warnings
    league.synced_at = datetime.now(UTC)
    record_sync_warnings(db, str(league_id), warnings)
    franchise_rows = _nested_rows(responses["league"].payload, "league", "franchises", "franchise")
    for row in franchise_rows:
        franchise_id = str(row.get("id", ""))
        if not franchise_id:
            continue
        franchise = db.scalar(
            select(Franchise).where(
                Franchise.league_id == str(league_id), Franchise.id == franchise_id
            )
        )
        if franchise is None:
            franchise = Franchise(id=franchise_id, league_id=str(league_id))
            db.add(franchise)
        franchise.name = str(row.get("name", franchise_id))
        franchise.abbreviation = row.get("abbrev") or row.get("abbreviation")
        franchise.starting_budget = _decimal(
            row.get("auctionFunds", row.get("startingBudget", row.get("salaryCapAmount"))),
            starting_budget or Decimal("0"),
        ) or Decimal("0")
        franchise.roster_slots = roster_size
    if "players" in responses:
        for row in _nested_rows(
            responses["players"].payload, "players", "players", "player"
        ) or _list(_root(responses["players"].payload, "players").get("player")):
            player_id = str(row.get("id", ""))
            if not player_id:
                continue
            player = db.get(Player, player_id) or Player(id=player_id)
            db.add(player)
            player.name = str(row.get("name", row.get("displayName", player_id)))
            player.position = str(row.get("position", "UNK")).upper()
            player.nfl_team = row.get("team") or row.get("nfl_team")
            player.status = row.get("status")
            if str(row.get("rookie", "")).lower() in {"1", "true", "yes"}:
                player.rookie = True
            player.fantasy_positions_json = [
                item.strip().upper()
                for item in str(row.get("position", "UNK")).replace("|", ",").split(",")
                if item.strip()
            ]
            player.metadata_json = {
                **(player.metadata_json or {}),
                "mfl": {
                    "source_player_id": player_id,
                    "fetched_at": responses["players"].fetched_at.isoformat(),
                },
            }
            player.updated_at = datetime.now(UTC)
    db.flush()
    if "rosters" in responses:
        db.execute(delete(RosterAssignment).where(RosterAssignment.league_id == str(league_id)))
        for franchise_row in _nested_rows(
            responses["rosters"].payload, "rosters", "rosters", "franchise"
        ) or _list(_root(responses["rosters"].payload, "rosters").get("franchise")):
            for row in _list(franchise_row.get("player")):
                player_id = str(row.get("id", ""))
                if player_id and db.scalar(select(Player).where(Player.id == player_id)):
                    db.add(
                        RosterAssignment(
                            league_id=str(league_id),
                            franchise_id=str(franchise_row.get("id", "")),
                            player_id=player_id,
                            status=str(row.get("status", "ROSTER")),
                            salary=_decimal(
                                row.get("salary", row.get("contractYear", row.get("cost")))
                            ),
                            contract_info=(
                                str(row.get("contractInfo", row.get("contract", ""))) or None
                            ),
                        )
                    )
    if league_type == LeagueType.KEEPER and "selectedKeepers" in responses:
        db.execute(
            delete(KeeperSelection).where(
                KeeperSelection.league_id == str(league_id), KeeperSelection.source == "mfl"
            )
        )
        keeper_root = _root(responses["selectedKeepers"].payload, "keepers")
        for franchise_row in _list(keeper_root.get("franchise")):
            for row in _list(franchise_row.get("player")):
                player_id = str(row.get("id", ""))
                if player_id and db.scalar(select(Player).where(Player.id == player_id)):
                    db.add(
                        KeeperSelection(
                            league_id=str(league_id),
                            franchise_id=str(franchise_row.get("id", "")),
                            player_id=player_id,
                            keeper_cost=_decimal(row.get("cost")),
                            source="mfl",
                        )
                    )
    db.commit()
    ensure_mfl_identities(db)
    calculate_rankings(db, league, responses)
    succeeded_at = datetime.now(UTC)
    for source in mfl_sources:
        source.last_success_at = succeeded_at
        source.last_error = None
    db.commit()
    return {"league_id": str(league_id), "warnings": warnings, "synced_at": league.synced_at}


def _signals(response: MFLResponse | None, root_name: str) -> dict[str, dict[str, Any]]:
    if response is None:
        return {}
    root = _root(response.payload, root_name)
    rows: list[dict[str, Any]] = []
    for key in ("player", "playerScore", "playerRank"):
        rows = _list(root.get(key))
        if rows:
            break
    return {str(row.get("id", row.get("player", ""))): row for row in rows}


def calculate_rankings(db: Session, league: League, responses: dict[str, MFLResponse]) -> None:
    from app.catalog import draftable_positions

    rank_signal = _signals(responses.get("playerRanks"), "playerRanks")
    adp_signal = _signals(responses.get("adp"), "adp")
    aav_signal = _signals(responses.get("aav"), "aav")
    projection_signal = _signals(responses.get("projectedScores"), "projectedScores")
    unavailable = set(
        db.scalars(
            select(RosterAssignment.player_id).where(RosterAssignment.league_id == league.id)
        )
    ) | set(
        db.scalars(select(KeeperSelection.player_id).where(KeeperSelection.league_id == league.id))
    )
    allowed_positions = draftable_positions(db, league.id)
    players = [
        player
        for player in db.scalars(select(Player).where(Player.id.not_in(unavailable)))
        if player.position.upper() in allowed_positions
        or (player.position.upper() == "K" and "PK" in allowed_positions)
    ]
    inputs: list[RankingInput] = []
    for player in players:
        rank_row = rank_signal.get(player.id, {})
        adp_row = adp_signal.get(player.id, {})
        aav_row = aav_signal.get(player.id, {})
        projection_row = projection_signal.get(player.id, {})
        inputs.append(
            RankingInput(
                player_id=player.id,
                position=player.position,
                projection=_decimal(projection_row.get("score", projection_row.get("points"))),
                mfl_rank=_int(rank_row.get("rank"), 0) or None,
                adp=_decimal(adp_row.get("averagePick", adp_row.get("adp"))),
                aav=_decimal(aav_row.get("aav", aav_row.get("value"))),
            )
        )
    franchises = list(db.scalars(select(Franchise).where(Franchise.league_id == league.id)))
    total_budget = sum((Decimal(item.starting_budget) for item in franchises), Decimal("0"))
    spent = db.scalar(
        select(
            func.coalesce(
                func.sum(
                    __import__("app.models", fromlist=["AuctionPurchase"]).AuctionPurchase.amount
                ),
                0,
            )
        ).where(
            __import__("app.models", fromlist=["AuctionPurchase"]).AuctionPurchase.league_id
            == league.id
        )
    )
    remaining_pool = total_budget - Decimal(str(spent or 0))
    ranked = rank_players(
        inputs,
        scoring_rules=league.scoring_rules_json,
        lineup=league.lineup_json,
        franchise_count=len(franchises),
        roster_size=league.roster_size,
        available_spending_pool=max(Decimal("0"), remaining_pool),
        minimum_bid=Decimal(league.minimum_bid),
    )
    db.execute(delete(RankingSnapshot).where(RankingSnapshot.league_id == league.id))
    created = datetime.now(UTC)
    input_map = {item.player_id: item for item in inputs}
    for row in ranked:
        source = input_map[row.player_id]
        db.add(
            RankingSnapshot(
                league_id=league.id,
                player_id=row.player_id,
                overall_rank=row.overall_rank,
                position_rank=row.position_rank,
                tier=row.tier,
                custom_score=row.custom_score,
                projected_points=row.projected_points,
                replacement_points=row.replacement_points,
                value_over_replacement=row.vorp,
                adp=source.adp,
                mfl_rank=source.mfl_rank,
                mfl_aav=source.aav,
                baseline_auction_value=row.baseline_value,
                suggested_auction_value=row.live_value,
                source_summary_json=row.sources,
                created_at=created,
            )
        )
    db.commit()


async def sync_configured(
    db: Session, client: MFLClient, settings: Settings
) -> list[dict[str, Any]]:
    league_ids = [
        league_id
        for league_id in [settings.mfl_keeper_league_id, settings.mfl_auction_league_id]
        if league_id
    ]
    if not league_ids:
        raise ValueError("Configure at least one MFL league ID in .env")
    leagues: list[tuple[str, LeagueType | None]] = []
    for league_id in dict.fromkeys(league_ids):
        stored = db.scalar(
            select(League).where(League.id == league_id, League.season == settings.mfl_season)
        )
        leagues.append((league_id, LeagueType(stored.league_type) if stored else None))
    return await sync_leagues(db, client, settings, leagues)


async def sync_leagues(
    db: Session,
    client: MFLClient,
    settings: Settings,
    leagues: list[tuple[str, LeagueType | None]],
) -> list[dict[str, Any]]:
    """Synchronize an explicit league list instead of relying on global configured defaults."""
    results = []
    seen: set[str] = set()
    for league_id, league_type in leagues:
        if league_id in seen:
            continue
        seen.add(league_id)
        results.append(await sync_league(db, client, settings, league_id, league_type))
    sync_local_ranking_sources(db)
    return results
