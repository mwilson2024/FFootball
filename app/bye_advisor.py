from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import draftable_consensus, live_roster_ownership
from app.models import Franchise, League, Player, SourcePlayerValue
from app.users import league_setting


def _schedule_values(db: Session) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for row in db.scalars(
        select(SourcePlayerValue)
        .where(
            SourcePlayerValue.source_id == "nflverse",
            SourcePlayerValue.value_type == "schedule",
        )
        .order_by(SourcePlayerValue.fetched_at.desc(), SourcePlayerValue.id.desc())
    ):
        values.setdefault(row.player_id, row.raw_value_json or {})
    return values


def _week_game(schedule: dict[str, Any], week: int) -> dict[str, Any] | None:
    return next(
        (
            game
            for game in schedule.get("games", [])
            if isinstance(game, dict) and int(game.get("week") or 0) == week
        ),
        None,
    )


def _is_defense(position: str) -> bool:
    return position.upper() in {"DEF", "DST", "D/ST"}


def _fallback_matchup_quality(schedule: dict[str, Any]) -> float:
    try:
        rank = int(schedule.get("schedule_rank") or 0)
        total = int(str(schedule.get("schedule_rank_label") or "").split(" of ")[-1])
    except (TypeError, ValueError):
        return 0.5
    if rank <= 0 or total <= 1:
        return 0.5
    return max(0.0, min(1.0, 1 - ((rank - 1) / (total - 1))))


def _matchup_label(quality: float, known: bool) -> str:
    if not known:
        return "Unknown"
    if quality >= 0.75:
        return "Great"
    if quality >= 0.55:
        return "Good"
    if quality >= 0.35:
        return "Neutral"
    return "Tough"


