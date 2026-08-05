import csv
import io
import re
import unicodedata
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import DataSource, League, Player, PlayerIdentity, SourcePlayerValue

NFLVERSE_PLAYERS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/players/players.csv"
)
NFLVERSE_SCHEDULE_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
)

DEFAULT_SOURCES: list[dict[str, Any]] = [
    {
        "id": "league_model",
        "name": "League Scoring & VOR Model",
        "kind": "ranking",
        "enabled": True,
        "weight": Decimal("1"),
        "terms_url": None,
        "license": "Local model",
        "attribution": "DraftDesk league-specific scoring and replacement model",
        "cache_ttl_seconds": 0,
    },
    {
        "id": "mfl_rank",
        "name": "MFL Player Rank",
        "kind": "ranking",
        "enabled": True,
        "weight": Decimal("1"),
        "terms_url": "https://api.myfantasyleague.com/2026/api_info",
        "license": "MFL API terms",
        "attribution": "MyFantasyLeague / FantasySharks",
        "cache_ttl_seconds": 21600,
    },
    {
        "id": "mfl_adp",
        "name": "MFL Average Draft Position",
        "kind": "market",
        "enabled": True,
        "weight": Decimal("1"),
        "terms_url": "https://api.myfantasyleague.com/2026/api_info",
        "license": "MFL API terms",
        "attribution": "MyFantasyLeague",
        "cache_ttl_seconds": 21600,
    },
    {
        "id": "mfl_aav",
        "name": "MFL Average Auction Value",
        "kind": "market",
        "enabled": True,
        "weight": Decimal("0.5"),
        "terms_url": "https://api.myfantasyleague.com/2026/api_info",
        "license": "MFL API terms",
        "attribution": "MyFantasyLeague",
        "cache_ttl_seconds": 21600,
    },
    {
        "id": "mfl_projection",
        "name": "MFL Scoring-Adjusted Weekly Projection",
        "kind": "projection",
        "enabled": True,
        "weight": Decimal("1"),
        "terms_url": "https://api.myfantasyleague.com/2026/api_info",
        "license": "MFL API terms",
        "attribution": "MyFantasyLeague / FantasySharks",
        "cache_ttl_seconds": 7200,
    },
    {
        "id": "gng",
        "name": "The GNG Pigskin Rankings",
        "kind": "ranking",
        "enabled": True,
        "weight": Decimal("1"),
        "terms_url": "https://www.thegng.us/feeds",
        "license": "CC BY 4.0",
        "attribution": "Pigskin rankings by The GNG — https://www.thegng.us/ranks",
        "cache_ttl_seconds": 86400,
    },
    {
        "id": "fantasypros",
        "name": "FantasyPros Expert Consensus Rankings",
        "kind": "ranking",
        "enabled": True,
        "weight": Decimal("1"),
        "terms_url": "https://api.fantasypros.com/public/v2/terms-of-use",
        "license": "FantasyPros API terms; personal use tier depends on key",
        "attribution": "Expert Consensus Rankings by FantasyPros",
        "cache_ttl_seconds": 21600,
    },
    {
        "id": "fantasypros_dynasty",
        "name": "FantasyPros Dynasty Rankings (ADFL only)",
        "kind": "ranking",
        "enabled": True,
        "weight": Decimal("1"),
        "terms_url": "https://api.fantasypros.com/public/v2/terms-of-use",
        "license": "FantasyPros API terms; personal use tier depends on key",
        "attribution": "Dynasty Expert Consensus Rankings by FantasyPros",
        "cache_ttl_seconds": 21600,
    },
    {
        "id": "sleeper",
        "name": "Sleeper Player Metadata & Trends",
        "kind": "metadata",
        "enabled": True,
        "weight": Decimal("0.25"),
        "terms_url": "https://docs.sleeper.com/",
        "license": "Sleeper API terms",
        "attribution": "Player metadata and trends by Sleeper",
        "cache_ttl_seconds": 86400,
    },
    {
        "id": "nflverse",
        "name": "nflverse Player Identity",
        "kind": "metadata",
        "enabled": True,
        "weight": Decimal("0.15"),
        "terms_url": "https://github.com/nflverse/nflverse-data",
        "license": "CC-BY-4.0",
        "attribution": "Data provided by nflverse",
        "cache_ttl_seconds": 86400,
    },
    {
        "id": "user_csv",
        "name": "User Ranking Imports",
        "kind": "ranking",
        "enabled": True,
        "weight": Decimal("1"),
        "terms_url": None,
        "license": "User supplied",
        "attribution": "User-supplied ranking sheet",
        "cache_ttl_seconds": 0,
    },
]


