from __future__ import annotations

import json
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auction import franchise_budget
from app.catalog import draftable_consensus
from app.config import Settings
from app.models import Franchise, League, Player, RosterAssignment
from app.users import league_setting


def _league_context(db: Session, league_id: str) -> dict[str, Any]:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise ValueError("League not found")
    setting = league_setting(db, league_id)
    roster: list[dict[str, str | None]] = []
    budget: dict[str, Any] | None = None
    franchise = db.get(Franchise, setting.franchise_id) if setting.franchise_id else None
    if franchise and franchise.league_id == league_id:
        roster = [
            {"name": player.name, "position": player.position, "team": player.nfl_team}
            for player in db.scalars(
                select(Player)
                .join(RosterAssignment, RosterAssignment.player_id == Player.id)
                .where(
                    RosterAssignment.league_id == league_id,
                    RosterAssignment.franchise_id == franchise.id,
                )
                .limit(80)
            )
        ]
        if league.starting_budget is not None:
            raw = franchise_budget(db, league, franchise)
            budget = {
                "remaining": str(raw["remaining"]),
                "spent": str(raw["spent"]),
                "slots_remaining": raw["slots_remaining"],
            }
    return {
        "league": {"name": league.name, "type": league.league_type, "season": league.season},
        "my_franchise": franchise.name if franchise else None,
        "my_roster": roster,
        "auction_budget": budget,
        "lineup": league.lineup_json,
        "scoring_rules": league.scoring_rules_json,
        "current_board": [
            {
                "rank": row["consensus_rank"],
                "name": row["player_name"],
                "position": row["position"],
                "tier": row["tier"],
                "available": row["available"],
                "target": row["preference"]["target"],
                "sleeper": "sleeper" in row["preference"].get("tags", []),
                "dynamic_bid": row.get("dynamic_bid"),
            }
            for row in draftable_consensus(db, league_id)[:40]
        ],
    }


async def ask_assistant(
    db: Session, settings: Settings, league_id: str, message: str, history: list[dict[str, str]]
) -> str:
    if not settings.openai_api_key:
        raise RuntimeError("The league assistant is not configured yet")
    context = _league_context(db, league_id)
    instructions = (
        "You are a concise fantasy-football draft assistant. Use only the supplied league "
        "context for roster, budget, lineup, and scoring claims. State uncertainty clearly. "
        "Never claim to submit a bid or draft pick."
    )
    prior = [
        {"role": item.get("role", "user"), "content": item.get("content", "")[:2000]}
        for item in history[-10:]
        if item.get("role") in {"user", "assistant"}
    ]
    payload = {
        "model": settings.openai_model,
        "instructions": instructions,
        "input": [
            {"role": "developer", "content": f"League context: {json.dumps(context, default=str)}"},
            *prior,
            {"role": "user", "content": message},
        ],
        "max_output_tokens": 700,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{settings.openai_base_url.rstrip('/')}/responses",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json=payload,
        )
    response.raise_for_status()
    data = response.json()
    if data.get("output_text"):
        return str(data["output_text"])
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                return str(content["text"])
    raise RuntimeError("The assistant returned no answer")
