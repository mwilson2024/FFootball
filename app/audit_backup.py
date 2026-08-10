from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _previous_hash(path: Path) -> str:
    if not path.exists() or path.stat().st_size == 0:
        return "GENESIS"
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 16_384))
        lines = handle.read().splitlines()
    if not lines:
        return "GENESIS"
    try:
        return str(json.loads(lines[-1]).get("event_hash") or "GENESIS")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "UNREADABLE_PREVIOUS_EVENT"


def append_audit_event(
    directory: Path,
    *,
    stream: str,
    action: str,
    league_id: str,
    actor: str,
    entity_id: str | None = None,
    before: Any = None,
    after: Any = None,
    details: Any = None,
) -> dict[str, Any]:
    """Append a tamper-evident, secret-free backup event outside the database."""

    safe_stream = "".join(
        character for character in stream if character.isalnum() or character in "-_"
    )
    if not safe_stream:
        raise ValueError("Audit stream name is invalid")
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{safe_stream}.jsonl"
    with _LOCK:
        previous_hash = _previous_hash(path)
        event = {
            "recorded_at": datetime.now(UTC).isoformat(),
            "stream": safe_stream,
            "action": action,
            "league_id": str(league_id),
            "actor": actor,
            "entity_id": str(entity_id) if entity_id is not None else None,
            "before": _json_safe(before),
            "after": _json_safe(after),
            "details": _json_safe(details),
            "previous_hash": previous_hash,
        }
        canonical = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        event["event_hash"] = hashlib.sha256(f"{previous_hash}\n{canonical}".encode()).hexdigest()
        encoded = (json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return event
