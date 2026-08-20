from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import DataSource, League, Player, SourcePlayerValue

STAT_KEYS = {
    "#P": ("passing_tds", "passing_touchdowns"),
    "PY": ("passing_yards",),
    "IN": ("interceptions", "passing_interceptions", "passing_ints"),
    "P2": ("passing_2pt_conversions", "passing_two_point_conversions"),
    "#R": ("rushing_tds", "rushing_touchdowns"),
    "RY": ("rushing_yards",),
    "R2": ("rushing_2pt_conversions", "rushing_two_point_conversions"),
    "#C": ("receiving_tds", "receiving_touchdowns"),
    "CY": ("receiving_yards",),
    "CC": ("receptions",),
    "C2": ("receiving_2pt_conversions", "receiving_two_point_conversions"),
    "FL": ("fumbles_lost",),
    "#UT": ("punt_return_tds",),
    "#KT": ("kickoff_return_tds",),
    "EP": ("extra_points_made", "pat_made"),
    "FC": ("def_fumbles", "fumble_recoveries"),
    "IC": ("def_interceptions",),
    "SK": ("def_sacks", "sacks"),
    "SF": ("def_safeties", "safeties"),
    "#T": ("def_tds", "special_teams_tds"),
}

VOLUME_KEYS = {
    "QB": (("attempts", "passing_attempts"), ("carries", "rushing_attempts")),
    "RB": (("carries", "rushing_attempts"), ("targets",)),
    "WR": (("targets",), ("carries", "rushing_attempts")),
    "TE": (("targets",),),
    "PK": (("field_goal_attempts",), ("extra_point_attempts",)),
    "DEF": (("def_snaps",),),
}

VOLUME_CEILINGS = {"QB": 650, "RB": 330, "WR": 180, "TE": 130, "PK": 80, "DEF": 1100}


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "")) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _first_number(value: Any) -> float | None:
    match = re.search(r"[-+]?\d*\.?\d+", str(value or ""))
    return float(match.group()) if match else None


def _position_allowed(rule: dict[str, Any], position: str) -> bool:
    raw = str(rule.get("positions") or "ALL").upper()
    if raw in {"", "ALL"}:
        return True
    aliases = {"K": "PK", "DST": "DEF", "D/ST": "DEF"}
    allowed = {aliases.get(item, item) for item in re.split(r"[,|+]", raw) if item}
    return aliases.get(position.upper(), position.upper()) in allowed


def _stat(stats: dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        value = _number(stats.get(key))
        if value is not None:
            return value
    return 0.0


def _rule_points(rule: dict[str, Any], stat_value: float) -> float:
    points = _first_number(rule.get("points"))
    if points is None:
        return 0.0
    raw_points = str(rule.get("points") or "").lower()
    event = str(rule.get("event") or "")
    is_multiplier = (
        raw_points.strip().startswith("*")
        or "each" in raw_points
        or event
        in {
            "#P",
            "IN",
            "P2",
            "#R",
            "R2",
            "#C",
            "CC",
            "C2",
            "FL",
            "#UT",
            "#KT",
            "EP",
            "FC",
            "IC",
            "SK",
            "SF",
            "#T",
        }
    )
    if is_multiplier:
        return stat_value * points
    range_match = re.match(
        r"^\s*(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*$",
        str(rule.get("range") or ""),
    )
    if range_match and not (
        float(range_match.group(1)) <= stat_value <= float(range_match.group(2))
    ):
        return 0.0
    # Flat range bonuses are defined per MFL scoring period. Season aggregates cannot
    # reproduce how many weeks crossed the threshold, so leave them out rather than
    # manufacturing precision from a season total.
    return 0.0 if range_match else points


def score_historical_stats(
    stats: dict[str, Any], scoring_rules: dict[str, Any], position: str
) -> tuple[float | None, int]:
    """Score historical nflverse totals with the league's imported MFL rule rows."""
    total = 0.0
    mapped = 0
    for rule in scoring_rules.values():
        if not isinstance(rule, dict) or not _position_allowed(rule, position):
            continue
        event = str(rule.get("event") or "")
        keys = STAT_KEYS.get(event)
        if not keys:
            continue
        total += _rule_points(rule, _stat(stats, keys))
        mapped += 1
    if mapped:
        return round(total, 3), mapped
    fallback = _number(stats.get("fantasy_points_ppr"))
    if fallback is None:
        fallback = _number(stats.get("fantasy_points"))
    if fallback is None:
        return None, 0
    receptions = _number(stats.get("receptions")) or 0.0
    target_ppr = _number(scoring_rules.get("receptions"))
    if target_ppr is not None and stats.get("fantasy_points_ppr") not in (None, ""):
        fallback += receptions * (target_ppr - 1.0)
    return round(fallback, 3), 0


def _latest_values(
    db: Session, league_id: str
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    values = list(
        db.scalars(
            select(SourcePlayerValue)
            .where(
                or_(SourcePlayerValue.league_id == league_id, SourcePlayerValue.league_id.is_(None))
            )
            .order_by(SourcePlayerValue.fetched_at.desc(), SourcePlayerValue.id.desc())
        )
    )
    historical: dict[str, dict[str, Any]] = {}
    raw_sources: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str, str]] = set()
    names = {item.id: item.name for item in db.scalars(select(DataSource))}
    for value in values:
        key = (value.source_id, value.player_id, value.value_type)
        if key in seen:
            continue
        seen.add(key)
        raw = value.raw_value_json or {}
        if value.source_id == "nflverse" and value.value_type == "player_stats":
            historical[value.player_id] = raw
        raw_sources[value.player_id].append(
            {
                "source_id": value.source_id,
                "source_name": names.get(value.source_id, value.source_id),
                "value_type": value.value_type,
                "raw": raw,
                "fetched_at": value.fetched_at.isoformat() if value.fetched_at else None,
            }
        )
    return historical, raw_sources