def bye_week_advice(
    db: Session, league_id: str, week: int, player_id: str | None = None
) -> dict[str, Any]:
    league = db.scalar(select(League).where(League.id == league_id).order_by(League.season.desc()))
    if league is None:
        raise ValueError("League not found")
    setting = league_setting(db, league_id)
    franchise = (
        db.scalar(
            select(Franchise).where(
                Franchise.league_id == league_id,
                Franchise.id == setting.franchise_id,
            )
        )
        if setting.franchise_id
        else None
    )
    base = {
        "league": {"id": league.id, "name": league.name, "type": league.league_type},
        "selected_week": week,
        "selected_player_id": player_id,
        "franchise": (
            {"id": franchise.id, "name": franchise.name} if franchise is not None else None
        ),
        "methodology": {
            "overall_rank_weight": 70,
            "weekly_matchup_weight": 30,
            "matchup_source": "nflverse schedule and opponent prior-season scoring",
        },
    }
    if franchise is None:
        return {
            **base,
            "configured": False,
            "week_summary": [],
            "roster": [],
            "conflicts": [],
            "schedule_available": False,
        }

    board = draftable_consensus(db, league_id)
    board_by_id = {row["player_id"]: row for row in board}
    schedules = _schedule_values(db)
    assignments = live_roster_ownership(db, league_id).get(franchise.id, {}).values()
    roster: list[dict[str, Any]] = []
    for assignment in assignments:
        player = db.get(Player, assignment["player_id"])
        if player is None:
            continue
        status = str(assignment["status"] or "ROSTER").upper()
        if any(label in status for label in ("INJURED", "TAXI", "PRACTICE")):
            continue
        row = board_by_id.get(player.id, {})
        roster.append(
            {
                "player_id": player.id,
                "player_name": player.name,
                "position": player.position,
                "nfl_team": player.nfl_team,
                "bye_week": player.bye_week,
                "overall_rank": row.get("consensus_rank"),
                "status": assignment["status"],
            }
        )

    week_summary = [
        {
            "week": item_week,
            "bye_count": sum(1 for player in roster if player["bye_week"] == item_week),
            "players": [
                player["player_name"] for player in roster if player["bye_week"] == item_week
            ],
        }
        for item_week in range(1, 19)
    ]
    manually_selected = next(
        (player for player in roster if player["player_id"] == player_id), None
    )
    conflicts = (
        [manually_selected]
        if manually_selected is not None
        else [player for player in roster if player["bye_week"] == week]
    )
    recommendations: dict[str, list[dict[str, Any]]] = {}

    for conflict in conflicts:
        position = str(conflict["position"]).upper()
        candidates: list[dict[str, Any]] = []
        for row in board:
            if not row.get("available") or str(row.get("position", "")).upper() != position:
                continue
            if row.get("bye_week") == week:
                continue
            schedule = schedules.get(str(row["player_id"]), {})
            game = _week_game(schedule, week)
            if schedule.get("games") and game is None:
                continue
            metric_name = (
                "defense_matchup_score" if _is_defense(position) else "offense_matchup_score"
            )
            raw_metric = game.get(metric_name) if game else None
            try:
                matchup_metric = float(raw_metric) if raw_metric is not None else None
            except (TypeError, ValueError):
                matchup_metric = None
            candidates.append(
                {
                    "row": row,
                    "schedule": schedule,
                    "game": game,
                    "matchup_metric": matchup_metric,
                }
            )

        measured = [item for item in candidates if item["matchup_metric"] is not None]
        unique_metrics = sorted(
            {float(item["matchup_metric"]) for item in measured},
            reverse=not _is_defense(position),
        )
        metric_ranks = {metric: index for index, metric in enumerate(unique_metrics, 1)}
        for item in candidates:
            row = item["row"]
            overall_rank = int(row.get("consensus_rank") or 99999)
            rank_quality = 1 / (1 + (max(1, overall_rank) - 1) / 35)
            matchup_rank = (
                metric_ranks.get(float(item["matchup_metric"]))
                if item["matchup_metric"] is not None
                else None
            )
            if matchup_rank is not None:
                matchup_quality = (
                    1
                    if len(unique_metrics) <= 1
                    else 1 - ((matchup_rank - 1) / (len(unique_metrics) - 1))
                )
                matchup_known = True
            else:
                matchup_quality = _fallback_matchup_quality(item["schedule"])
                matchup_known = bool(item["schedule"])
            score = rank_quality * 0.70 + matchup_quality * 0.30
            game = item["game"] or {}
            item["result"] = {
                "player_id": row["player_id"],
                "player_name": row["player_name"],
                "position": row["position"],
                "nfl_team": row.get("nfl_team"),
                "overall_rank": overall_rank,
                "tier": row.get("tier"),
                "projected_points": row.get("projected_points"),
                "opponent": game.get("opponent"),
                "home_away": game.get("home_away"),
                "game_date": game.get("date"),
                "matchup_rank": matchup_rank,
                "matchup_pool": len(unique_metrics),
                "matchup_label": _matchup_label(matchup_quality, matchup_known),
                "recommendation_score": round(score * 100, 1),
            }
        candidates.sort(
            key=lambda item: (
                -float(item["result"]["recommendation_score"]),
                int(item["result"]["overall_rank"]),
                str(item["result"]["player_id"]),
            )
        )
        recommendations[str(conflict["player_id"])] = [item["result"] for item in candidates[:5]]

    return {
        **base,
        "configured": True,
        "week_summary": week_summary,
        "roster": sorted(
            roster,
            key=lambda player: (
                player["bye_week"] or 99,
                player["overall_rank"] or 99999,
                player["player_name"],
            ),
        ),
        "conflicts": [
            {
                **conflict,
                "is_bye": conflict["bye_week"] == week,
                "selected_manually": manually_selected is not None,
                "recommendations": recommendations[str(conflict["player_id"])],
            }
            for conflict in conflicts
        ],
        "schedule_available": bool(schedules),
        "weekly_matchups_available": any(
            game.get("offense_matchup_score") is not None
            or game.get("defense_matchup_score") is not None
            for schedule in schedules.values()
            for game in schedule.get("games", [])
            if isinstance(game, dict)
        ),
    }