def initialize_sources(db: Session) -> None:
    for values in DEFAULT_SOURCES:
        source = db.get(DataSource, values["id"])
        if source is None:
            db.add(DataSource(**values))
            continue
        source.enabled = True
        if Decimal(source.weight) <= 0 and Decimal(values["weight"]) > 0:
            source.weight = values["weight"]
    db.commit()


def source_json(source: DataSource) -> dict[str, Any]:
    return {
        "id": source.id,
        "name": source.name,
        "kind": source.kind,
        "enabled": source.enabled,
        "weight": str(source.weight),
        "terms_url": source.terms_url,
        "license": source.license,
        "attribution": source.attribution,
        "cache_ttl_seconds": source.cache_ttl_seconds,
        "last_attempt_at": source.last_attempt_at.isoformat() if source.last_attempt_at else None,
        "last_success_at": source.last_success_at.isoformat() if source.last_success_at else None,
        "last_error": source.last_error,
        "healthy": source.last_error is None,
    }


def normalize_player_name(value: str) -> str:
    name = value.strip()
    if "," in name:
        last, first = name.split(",", 1)
        name = f"{first.strip()} {last.strip()}"
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", name, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]", "", name.lower())


TEAM_ALIASES = {
    "GB": "GBP",
    "JAX": "JAC",
    "KC": "KCC",
    "LV": "LVR",
    "NE": "NEP",
    "NO": "NOS",
    "SF": "SFO",
    "TB": "TBB",
}


def normalize_team(value: str | None) -> str:
    team = (value or "").upper().strip()
    return TEAM_ALIASES.get(team, team)


def _schedule_data(
    csv_text: str, season: int
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, int],
    dict[str, int],
    dict[str, float],
    dict[str, float],
]:
    """Build team schedules and transparent difficulty ranks from nflverse games."""
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    if not rows or not {"season", "week", "away_team", "home_team"}.issubset(rows[0]):
        return {}, {}, {}, {}, {}

    prior_stats: dict[str, dict[str, float]] = {}
    for row in rows:
        if row.get("season") != str(season - 1) or row.get("game_type") != "REG":
            continue
        away = normalize_team(row.get("away_team"))
        home = normalize_team(row.get("home_team"))
        try:
            away_score = float(row.get("away_score") or "")
            home_score = float(row.get("home_score") or "")
        except ValueError:
            continue
        for team, scored, allowed in (
            (away, away_score, home_score),
            (home, home_score, away_score),
        ):
            stats = prior_stats.setdefault(team, {"games": 0, "for": 0, "against": 0})
            stats["games"] += 1
            stats["for"] += scored
            stats["against"] += allowed

    points_for = {
        team: values["for"] / values["games"]
        for team, values in prior_stats.items()
        if values["games"]
    }
    points_against = {
        team: values["against"] / values["games"]
        for team, values in prior_stats.items()
        if values["games"]
    }
    league_for = sum(points_for.values()) / len(points_for) if points_for else 22.5
    league_against = sum(points_against.values()) / len(points_against) if points_against else 22.5

    schedules: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("season") != str(season) or row.get("game_type") != "REG":
            continue
        try:
            week = int(row.get("week") or 0)
        except ValueError:
            continue
        away = normalize_team(row.get("away_team"))
        home = normalize_team(row.get("home_team"))
        common = {
            "week": week,
            "date": row.get("gameday"),
            "weekday": row.get("weekday"),
            "time": row.get("gametime"),
            "game_id": row.get("game_id"),
        }
        schedules.setdefault(away, []).append({**common, "opponent": home, "home_away": "away"})
        schedules.setdefault(home, []).append({**common, "opponent": away, "home_away": "home"})
    for games in schedules.values():
        games.sort(key=lambda game: (int(game["week"]), str(game.get("date") or "")))

    offense_scores = {
        team: sum(points_against.get(game["opponent"], league_against) for game in games)
        / len(games)
        for team, games in schedules.items()
        if games
    }
    defense_scores = {
        team: sum(points_for.get(game["opponent"], league_for) for game in games) / len(games)
        for team, games in schedules.items()
        if games
    }
    offense_ranks = {
        team: rank
        for rank, (team, _) in enumerate(
            sorted(offense_scores.items(), key=lambda item: (-item[1], item[0])), 1
        )
    }
    defense_ranks = {
        team: rank
        for rank, (team, _) in enumerate(
            sorted(defense_scores.items(), key=lambda item: (item[1], item[0])), 1
        )
    }
    return schedules, offense_ranks, defense_ranks, offense_scores, defense_scores


