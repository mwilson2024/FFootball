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
        self._subscribers: dict[
            str,
            list[
                tuple[
                    asyncio.AbstractEventLoop,
                    asyncio.Queue[tuple[int, dict[str, Any]]],
                ]
            ],
        ] = {}

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
            event = self._events[league_id]
            subscribers = list(self._subscribers.get(league_id, ()))
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(self._offer_latest, queue, (version, event))
            except RuntimeError:
                # A disconnected client can close its event loop between the snapshot and wake-up.
                continue
        return version

    def snapshot(self, league_id: str) -> tuple[int, dict[str, Any] | None]:
        with self._lock:
            return self._versions.get(league_id, 0), self._events.get(league_id)

    async def stream(self, league_id: str) -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[int, dict[str, Any]]] = asyncio.Queue(maxsize=1)
        subscriber = (loop, queue)
        with self._lock:
            version = self._versions.get(league_id, 0)
            self._subscribers.setdefault(league_id, []).append(subscriber)
        try:
            yield self._format(
                "ready",
                {
                    "league_id": league_id,
                    "version": version,
                    "at": datetime.now(UTC).isoformat(),
                },
            )
            while True:
                try:
                    current, event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if current == version:
                    continue
                version = current
                yield self._format("league-update", event, event_id=str(version))
        finally:
            with self._lock:
                subscribers = self._subscribers.get(league_id, [])
                if subscriber in subscribers:
                    subscribers.remove(subscriber)
                if not subscribers:
                    self._subscribers.pop(league_id, None)

    @staticmethod
    def _offer_latest(
        queue: asyncio.Queue[tuple[int, dict[str, Any]]],
        item: tuple[int, dict[str, Any]],
    ) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(item)

    @staticmethod
    def _format(event: str, data: dict[str, Any], event_id: str | None = None) -> str:
        prefix = f"id: {event_id}\n" if event_id else ""
        return f"{prefix}event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


league_events = LeagueEventBroker()
