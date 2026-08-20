from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from threading import Lock
from typing import Any


class LeagueEventBroker:
    """Small single-process event broker for Railway's current one-replica deployment."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._versions: dict[str, int] = {}
        self._events: dict[str, dict[str, Any]] = {}

    def publish(
        self, league_id: str, event_type: str, details: dict[str, Any] | None = None
    ) -> int:
        with self._lock:
            version = self._versions.get(league_id, 0) + 1
            self._versions[league_id] = version
            self._events[league_id] = {
                "league_id": league_id,
                "type": event_type,
                "version": version,
                "at": datetime.now(UTC).isoformat(),
                "details": details or {},
            }
            return version

    def snapshot(self, league_id: str) -> tuple[int, dict[str, Any] | None]:
        with self._lock:
            return self._versions.get(league_id, 0), self._events.get(league_id)

    async def stream(self, league_id: str) -> AsyncIterator[str]:
        version, _ = self.snapshot(league_id)
        yield self._format(
            "ready",
            {
                "league_id": league_id,
                "version": version,
                "at": datetime.now(UTC).isoformat(),
            },
        )
        keepalive = 0
        while True:
            await asyncio.sleep(0.5)
            current, event = self.snapshot(league_id)
            if current != version and event is not None:
                version = current
                keepalive = 0
                yield self._format("league-update", event, event_id=str(version))
                continue
            keepalive += 1
            if keepalive >= 30:
                keepalive = 0
                yield ": keepalive\n\n"

    @staticmethod
    def _format(event: str, data: dict[str, Any], event_id: str | None = None) -> str:
        prefix = f"id: {event_id}\n" if event_id else ""
        return f"{prefix}event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


league_events = LeagueEventBroker()
