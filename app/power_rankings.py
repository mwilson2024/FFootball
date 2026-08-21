from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import draftable_consensus
from app.config import Settings
from app.models import Franchise, League

FLEX_POSITIONS = {"RB", "WR", "TE"}
SUPERFLEX_POSITIONS = {"QB", "RB", "WR", "TE"}


def _number(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (ValueError, TypeError):
        return Decimal("0")


def _player_value(row: dict[str, Any]) -> Decimal:
    vor = _number(row.get("value_over_replacement"))
    if vor:
        return vor
    score = _number(row.get("custom_score"))
    if score:
        return score
    rank = int(row.get("consensus_rank") or 500)
    return max(Decimal("0"), Decimal(250 - rank) / Decimal("10"))


def _lineup_requirements(lineup: dict[str, Any]) -> tuple[dict[str, int], int, int]:
    fixed: dict[str, int] = {}
    flex = 0
    superflex = 0
    for raw_position, raw_count in (lineup or {}).items():
        try:
            count = max(0, int(raw_count or 0))
        except (TypeError, ValueError):
            continue
        position = str(raw_position).upper()
        if position == "FLEX":
            flex += count
        elif position == "SUPERFLEX":
            superflex += count
        elif position in {"QB", "RB", "WR", "TE", "PK", "K", "DEF", "DST", "D/ST"}:
            normalized = (
                "PK" if position == "K" else "DEF" if position in {"DST", "D/ST"} else position
            )
            fixed[normalized] = fixed.get(normalized, 0) + count
    return fixed, flex, superflex


def build_power_rankings(
    db: Session,
    league_id: str,
    *,
    board: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    league = db.scalar(select(League).where(League.id == league_id).order_by(League.season.desc()))
    if league is None:
        raise ValueError("League not found")
    franchises = list(
        db.scalars(
            select(Franchise).where(Franchise.league_id == league_id).order_by(Franchise.name)
        )
    )
    board = board if board is not None else draftable_consensus(db, league_id)
    fixed, flex_slots, superflex_slots = _lineup_requirements(league.lineup_json or {})
    teams: list[dict[str, Any]] = []
    for franchise in franchises:
        roster = [row for row in board if row.get("owner_id") == franchise.id]
        roster.sort(key=lambda row: (-_player_value(row), int(row.get("consensus_rank") or 99999)))
        selected: set[str] = set()
        needs: dict[str, int] = {}
        starter_value = Decimal("0")
        position_values: dict[str, Decimal] = {}
        for position, required in fixed.items():
            candidates = [
                row
                for row in roster
                if row["player_id"] not in selected
                and position in {str(item).upper() for item in row.get("fantasy_positions", [])}
            ]
            chosen = candidates[:required]
            needs[position] = max(0, required - len(chosen))
            for row in chosen:
                selected.add(row["player_id"])
                value = _player_value(row)
                starter_value += value
                position_values[position] = position_values.get(position, Decimal("0")) + value
        for label, count, eligible in (
            ("FLEX", flex_slots, FLEX_POSITIONS),
            ("SUPERFLEX", superflex_slots, SUPERFLEX_POSITIONS),
        ):
            candidates = [
                row
                for row in roster
                if row["player_id"] not in selected
                and eligible.intersection(
                    {str(item).upper() for item in row.get("fantasy_positions", [])}
                )
            ]
            chosen = candidates[:count]
            needs[label] = max(0, count - len(chosen))
            for row in chosen:
                selected.add(row["player_id"])
                value = _player_value(row)
                starter_value += value
                position_values[label] = position_values.get(label, Decimal("0")) + value
        bench = [row for row in roster if row["player_id"] not in selected]
        depth_value = sum((_player_value(row) for row in bench[:8]), Decimal("0")) * Decimal("0.25")
        missing = sum(needs.values())
        raw_score = starter_value + depth_value - Decimal(missing * 5)
        strongest = (
            max(position_values, key=lambda position: position_values[position])
            if position_values
            else None
        )
        need_labels = [f"{count} {position}" for position, count in needs.items() if count]
        teams.append(
            {
                "franchise_id": franchise.id,
                "franchise_name": franchise.name,
                "roster_size": len(roster),
                "starter_value": float(starter_value.quantize(Decimal("0.01"))),
                "depth_value": float(depth_value.quantize(Decimal("0.01"))),
                "raw_score": raw_score,
                "position_counts": {
                    position: sum(1 for row in roster if row["position"].upper() == position)
                    for position in sorted({row["position"].upper() for row in roster})
                },
                "needs": needs,
                "summary": (
                    f"Best starting value at {strongest}; " if strongest else "No starters filled; "
                )
                + (
                    f"still needs {', '.join(need_labels)}."
                    if need_labels
                    else "starting lineup covered."
                ),
            }
        )
    teams.sort(key=lambda team: (-team["raw_score"], team["franchise_name"].casefold()))
    raw_values = [team["raw_score"] for team in teams]
    minimum = min(raw_values, default=Decimal("0"))
    maximum = max(raw_values, default=Decimal("0"))
    spread = maximum - minimum
    for rank, team in enumerate(teams, 1):
        team["rank"] = rank
        team["power_score"] = float(
            (
                Decimal("70")
                if not spread
                else Decimal("50") + (team["raw_score"] - minimum) / spread * Decimal("50")
            ).quantize(Decimal("0.1"))
        )
        del team["raw_score"]
    return {
        "league": {"id": league.id, "name": league.name, "season": league.season},
        "methodology": (
            "Starting-lineup value from the league's configured positions, plus 25% of the top "
            "eight bench values, minus five points for every unfilled starter."
        ),
        "rankings": teams,
    }


async def chatgpt_power_rankings(
    db: Session,
    settings: Settings,
    league_id: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    baseline = build_power_rankings(db, league_id)
    context = [
        {
            "model_rank": team["rank"],
            "team": team["franchise_name"],
            "starter_value": team["starter_value"],
            "depth_value": team["depth_value"],
            "position_counts": team["position_counts"],
            "needs": team["needs"],
        }
        for team in baseline["rankings"]
    ]
    payload = {
        "model": settings.openai_model,
        "instructions": (
            "You are judging fantasy-football roster power rankings. Re-rank every supplied team. "
            "Use only supplied values, roster construction, depth, and league needs. Be concise, "
            "identify uncertainty, and return a numbered ranking with one sentence per team."
        ),
        "input": f"League: {baseline['league']['name']}\nTeams: {json.dumps(context)}",
        "max_output_tokens": 1200,
    }
    async with httpx.AsyncClient(timeout=45, transport=transport) as client:
        response = await client.post(
            f"{settings.openai_base_url.rstrip('/')}/responses",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json=payload,
        )
    response.raise_for_status()
    data = response.json()
    analysis = data.get("output_text")
    if not analysis:
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    analysis = content["text"]
                    break
    if not analysis:
        raise RuntimeError("ChatGPT returned no power-ranking analysis")
    return {"model": settings.openai_model, "analysis": str(analysis), "baseline": baseline}