def _identity_for(db: Session, player: Player) -> PlayerIdentity:
    identity = db.scalar(select(PlayerIdentity).where(PlayerIdentity.player_id == player.id))
    if identity is None:
        identity = PlayerIdentity(
            player_id=player.id,
            mfl_id=player.id,
            match_method="exact_id",
            match_confidence=Decimal("1"),
            verified=True,
        )
        db.add(identity)
    return identity


def ensure_mfl_identities(db: Session) -> None:
    for player in db.scalars(select(Player)):
        _identity_for(db, player)
    db.commit()


def _player_indexes(
    players: list[Player],
) -> tuple[dict[tuple[str, str, str], Player], dict[tuple[str, str], list[Player]]]:
    exact: dict[tuple[str, str, str], Player] = {}
    fallback: dict[tuple[str, str], list[Player]] = {}
    for player in players:
        name = normalize_player_name(player.name)
        position = player.position.upper()
        team = normalize_team(player.nfl_team)
        exact[(name, team, position)] = player
        fallback.setdefault((name, position), []).append(player)
    return exact, fallback


def _match_player(
    exact: dict[tuple[str, str, str], Player],
    fallback: dict[tuple[str, str], list[Player]],
    *,
    name: str,
    team: str | None,
    position: str,
) -> tuple[Player | None, str, Decimal]:
    normalized_name = normalize_player_name(name)
    normalized_position = "DEF" if position.upper() in {"DST", "D/ST"} else position.upper()
    player = exact.get((normalized_name, normalize_team(team), normalized_position))
    if player is not None:
        return player, "exact_name_team", Decimal("0.96")
    candidates = fallback.get((normalized_name, normalized_position), [])
    if len(candidates) == 1:
        return candidates[0], "exact_name_position", Decimal("0.86")
    return None, "unresolved", Decimal("0")


def scoring_profile(scoring_rules: dict[str, Any]) -> tuple[str, str]:
    receptions = Decimal(str(scoring_rules.get("receptions", 0) or 0))
    if receptions >= Decimal("0.75"):
        return "ppr", "PPR"
    if receptions >= Decimal("0.25"):
        return "half_ppr", "HALF"
    return "standard", "STD"