def _external_projection(sources: list[dict[str, Any]]) -> tuple[float | None, list[str]]:
    values: list[float] = []
    labels: list[str] = []
    for source in sources:
        raw = source["raw"]
        if not isinstance(raw, dict):
            continue
        for key in ("season_projection", "projected_points", "projection", "proj_points", "points"):
            value = _number(raw.get(key))
            if value is not None and value > 0:
                values.append(value)
                labels.append(source["source_name"])
                break
    return (sum(values) / len(values), list(dict.fromkeys(labels))) if values else (None, [])


def _workload(stats: dict[str, Any], position: str) -> float:
    volume = 0.0
    groups = VOLUME_KEYS.get(position, ())
    for index, keys in enumerate(groups):
        amount = _stat(stats, keys)
        volume += amount if index == 0 else amount * 0.7
    ceiling = VOLUME_CEILINGS.get(position, 250)
    return round(max(0.0, min(100.0, volume / ceiling * 100)), 1)


def _injury_risk(player: Player, stats: dict[str, Any]) -> tuple[int, str]:
    games = _number(stats.get("games")) or _number(stats.get("games_played"))
    probability = 14.0
    if games is not None:
        probability += max(0.0, 17.0 - games) * 1.8
    status = f"{player.injury_status or ''} {player.status or ''}".upper()
    if any(token in status for token in ("IR", "OUT", "PUP")):
        probability += 28
    elif any(token in status for token in ("DOUBTFUL", "QUESTIONABLE", "LIMITED")):
        probability += 13
    probability = int(round(max(7, min(65, probability))))
    return probability, "High" if probability >= 35 else "Moderate" if probability >= 22 else "Low"


