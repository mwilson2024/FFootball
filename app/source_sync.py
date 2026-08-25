from __future__ import annotations

from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import DataSource, League
from app.sources import (
    LOCAL_PROJECTION_SOURCE_IDS,
    LOCAL_RANKING_SOURCE_IDS,
    initialize_sources,
    sync_gng,
    sync_local_projection_source,
    sync_local_ranking_source,
    sync_nflverse,
    sync_sleeper,
)


async def sync_enabled_sources(db: Session, settings: Settings) -> dict[str, Any]:
    """Refresh every built-in enrichment and ranking source without aborting the batch."""
    initialize_sources(db)
    enabled = {
        source.id for source in db.scalars(select(DataSource).where(DataSource.enabled.is_(True)))
    }
    leagues = list(db.scalars(select(League).order_by(League.league_type, League.id)))
    results: list[dict[str, Any]] = []

    async def capture(
        source_id: str,
        operation: Awaitable[Any],
        *,
        league_id: str | None = None,
    ) -> None:
        try:
            result = await operation
            results.append({"source_id": source_id, "league_id": league_id, **result})
        except Exception as exc:
            source = db.get(DataSource, source_id)
            if source is not None:
                source.last_attempt_at = datetime.now(UTC)
                source.last_error = f"{type(exc).__name__}: {exc}"
                db.commit()
            results.append(
                {
                    "source_id": source_id,
                    "league_id": league_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    if "sleeper" in enabled:
        await capture("sleeper", sync_sleeper(db))
    if "nflverse" in enabled:
        await capture("nflverse", sync_nflverse(db, settings.mfl_season))
    if "gng" in enabled:
        for league in leagues:
            await capture(
                "gng",
                sync_gng(db, league.id, league.scoring_rules_json or {}),
                league_id=league.id,
            )

    for source_id in LOCAL_RANKING_SOURCE_IDS:
        if source_id not in enabled:
            continue
        try:
            results.append({"source_id": source_id, **sync_local_ranking_source(db, source_id)})
        except Exception as exc:
            source = db.get(DataSource, source_id)
            if source is not None:
                source.last_attempt_at = datetime.now(UTC)
                source.last_error = f"{type(exc).__name__}: {exc}"
                db.commit()
            results.append({"source_id": source_id, "error": f"{type(exc).__name__}: {exc}"})

    for source_id in LOCAL_PROJECTION_SOURCE_IDS:
        if source_id not in enabled:
            continue
        try:
            results.append({"source_id": source_id, **sync_local_projection_source(db, source_id)})
        except Exception as exc:
            source = db.get(DataSource, source_id)
            if source is not None:
                source.last_attempt_at = datetime.now(UTC)
                source.last_error = f"{type(exc).__name__}: {exc}"
                db.commit()
            results.append({"source_id": source_id, "error": f"{type(exc).__name__}: {exc}"})

    return {
        "sources": results,
        "successful": sum("error" not in item for item in results),
        "failed": sum("error" in item for item in results),
    }