async def sync_gng(
    db: Session,
    league_id: str,
    scoring_rules: dict[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    source = db.get(DataSource, "gng")
    if source is None:
        initialize_sources(db)
        source = db.get(DataSource, "gng")
    assert source is not None
    source.last_attempt_at = datetime.now(UTC)
    db.commit()
    profile, _ = scoring_profile(scoring_rules)
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "MFLDraftManager/1.0"},
            timeout=httpx.Timeout(30),
            transport=transport,
        ) as client:
            response = await client.get(
                "https://www.thegng.us/api/rankings.json",
                params={"profile": profile, "pos": "overall", "limit": "150"},
            )
            response.raise_for_status()
        payload = response.json()
        rows = payload.get("players", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise ValueError("The GNG returned an unexpected response shape")
        exact, fallback = _player_indexes(list(db.scalars(select(Player))))
        mapped: list[tuple[Player, dict[str, Any]]] = []
        unresolved = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            player, _, _ = _match_player(
                exact,
                fallback,
                name=str(row.get("player", "")),
                team=str(row.get("team", "")),
                position=str(row.get("position", "")),
            )
            if player is None or row.get("rank") is None:
                unresolved += 1
                continue
            mapped.append((player, row))
        snapshot_id = str(payload.get("board_version") or f"gng-{datetime.now(UTC).timestamp()}")
        db.execute(
            delete(SourcePlayerValue).where(
                SourcePlayerValue.source_id == "gng",
                SourcePlayerValue.league_id == league_id,
            )
        )
        generated_at = payload.get("generated_at")
        source_updated = (
            datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
            if generated_at
            else None
        )
        for player, row in mapped:
            db.add(
                SourcePlayerValue(
                    source_id="gng",
                    league_id=league_id,
                    player_id=player.id,
                    value_type="rank",
                    raw_value_json={
                        **row,
                        "profile": profile,
                        "license": payload.get("license"),
                        "source_url": payload.get("url"),
                    },
                    normalized_value=Decimal(str(row["rank"])),
                    source_updated_at=source_updated,
                    fetched_at=datetime.now(UTC),
                    snapshot_id=snapshot_id,
                )
            )
        source.last_success_at = datetime.now(UTC)
        source.last_error = None
        db.commit()
        return {
            "matched": len(mapped),
            "unresolved": unresolved,
            "profile": profile,
            "snapshot_id": snapshot_id,
        }
    except Exception as exc:
        source.last_error = f"{type(exc).__name__}: {exc}"
        db.commit()
        raise


async def sync_fantasypros(
    db: Session,
    league_id: str,
    season: int,
    scoring_rules: dict[str, Any],
    api_key: str,
    *,
    source_id: str = "fantasypros",
    ranking_type: str = "DRAFT",
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    if not api_key:
        raise ValueError("Add the FantasyPros API key in Settings before syncing")
    source = db.get(DataSource, source_id)
    if source is None:
        initialize_sources(db)
        source = db.get(DataSource, source_id)
    assert source is not None
    source.last_attempt_at = datetime.now(UTC)
    db.commit()
    _, scoring = scoring_profile(scoring_rules)
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "MFLDraftManager/1.0", "x-api-key": api_key},
            timeout=httpx.Timeout(30),
            transport=transport,
        ) as client:
            response = await client.get(
                f"https://api.fantasypros.com/public/v2/json/nfl/{season}/consensus-rankings",
                params={"position": "ALL", "scoring": scoring, "type": ranking_type},
            )
            response.raise_for_status()
        payload = response.json()
        rows = payload.get("players", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise ValueError("FantasyPros returned an unexpected response shape")
        exact, fallback = _player_indexes(list(db.scalars(select(Player))))
        mapped: list[tuple[Player, dict[str, Any], str, Decimal]] = []
        unresolved = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            player, method, confidence = _match_player(
                exact,
                fallback,
                name=str(row.get("player_name", "")),
                team=str(row.get("player_team_id", row.get("team_id", ""))),
                position=str(row.get("player_position_id", row.get("position_id", ""))),
            )
            rank = row.get("rank_ecr", row.get("rank"))
            if player is None or rank is None:
                unresolved += 1
                continue
            mapped.append((player, row, method, confidence))
        updated_token = payload.get("last_updated_ts", int(datetime.now(UTC).timestamp()))
        snapshot_id = f"{source_id}-{season}-{scoring}-{ranking_type.lower()}-{updated_token}"
        db.execute(
            delete(SourcePlayerValue).where(
                SourcePlayerValue.source_id == source_id,
                SourcePlayerValue.league_id == league_id,
            )
        )
        for player, row, method, confidence in mapped:
            identity = _identity_for(db, player)
            identity.fantasypros_id = str(row.get("player_id"))
            identity.match_method = method
            identity.match_confidence = confidence
            identity.updated_at = datetime.now(UTC)
            db.add(
                SourcePlayerValue(
                    source_id=source_id,
                    league_id=league_id,
                    player_id=player.id,
                    value_type="rank",
                    raw_value_json={
                        **row,
                        "scoring": scoring,
                        "ranking_type": ranking_type,
                    },
                    normalized_value=Decimal(str(row.get("rank_ecr", row.get("rank")))),
                    source_updated_at=datetime.now(UTC),
                    fetched_at=datetime.now(UTC),
                    snapshot_id=snapshot_id,
                )
            )
        source.last_success_at = datetime.now(UTC)
        source.last_error = None
        db.commit()
        return {
            "matched": len(mapped),
            "unresolved": unresolved,
            "scoring": scoring,
            "ranking_type": ranking_type,
            "snapshot_id": snapshot_id,
        }
    except Exception as exc:
        source.last_error = f"{type(exc).__name__}: {exc}"
        db.commit()
        raise


async def sync_sleeper(
    db: Session, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, int]:
    source = db.get(DataSource, "sleeper")
    if source is None:
        initialize_sources(db)
        source = db.get(DataSource, "sleeper")
    assert source is not None
    source.last_attempt_at = datetime.now(UTC)
    db.commit()
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "MFLDraftManager/1.0"},
            timeout=httpx.Timeout(30),
            transport=transport,
        ) as client:
            directory_response = await client.get(
                "https://api.sleeper.app/v1/players/nfl?active=true"
            )
            directory_response.raise_for_status()
            trend_response = await client.get(
                "https://api.sleeper.app/v1/players/nfl/trending/add",
                params={"lookback_hours": "24", "limit": "250"},
            )
            trend_response.raise_for_status()
        directory = directory_response.json()
        trends = trend_response.json()
        if not isinstance(directory, dict) or not isinstance(trends, list):
            raise ValueError("Sleeper returned an unexpected response shape")
        players = list(db.scalars(select(Player)))
        exact, fallback = _player_indexes(players)
        sleeper_to_player: dict[str, Player] = {}
        matched = 0
        unresolved = 0
        for sleeper_id, row in directory.items():
            if not isinstance(row, dict):
                continue
            display_name = str(
                row.get("full_name")
                or " ".join(part for part in [row.get("first_name"), row.get("last_name")] if part)
            )
            position = str(row.get("position") or "").upper()
            team = str(row.get("team") or "").upper()
            name = normalize_player_name(display_name)
            player = exact.get((name, team, position))
            method = "exact_name_team"
            confidence = Decimal("0.96")
            if player is None:
                candidates = fallback.get((name, position), [])
                if len(candidates) == 1:
                    player = candidates[0]
                    method = "exact_name_position"
                    confidence = Decimal("0.86")
            if player is None:
                unresolved += 1
                continue
            matched += 1
            sleeper_to_player[str(sleeper_id)] = player
            identity = _identity_for(db, player)
            identity.sleeper_id = str(sleeper_id)
            identity.match_method = method
            identity.match_confidence = confidence
            identity.updated_at = datetime.now(UTC)
            player.fantasy_positions_json = [
                str(item) for item in row.get("fantasy_positions", []) if item
            ]
            player.injury_status = row.get("injury_status")
            player.practice_participation = row.get("practice_participation")
            player.status = row.get("status") or player.status
            player.metadata_json = {
                **(player.metadata_json or {}),
                "sleeper": {
                    "depth_chart_position": row.get("depth_chart_position"),
                    "years_exp": row.get("years_exp"),
                    "number": row.get("number"),
                },
            }
            player.updated_at = datetime.now(UTC)
        snapshot = f"sleeper-{int(datetime.now(UTC).timestamp())}"
        db.execute(
            delete(SourcePlayerValue).where(
                SourcePlayerValue.source_id == "sleeper",
                SourcePlayerValue.value_type == "trend_add_24h",
            )
        )
        for row in trends:
            if not isinstance(row, dict):
                continue
            player = sleeper_to_player.get(str(row.get("player_id", "")))
            if player is None:
                continue
            count = Decimal(str(row.get("count", 0)))
            db.add(
                SourcePlayerValue(
                    source_id="sleeper",
                    player_id=player.id,
                    value_type="trend_add_24h",
                    raw_value_json={"count": int(count)},
                    normalized_value=count,
                    fetched_at=datetime.now(UTC),
                    snapshot_id=snapshot,
                )
            )
        source.last_success_at = datetime.now(UTC)
        source.last_error = None
        db.commit()
        return {"matched": matched, "unresolved": unresolved}
    except Exception as exc:
        source.last_error = f"{type(exc).__name__}: {exc}"
        db.commit()
        raise