def build_projection_board(
    db: Session, league: League, board: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Build transparent internal season distributions, not vendor projections."""
    players = {item.id: item for item in db.scalars(select(Player))}
    historical, source_rows = _latest_values(db, league.id)
    result: dict[str, dict[str, Any]] = {}
    pool = max(len(board), 1)
    for row in board:
        player = players.get(str(row["player_id"]))
        if player is None:
            continue
        raw_history = historical.get(player.id, {})
        raw_stats = raw_history.get("stats")
        stats: dict[str, Any] = raw_stats if isinstance(raw_stats, dict) else {}
        historical_points, mapped_rules = score_historical_stats(
            stats, league.scoring_rules_json or {}, player.position.upper()
        )
        external, external_sources = _external_projection(source_rows.get(player.id, []))
        rank = int(row.get("consensus_rank") or pool)
        rank_baseline = max(24.0, 310.0 * math.exp(-2.25 * (rank - 1) / pool))
        if external is not None and historical_points is not None:
            median = external * 0.65 + historical_points * 0.35
            basis = "Imported season projection blended with prior-year league-scored production"
            confidence = 84
        elif external is not None:
            median = external
            basis = "Imported full-season projection"
            confidence = 78
        elif historical_points is not None:
            median = historical_points * 0.82 + rank_baseline * 0.18
            basis = (
                "Prior-year production recalculated under this league's scoring, "
                "regressed toward role"
            )
            confidence = 72 if mapped_rules else 62
        else:
            median = rank_baseline
            basis = "Role-and-consensus fallback; no usable season stat line was available"
            confidence = 38
        workload = _workload(stats, player.position.upper())
        workload_basis = "Prior-season opportunities"
        if workload <= 0:
            workload = round(max(10.0, min(88.0, 88.0 - (rank - 1) / pool * 70.0)), 1)
            workload_basis = "Current consensus role estimate"
        if player.rookie and historical_points is None:
            confidence = min(confidence, 34)
        risk_probability, risk_band = _injury_risk(player, stats)
        uncertainty = 0.16 + (100 - confidence) / 500
        floor = max(0.0, median * (0.76 - uncertainty / 2))
        ceiling = median * (1.16 + uncertainty / 2)
        source_labels = list(external_sources)
        if historical_points is not None:
            source_labels.append("nflverse historical stats")
        if mapped_rules:
            source_labels.append("Imported MFL scoring rules")
        source_labels.append("DraftDesk role model")
        result[player.id] = {
            "median": round(median, 1),
            "ceiling": round(ceiling, 1),
            "floor": round(floor, 1),
            "workload": workload,
            "workload_basis": workload_basis,
            "injury_risk_probability": risk_probability,
            "injury_risk": risk_band,
            "confidence": confidence,
            "confidence_label": "High"
            if confidence >= 75
            else "Medium"
            if confidence >= 55
            else "Low",
            "basis": basis,
            "sources": list(dict.fromkeys(source_labels)),
            "historical_season": raw_history.get("season"),
            "mapped_scoring_rules": mapped_rules,
            "model_version": "season-outcomes-v1",
        }
    return result


def lineup_projection(
    player_ids: set[str] | list[str],
    board_by_id: dict[str, dict[str, Any]],
    projections: dict[str, dict[str, Any]],
    lineup: dict[str, Any],
) -> dict[str, Any]:
    rows = [
        (player_id, board_by_id.get(player_id, {}), projections.get(player_id, {}))
        for player_id in player_ids
        if player_id in projections
    ]
    rows.sort(key=lambda item: (-float(item[2].get("median") or 0), item[0]))
    selected: set[str] = set()
    by_position: dict[str, float] = defaultdict(float)
    missing: dict[str, int] = {}
    for raw_position, raw_count in (lineup or {}).items():
        position = str(raw_position).upper()
        if position in {"FLEX", "SUPERFLEX", "QB_FLEX"}:
            continue
        try:
            count = max(0, int(raw_count or 0))
        except (TypeError, ValueError):
            continue
        aliases = {"K": "PK", "DST": "DEF", "D/ST": "DEF", "D": "DEF"}
        eligible = {
            aliases.get(item.strip(), item.strip())
            for item in re.split(r"[,|+]", position)
            if item.strip()
        }
        normalized = aliases.get(position, position)
        candidates = [
            item
            for item in rows
            if item[0] not in selected
            and aliases.get(
                str(item[1].get("position") or "").upper(),
                str(item[1].get("position") or "").upper(),
            )
            in eligible
        ]
        chosen = candidates[:count]
        missing[normalized] = max(0, count - len(chosen))
        for player_id, _, projection in chosen:
            selected.add(player_id)
            by_position[normalized] += float(projection.get("median") or 0)
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
            item
            for item in rows
            if item[0] not in selected and str(item[1].get("position") or "").upper() in eligible
        ]
        chosen = candidates[:count]
        missing[label] = max(0, count - len(chosen))
        for player_id, _, projection in chosen:
            selected.add(player_id)
            by_position[label] += float(projection.get("median") or 0)
    starter = sum(by_position.values())
    bench = sum(float(item[2].get("median") or 0) for item in rows if item[0] not in selected)
    return {
        "projected_starter_points": round(starter, 1),
        "depth_points": round(bench * 0.12, 1),
        "roster_strength": round(starter + bench * 0.12 - sum(missing.values()) * 12, 1),
        "position_points": {key: round(value, 1) for key, value in by_position.items()},
        "missing_starters": {key: value for key, value in missing.items() if value},
    }
