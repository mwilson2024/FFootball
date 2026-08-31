import csv
import hashlib
import io
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import mean, median
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AuctionPurchase,
    ConsensusSnapshot,
    DataSource,
    DraftPick,
    Franchise,
    KeeperSelection,
    League,
    LeagueType,
    PersonalPlayerPreference,
    Player,
    RankingSnapshot,
    RosterAssignment,
    SourcePlayerValue,
    UserPlayerPreference,
)
from app.sources import normalize_player_name
from app.user_context import active_username, personal_source_prefix
from app.users import avoided_teams, canonical_nfl_team, effective_source_settings

SOURCE_FAMILIES = {
    "league_model": "mfl",
    "mfl_rank": "mfl",
    "mfl_adp": "mfl",
    "mfl_aav": "mfl",
    "mfl_projection": "mfl",
    "espn_ppr_csv": "espn",
    "espn_dynasty_csv": "espn",
    "fantasypros_redraft_csv": "fantasypros",
    "fantasypros_dynasty_csv": "fantasypros",
    "fantasysharks_dynasty_csv": "fantasysharks",
    "pff_rankings_csv": "pff",
    "gng": "gng",
    "sleeper": "sleeper",
    "nflverse": "nflverse",
}


def _display_rank(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.01"))
    return format(rounded, "f").rstrip("0").rstrip(".")


def _preference_json(
    preference: PersonalPlayerPreference | UserPlayerPreference | None,
) -> dict[str, Any]:
    if preference is None:
        return {
            "manual_rank": None,
            "manual_tier": None,
            "queue_order": None,
            "target": False,
            "fade": False,
            "do_not_draft": False,
            "notes": None,
            "tags": [],
            "hidden": False,
        }
    return {
        "manual_rank": preference.manual_rank,
        "manual_tier": preference.manual_tier,
        "queue_order": preference.queue_order,
        "target": preference.target,
        "fade": preference.fade,
        "do_not_draft": preference.do_not_draft,
        "notes": preference.notes,
        "tags": preference.tags_json,
        "hidden": "hidden" in (preference.tags_json or []),
    }


def availability_maps(db: Session, league_id: str) -> dict[str, Any]:
    roster_rows = list(
        db.execute(
            select(RosterAssignment.player_id, RosterAssignment.franchise_id).where(
                RosterAssignment.league_id == league_id
            )
        )
    )
    rostered_by = {str(player_id): str(franchise_id) for player_id, franchise_id in roster_rows}
    keepers = set(
        db.scalars(select(KeeperSelection.player_id).where(KeeperSelection.league_id == league_id))
    )
    purchased_by = {
        str(player_id): str(franchise_id)
        for player_id, franchise_id in db.execute(
            select(AuctionPurchase.player_id, AuctionPurchase.franchise_id).where(
                AuctionPurchase.league_id == league_id,
                AuctionPurchase.active.is_(True),
            )
        )
    }
    drafted_by = {
        str(player_id): str(franchise_id or "")
        for player_id, franchise_id in db.execute(
            select(DraftPick.player_id, DraftPick.franchise_id).where(
                DraftPick.league_id == league_id
            )
        )
    }
    unavailable = set(rostered_by) | keepers | set(purchased_by) | set(drafted_by)
    return {
        "rostered_by": rostered_by,
        "keepers": keepers,
        "purchased_by": purchased_by,
        "drafted_by": drafted_by,
        "unavailable": unavailable,
    }


def _latest_user_ranks(db: Session, league_id: str) -> dict[str, dict[str, Decimal]]:
    values = list(
        db.scalars(
            select(SourcePlayerValue)
            .join(DataSource, DataSource.id == SourcePlayerValue.source_id)
            .where(
                SourcePlayerValue.league_id == league_id,
                SourcePlayerValue.value_type == "rank",
            )
            .order_by(SourcePlayerValue.fetched_at.desc(), SourcePlayerValue.id.desc())
        )
    )
    result: dict[str, dict[str, Decimal]] = {}
    seen: set[tuple[str, str]] = set()
    for value in values:
        key = (value.source_id, value.player_id)
        if key in seen or value.normalized_value is None:
            continue
        seen.add(key)
        result.setdefault(value.player_id, {})[value.source_id] = Decimal(value.normalized_value)
    return result


def _latest_source_raw(db: Session, league_id: str, source_id: str) -> dict[str, dict[str, Any]]:
    values = list(
        db.scalars(
            select(SourcePlayerValue)
            .where(
                SourcePlayerValue.league_id == league_id,
                SourcePlayerValue.source_id == source_id,
                SourcePlayerValue.value_type == "rank",
            )
            .order_by(SourcePlayerValue.fetched_at.desc(), SourcePlayerValue.id.desc())
        )
    )
    result: dict[str, dict[str, Any]] = {}
    for value in values:
        if value.player_id not in result:
            result[value.player_id] = value.raw_value_json or {}
    return result


def _latest_metric_values(
    db: Session,
    league_id: str,
    source_id: str,
    value_type: str,
) -> dict[str, Decimal]:
    values = list(
        db.scalars(
            select(SourcePlayerValue)
            .where(
                SourcePlayerValue.source_id == source_id,
                SourcePlayerValue.value_type == value_type,
                or_(
                    SourcePlayerValue.league_id == league_id,
                    SourcePlayerValue.league_id.is_(None),
                ),
            )
            .order_by(SourcePlayerValue.fetched_at.desc(), SourcePlayerValue.id.desc())
        )
    )
    result: dict[str, Decimal] = {}
    for value in values:
        if value.player_id not in result and value.normalized_value is not None:
            result[value.player_id] = Decimal(value.normalized_value)
    return result


def _ordinal_ranks(values: dict[str, Decimal], *, higher_is_better: bool) -> dict[str, Decimal]:
    ordered = sorted(
        values.items(),
        key=lambda item: (item[1], item[0]),
        reverse=higher_is_better,
    )
    return {player_id: Decimal(index) for index, (player_id, _) in enumerate(ordered, 1)}


def build_consensus(
    db: Session,
    league_id: str,
    source_overrides: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    league_type = db.scalar(
        select(League.league_type)
        .where(League.id == league_id)
        .order_by(League.season.desc())
        .limit(1)
    )
    players = list(db.scalars(select(Player)))
    rankings = {
        item.player_id: item
        for item in db.scalars(
            select(RankingSnapshot).where(RankingSnapshot.league_id == league_id)
        )
    }
    legacy_preferences = {
        item.player_id: item
        for item in db.scalars(
            select(PersonalPlayerPreference).where(PersonalPlayerPreference.league_id == league_id)
        )
    }
    preferences: dict[str, PersonalPlayerPreference | UserPlayerPreference] = (
        dict(legacy_preferences) if active_username() == "wilsonmw" else {}
    )
    preferences.update(
        {
            item.player_id: item
            for item in db.scalars(
                select(UserPlayerPreference).where(
                    UserPlayerPreference.username == active_username(),
                    UserPlayerPreference.league_id == league_id,
                )
            )
        }
    )
    avoided_team_codes = avoided_teams(db)
    source_options = effective_source_settings(db)
    for source_id, override in (source_overrides or {}).items():
        if source_id in source_options:
            source_options[source_id] = {
                "enabled": bool(override.get("enabled", source_options[source_id]["enabled"])),
                "weight": Decimal(str(override.get("weight", source_options[source_id]["weight"]))),
            }
    sources = {
        item.id: item
        for item in db.scalars(select(DataSource))
        if source_options.get(item.id, {}).get("enabled", True)
        and Decimal(source_options.get(item.id, {}).get("weight", 0)) > 0
    }
    custom_ranks = _latest_user_ranks(db, league_id)
    fantasypros_source_id = (
        "fantasypros_dynasty_csv" if league_type == LeagueType.KEEPER else "fantasypros_redraft_csv"
    )
    fantasypros_raw = _latest_source_raw(db, league_id, fantasypros_source_id)
    rank_values_by_source: dict[str, dict[str, Decimal]] = {}
    for player in players:
        ranking = rankings.get(player.id)
        if ranking and "league_model" in sources:
            rank_values_by_source.setdefault("league_model", {})[player.id] = Decimal(
                ranking.overall_rank
            )
        if ranking and ranking.mfl_rank is not None and "mfl_rank" in sources:
            rank_values_by_source.setdefault("mfl_rank", {})[player.id] = Decimal(ranking.mfl_rank)
        if ranking and ranking.adp is not None and "mfl_adp" in sources:
            rank_values_by_source.setdefault("mfl_adp", {})[player.id] = Decimal(ranking.adp)
        for source_id, rank in custom_ranks.get(player.id, {}).items():
            if source_id in sources:
                rank_values_by_source.setdefault(source_id, {})[player.id] = rank
    if "mfl_projection" in sources:
        projection_values = {
            player_id: Decimal(ranking.projected_points)
            for player_id, ranking in rankings.items()
            if ranking.projected_points is not None
            and str((ranking.source_summary_json or {}).get("projection_note", "")).startswith(
                "MFL weekly projected score"
            )
        }
        rank_values_by_source["mfl_projection"] = _ordinal_ranks(
            projection_values, higher_is_better=True
        )
    if "mfl_aav" in sources and league_type == LeagueType.AUCTION:
        aav_values = {
            player_id: Decimal(ranking.mfl_aav)
            for player_id, ranking in rankings.items()
            if ranking.mfl_aav is not None
        }
        rank_values_by_source["mfl_aav"] = _ordinal_ranks(aav_values, higher_is_better=True)
    if "sleeper" in sources:
        sleeper_values = _latest_metric_values(db, league_id, "sleeper", "trend_add_24h")
        rank_values_by_source["sleeper"] = _ordinal_ranks(sleeper_values, higher_is_better=True)
    # Schedule difficulty is contextual matchup data, not an overall player-rank signal.
    # It remains available on profiles and in the Bye Advisor, but must not promote a
    # player above actual ranking, projection, ADP, or market sources.
    rank_values_by_source = {
        source_id: values for source_id, values in rank_values_by_source.items() if values
    }
    percentiles: dict[str, dict[str, Decimal]] = {}
    for source_id, values in rank_values_by_source.items():
        ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
        size = len(ordered)
        percentiles[source_id] = {
            player_id: (
                Decimal("1") if size <= 1 else Decimal(size - index - 1) / Decimal(size - 1)
            )
            for index, (player_id, _) in enumerate(ordered)
        }
    availability = availability_maps(db, league_id)
    franchise_names = {
        franchise.id: franchise.name
        for franchise in db.scalars(select(Franchise).where(Franchise.league_id == league_id))
    }
    rows: list[dict[str, Any]] = []
    for player in players:
        ranking = rankings.get(player.id)
        raw_ranks: dict[str, Decimal] = {
            source_id: values[player.id]
            for source_id, values in rank_values_by_source.items()
            if player.id in values
        }
        weighted_total = Decimal("0")
        total_weight = Decimal("0")
        for source_id in raw_ranks:
            weight = Decimal(source_options[source_id]["weight"])
            weighted_total += percentiles[source_id][player.id] * weight
            total_weight += weight
        raw_score = weighted_total / total_weight if total_weight else Decimal("-1")
        source_families = {SOURCE_FAMILIES.get(source_id, source_id) for source_id in raw_ranks}
        family_count = len(source_families)
        confidence_multiplier = {
            0: Decimal("0"),
            1: Decimal("0.72"),
            2: Decimal("0.86"),
        }.get(family_count, Decimal("1"))
        score = raw_score * confidence_multiplier if raw_score >= 0 else raw_score
        if player.position.upper() == "DEF":
            score = score * Decimal("0.08") - Decimal("0.25")
        rank_numbers = [float(value) for value in raw_ranks.values()]
        owner = (
            availability["rostered_by"].get(player.id)
            or availability["purchased_by"].get(player.id)
            or availability["drafted_by"].get(player.id)
        )
        owner_name = franchise_names.get(owner, "Team name unavailable") if owner else None
        preference = preferences.get(player.id)
        nfl_team = canonical_nfl_team(player.nfl_team)
        avoid_team = bool(nfl_team and nfl_team in avoided_team_codes)
        rows.append(
            {
                "player_id": player.id,
                "player_name": player.name,
                "position": player.position,
                "fantasy_positions": player.fantasy_positions_json or [player.position],
                "nfl_team": player.nfl_team,
                "avoid_team": avoid_team,
                "avoid_team_label": f"Avoid · {nfl_team}" if avoid_team else None,
                "status": player.status,
                "injury_status": player.injury_status,
                "practice_participation": player.practice_participation,
                "rookie": player.rookie,
                "bye_week": player.bye_week,
                "consensus_score": str(score.quantize(Decimal("0.0001"))),
                "source_family_count": family_count,
                "source_confidence": (
                    "high" if family_count >= 3 else "medium" if family_count == 2 else "low"
                ),
                "source_count": len(raw_ranks),
                "source_ranks": {key: _display_rank(value) for key, value in raw_ranks.items()},
                "average_rank": round(mean(rank_numbers), 2) if rank_numbers else None,
                "median_rank": round(median(rank_numbers), 2) if rank_numbers else None,
                "best_rank": min(rank_numbers) if rank_numbers else None,
                "worst_rank": max(rank_numbers) if rank_numbers else None,
                "rank_range": max(rank_numbers) - min(rank_numbers) if rank_numbers else None,
                "high_disagreement": (
                    max(rank_numbers) - min(rank_numbers) >= 40 if len(rank_numbers) > 1 else False
                ),
                "league_adjusted_rank": ranking.overall_rank if ranking else None,
                "position_rank": ranking.position_rank if ranking else None,
                "tier": preference.manual_tier
                if preference and preference.manual_tier
                else (ranking.tier if ranking else None),
                "source_tier": fantasypros_raw.get(player.id, {}).get("tier"),
                "custom_score": str(ranking.custom_score) if ranking else None,
                "projected_points": str(ranking.projected_points)
                if ranking and ranking.projected_points is not None
                else None,
                "replacement_points": str(ranking.replacement_points)
                if ranking and ranking.replacement_points is not None
                else None,
                "value_over_replacement": str(ranking.value_over_replacement) if ranking else None,
                "adp": str(ranking.adp) if ranking and ranking.adp is not None else None,
                "mfl_aav": str(ranking.mfl_aav)
                if ranking and ranking.mfl_aav is not None
                else None,
                "baseline_auction_value": str(ranking.baseline_auction_value)
                if ranking and ranking.baseline_auction_value is not None
                else None,
                "live_auction_value": str(ranking.suggested_auction_value)
                if ranking and ranking.suggested_auction_value is not None
                else None,
                "available": player.id not in availability["unavailable"],
                "owner_id": owner,
                "rostered_by": owner_name,
                "keeper": player.id in availability["keepers"],
                "drafted": player.id in availability["drafted_by"],
                "preference": _preference_json(preference),
                "data_updated_at": ranking.created_at.isoformat() if ranking else None,
                "projection_note": (
                    ranking.source_summary_json.get("projection_note") if ranking else None
                ),
            }
        )
    rows.sort(key=lambda row: (-float(row["consensus_score"]), row["league_adjusted_rank"] or 99999, row["player_id"]))
    # Manual ranks are positional moves, not a separate group that jumps ahead of
    # every unedited player. Applying them after the calculated sort makes a single
    # edit behave like moving a row in a conventional draft board.
    moved = sorted(
        (row for row in rows if row["preference"]["manual_rank"] is not None),
        key=lambda row: (int(row["preference"]["manual_rank"]), row["player_id"]),
    )
    for row in moved:
        rows.remove(row)
        target = max(0, min(len(rows), int(row["preference"]["manual_rank"]) - 1))
        rows.insert(target, row)
    for index, row in enumerate(rows, 1):
        row["consensus_rank"] = index
    return rows


def create_consensus_snapshot(
    db: Session, league_id: str, name: str = "Generated consensus"
) -> ConsensusSnapshot:
    weights = {
        source_id: str(option["weight"])
        for source_id, option in effective_source_settings(db).items()
        if option["enabled"]
    }
    snapshot = ConsensusSnapshot(
        league_id=league_id,
        name=name,
        source_weights_json=weights,
        formula_version="consensus-v1",
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


def _source_id(name: str, username: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:12] or "rankings"
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"{personal_source_prefix(username)}{slug}_{digest}"


def parse_ranking_csv(
    db: Session,
    league_id: str,
    content: bytes,
    source_name: str,
    *,
    confirm: bool,
    import_directory: Path,
) -> dict[str, Any]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "player_name" not in reader.fieldnames:
        raise ValueError("CSV must contain a player_name column")
    players = list(db.scalars(select(Player)))
    by_name: dict[str, list[Player]] = {}
    for player in players:
        by_name.setdefault(normalize_player_name(player.name), []).append(player)
    mapped: list[tuple[Player, dict[str, str]]] = []
    unresolved: list[dict[str, str]] = []
    for row in reader:
        name = normalize_player_name(row.get("player_name", ""))
        candidates = by_name.get(name, [])
        team = (row.get("team") or "").upper()
        position = (row.get("position") or "").upper()
        if team:
            candidates = [item for item in candidates if (item.nfl_team or "").upper() == team]
        if position:
            candidates = [item for item in candidates if item.position.upper() == position]
        if len(candidates) != 1 or not row.get("overall_rank"):
            unresolved.append(dict(row))
            continue
        mapped.append((candidates[0], dict(row)))
    result: dict[str, Any] = {
        "source_name": source_name,
        "mapped": len(mapped),
        "unresolved": unresolved,
        "ready": not unresolved and bool(mapped),
    }
    if not confirm:
        return result
    if unresolved:
        raise ValueError("Resolve ambiguous or incomplete rows before importing")
    username = active_username()
    source_id = _source_id(source_name, username)
    source = db.get(DataSource, source_id)
    if source is None:
        source = DataSource(
            id=source_id,
            name=source_name,
            kind="ranking",
            enabled=True,
            weight=Decimal("1"),
            license="Private user upload",
            attribution="Private CSV uploaded through your account",
            cache_ttl_seconds=0,
        )
        db.add(source)
    snapshot_id = hashlib.sha256(content).hexdigest()
    db.execute(
        delete(SourcePlayerValue).where(
            SourcePlayerValue.source_id == source_id,
            SourcePlayerValue.league_id == league_id,
            SourcePlayerValue.value_type == "rank",
        )
    )
    for player, row in mapped:
        raw_rank = Decimal(str(row["overall_rank"]))
        db.add(
            SourcePlayerValue(
                source_id=source_id,
                league_id=league_id,
                player_id=player.id,
                value_type="rank",
                raw_value_json=row,
                normalized_value=raw_rank,
                fetched_at=datetime.now(UTC),
                snapshot_id=snapshot_id,
            )
        )
    import_directory.mkdir(parents=True, exist_ok=True)
    path = import_directory / f"{source_id}_{snapshot_id[:12]}.csv"
    path.write_bytes(content)
    source.last_attempt_at = datetime.now(UTC)
    source.last_success_at = datetime.now(UTC)
    source.last_error = None
    db.commit()
    result.update({"source_id": source_id, "checksum": snapshot_id, "file": path.name})
    return result