async def sync_nflverse(
    db: Session,
    season: int | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, int]:
    source = db.get(DataSource, "nflverse")
    if source is None:
        initialize_sources(db)
        source = db.get(DataSource, "nflverse")
    assert source is not None
    source.last_attempt_at = datetime.now(UTC)
    db.commit()
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "MFLDraftManager/1.0"},
            timeout=httpx.Timeout(60),
            follow_redirects=True,
            transport=transport,
        ) as client:
            response = await client.get(NFLVERSE_PLAYERS_URL)
            response.raise_for_status()
            schedule_response = await client.get(NFLVERSE_SCHEDULE_URL)
            schedule_response.raise_for_status()
        selected_season = season or max(list(db.scalars(select(League.season))), default=2026)
        (
            schedules,
            offense_ranks,
            defense_ranks,
            offense_scores,
            defense_scores,
        ) = _schedule_data(schedule_response.text, selected_season)
        players = list(db.scalars(select(Player)))
        exact, fallback = _player_indexes(players)
        matched = 0
        unresolved = 0
        for row in csv.DictReader(io.StringIO(response.text)):
            name = normalize_player_name(row.get("display_name", ""))
            position = (row.get("position") or row.get("position_group") or "").upper()
            team = (row.get("latest_team") or "").upper()
            player = exact.get((name, team, position))
            method = "exact_name_team"
            confidence = Decimal("0.94")
            if player is None:
                candidates = fallback.get((name, position), [])
                if len(candidates) == 1:
                    player = candidates[0]
                    method = "exact_name_position"
                    confidence = Decimal("0.84")
            if player is None:
                unresolved += 1
                continue
            matched += 1
            identity = _identity_for(db, player)
            identity.gsis_id = row.get("gsis_id") or identity.gsis_id
            identity.espn_id = row.get("espn_id") or identity.espn_id
            identity.other_ids_json = {
                **(identity.other_ids_json or {}),
                "pfr_id": row.get("pfr_id"),
                "pff_id": row.get("pff_id"),
            }
            identity.match_method = method
            identity.match_confidence = confidence
            identity.updated_at = datetime.now(UTC)
            if not player.birthdate and row.get("birth_date"):
                try:
                    player.birthdate = date.fromisoformat(row["birth_date"])
                except ValueError:
                    pass
            if row.get("rookie_season"):
                player.rookie = row["rookie_season"] == str(datetime.now(UTC).year)
            player.metadata_json = {
                **(player.metadata_json or {}),
                "nflverse": {
                    "gsis_status": row.get("status"),
                    "years_of_experience": row.get("years_of_experience"),
                    "college": row.get("college_name"),
                    "headshot": row.get("headshot"),
                },
            }
            player.updated_at = datetime.now(UTC)
        db.execute(
            delete(SourcePlayerValue).where(
                SourcePlayerValue.source_id == "nflverse",
                SourcePlayerValue.value_type == "schedule",
            )
        )
        schedule_snapshot = (
            f"nflverse-schedule-{selected_season}-{int(datetime.now(UTC).timestamp())}"
        )
        for player in players:
            team = normalize_team(player.nfl_team)
            games = schedules.get(team)
            if not games:
                continue
            is_defense = player.position.upper() in {"DEF", "DST", "D/ST"}
            schedule_rank = (defense_ranks if is_defense else offense_ranks).get(team)
            difficulty_score = (defense_scores if is_defense else offense_scores).get(team)
            if schedule_rank is None:
                continue
            weeks = {int(game["week"]) for game in games}
            bye_week = next((week for week in range(1, 19) if week not in weeks), None)
            if bye_week is not None:
                player.bye_week = bye_week
            db.add(
                SourcePlayerValue(
                    source_id="nflverse",
                    league_id=None,
                    player_id=player.id,
                    value_type="schedule",
                    raw_value_json={
                        "season": selected_season,
                        "team": team,
                        "games": games,
                        "schedule_rank": schedule_rank,
                        "schedule_rank_label": f"{schedule_rank} of {len(schedules)}",
                        "difficulty_score": round(difficulty_score or 0, 2),
                        "rank_basis": (
                            "Opponent prior-season scoring; lower is easier for team defense"
                            if is_defense
                            else (
                                "Opponent prior-season points allowed; higher is easier for offense"
                            )
                        ),
                        "source_url": NFLVERSE_SCHEDULE_URL,
                    },
                    normalized_value=Decimal(schedule_rank),
                    source_updated_at=datetime.now(UTC),
                    fetched_at=datetime.now(UTC),
                    snapshot_id=schedule_snapshot,
                )
            )
        source.last_success_at = datetime.now(UTC)
        source.last_error = None
        db.commit()
        return {"matched": matched, "unresolved": unresolved}
    except Exception as exc:
        source.last_error = f"{type(exc).__name__}: {exc}"
        db.commit()
        raise
