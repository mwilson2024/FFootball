from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.mfl import MFLClient
from app.models import AppSetting
from app.settings_store import runtime_settings
from app.source_sync import sync_enabled_sources
from app.sync import sync_league

LOGGER = logging.getLogger("uvicorn.error")


def next_daily_sync(
    now: datetime,
    *,
    timezone_name: str = "America/New_York",
    hour: int = 1,
) -> datetime:
    timezone = ZoneInfo(timezone_name)
    local_now = now.astimezone(timezone)
    candidate = datetime.combine(local_now.date(), time(hour=hour), tzinfo=timezone)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def _save_status(db: Session, key: str, value: str) -> None:
    item = db.get(AppSetting, key)
    if item is None:
        db.add(AppSetting(key=key, value=value))
    else:
        item.value = value
        item.updated_at = datetime.now(UTC)
    db.commit()


async def automatic_sync_once() -> dict[str, Any]:
    with SessionLocal() as db:
        settings = runtime_settings(db)
        league_ids = list(
            dict.fromkeys(
                league_id
                for league_id in (
                    settings.mfl_keeper_league_id,
                    settings.mfl_auction_league_id,
                )
                if league_id
            )
        )
        started_at = datetime.now(UTC)
        _save_status(db, "auto_sync_last_attempt_at", started_at.isoformat())
        league_results: list[dict[str, Any]] = []
        async with MFLClient(settings) as client:
            for league_id in league_ids:
                try:
                    league_results.append(await sync_league(db, client, settings, league_id))
                except Exception as exc:
                    LOGGER.warning(
                        "Automatic MFL sync failed for league %s: %s: %s",
                        league_id,
                        type(exc).__name__,
                        exc,
                    )
                    league_results.append(
                        {
                            "league_id": league_id,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
        source_results = await sync_enabled_sources(db, settings)
        completed_at = datetime.now(UTC)
        _save_status(db, "auto_sync_last_completed_at", completed_at.isoformat())
        LOGGER.info(
            "Automatic daily sync completed: %s league(s), %s source job(s), %s failure(s)",
            len(league_results),
            source_results["successful"],
            source_results["failed"] + sum("error" in item for item in league_results),
        )
        return {"leagues": league_results, **source_results}


async def daily_sync_loop(settings: Settings | None = None) -> None:
    configured = settings or get_settings()
    while True:
        scheduled_for = next_daily_sync(
            datetime.now(UTC),
            timezone_name=configured.auto_sync_timezone,
            hour=configured.auto_sync_hour,
        )
        delay = max(0.0, (scheduled_for - datetime.now(UTC)).total_seconds())
        LOGGER.info(
            "Next automatic TMFL/ADFL sync is scheduled for %s (%s)",
            scheduled_for.isoformat(),
            configured.auto_sync_timezone,
        )
        await asyncio.sleep(delay)
        try:
            await automatic_sync_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Automatic daily sync failed")


async def stop_daily_sync(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
