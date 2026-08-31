from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.draft import apply_reconciliation, reconcile_preview
from app.mfl import MFLClient
from app.models import AppSetting, DraftSession, League, LeagueType, MFLSnapshot
from app.power_cache import (
    refresh_power_snapshot_job,
    round_refresh_due,
)
from app.realtime import league_events
from app.settings_store import runtime_settings
from app.source_sync import sync_enabled_sources
from app.sync import sync_league
from app.users import draft_mode, draft_poll_interval

LOGGER = logging.getLogger("uvicorn.error")
# Lightweight scheduler wake-up; per-league settings control actual MFL calls.
LIVE_DRAFT_SYNC_SECONDS = 5


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
        stored_leagues = {
            league.id: LeagueType(league.league_type)
            for league in db.scalars(select(League).where(League.season == settings.mfl_season))
        }
        league_ids = list(
            dict.fromkeys(
                [
                    league_id
                    for league_id in (
                        settings.mfl_keeper_league_id,
                        settings.mfl_auction_league_id,
                    )
                    if league_id
                ]
                + list(stored_leagues)
            )
        )
        started_at = datetime.now(UTC)
        _save_status(db, "auto_sync_last_attempt_at", started_at.isoformat())
        league_results: list[dict[str, Any]] = []
        async with MFLClient(settings) as client:
            for league_id in league_ids:
                try:
                    league_results.append(
                        await sync_league(
                            db, client, settings, league_id, stored_leagues.get(league_id)
                        )
                    )
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


async def sync_live_draft_sessions(db: Session, client: MFLClient, *, respect_intervals: bool = False) -> list[dict[str, Any]]:
    sessions = list(
        db.scalars(
            select(DraftSession)
            .where(DraftSession.status == "live")
            .order_by(DraftSession.league_id)
        )
    )
    sessions = [session for session in sessions if draft_mode(db, session.league_id) == "companion"]
    results: list[dict[str, Any]] = []
    for session in sessions:
        if respect_intervals:
            latest = db.scalar(select(MFLSnapshot).where(MFLSnapshot.league_id == session.league_id, MFLSnapshot.export_type == "draftResults").order_by(MFLSnapshot.fetched_at.desc()).limit(1))
            if latest and latest.fetched_at:
                fetched = latest.fetched_at.replace(tzinfo=UTC) if latest.fetched_at.tzinfo is None else latest.fetched_at
                if (datetime.now(UTC) - fetched).total_seconds() < draft_poll_interval(db, session.league_id):
                    continue
        try:
            response = await client.export(
                "draftResults",
                league_id=session.league_id,
                db=db,
                force=True,
            )
            preview = reconcile_preview(db, session.league_id, response.payload)
            if preview["conflicts"]:
                LOGGER.warning(
                    "Automatic live draft sync paused for league %s: %s conflict(s)",
                    session.league_id,
                    len(preview["conflicts"]),
                )
                results.append(
                    {
                        "league_id": session.league_id,
                        "applied_count": 0,
                        **preview,
                    }
                )
                continue
            applied = apply_reconciliation(db, session.league_id, preview)
            results.append(
                {
                    "league_id": session.league_id,
                    "applied_count": applied,
                    **preview,
                }
            )
            if applied:
                league_events.publish(
                    session.league_id,
                    "mfl-draft-sync",
                    {"applied_count": applied},
                )
                LOGGER.info(
                    "Automatic live draft sync applied %s MFL pick(s) for league %s",
                    applied,
                    session.league_id,
                )
                if round_refresh_due(db, session.league_id, "draft"):
                    await asyncio.to_thread(
                        refresh_power_snapshot_job,
                        session.league_id,
                        "mfl-draft-round-complete",
                    )
        except Exception as exc:
            LOGGER.warning(
                "Automatic live draft sync failed for league %s: %s: %s",
                session.league_id,
                type(exc).__name__,
                exc,
            )
            results.append(
                {
                    "league_id": session.league_id,
                    "applied_count": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return results


async def live_draft_sync_once() -> list[dict[str, Any]]:
    with SessionLocal() as db:
        if not db.scalar(select(DraftSession.id).where(DraftSession.status == "live").limit(1)):
            return []
        settings = runtime_settings(db)
        async with MFLClient(settings) as client:
            return await sync_live_draft_sessions(db, client, respect_intervals=True)


async def live_draft_sync_loop(interval_seconds: int = LIVE_DRAFT_SYNC_SECONDS) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await live_draft_sync_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Automatic live draft synchronization failed")


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
