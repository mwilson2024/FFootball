from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings

SESSION_COOKIE = "draftdesk_session"
_LOGIN_WINDOW_SECONDS = 10 * 60
_LOGIN_MAX_FAILURES = 5
_login_failures: dict[str, deque[float]] = defaultdict(deque)


@dataclass(frozen=True)
class UserSession:
    username: str
    league_ids: tuple[str, ...]
    csrf_token: str
    issued_at: int
    expires_at: int


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def resolve_session_secret(settings: Settings) -> str:
    configured = settings.session_secret.strip()
    if configured:
        if len(configured) < 32:
            raise RuntimeError("SESSION_SECRET must contain at least 32 characters")
        return configured
    if settings.app_env.lower() in {"production", "prod"}:
        raise RuntimeError("SESSION_SECRET is required in production")

    path = Path("data/.session_secret")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    generated = secrets.token_urlsafe(48)
    path.write_text(generated, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return generated


def make_session_token(
    secret: str,
    username: str,
    league_ids: set[str] | list[str] | tuple[str, ...],
    *,
    max_age_seconds: int,
    now: int | None = None,
) -> tuple[str, UserSession]:
    issued_at = int(time.time() if now is None else now)
    session = UserSession(
        username=username,
        league_ids=tuple(sorted(str(item) for item in league_ids)),
        csrf_token=secrets.token_urlsafe(24),
        issued_at=issued_at,
        expires_at=issued_at + max_age_seconds,
    )
    payload = {
        "u": session.username,
        "l": list(session.league_ids),
        "c": session.csrf_token,
        "iat": session.issued_at,
        "exp": session.expires_at,
    }
    encoded = _encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = _encode(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{signature}", session


def read_session_token(
    secret: str, token: str | None, *, now: int | None = None
) -> UserSession | None:
    if not token or "." not in token:
        return None
    encoded, supplied_signature = token.rsplit(".", 1)
    expected_signature = _encode(
        hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return None
    try:
        payload = json.loads(_decode(encoded))
        current = int(time.time() if now is None else now)
        session = UserSession(
            username=str(payload["u"]),
            league_ids=tuple(str(item) for item in payload["l"]),
            csrf_token=str(payload["c"]),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if session.expires_at <= current or session.issued_at > current + 60:
        return None
    return session


def mfl_league_ids(payload: Any) -> set[str]:
    return {
        str(item["league_id"]) for item in mfl_memberships(payload) if item["league_id"] is not None
    }


def mfl_memberships(payload: Any) -> list[dict[str, str | None]]:
    found: dict[str, dict[str, str | None]] = {}

    def walk(value: Any, parent_key: str = "") -> None:
        if isinstance(value, dict):
            if parent_key.lower() == "league":
                league_id = value.get("id") or value.get("league_id") or value.get("leagueId")
                if league_id is not None:
                    normalized = str(league_id)
                    franchise = (
                        value.get("franchise_id")
                        or value.get("franchiseId")
                        or value.get("franchise")
                    )
                    if isinstance(franchise, dict):
                        franchise = franchise.get("id") or franchise.get("franchise_id")
                    found[normalized] = {
                        "league_id": normalized,
                        "league_name": str(value.get("name")) if value.get("name") else None,
                        "franchise_id": str(franchise) if franchise not in (None, "") else None,
                        "source_url": str(value.get("url") or value.get("baseURL"))
                        if value.get("url") or value.get("baseURL")
                        else None,
                    }
            for key, child in value.items():
                walk(child, str(key))
        elif isinstance(value, list):
            for child in value:
                walk(child, parent_key)

    walk(payload)
    return list(found.values())


def login_rate_key(client_ip: str, username: str) -> str:
    digest = hashlib.sha256(username.strip().lower().encode()).hexdigest()[:16]
    return f"{client_ip}:{digest}"


def login_allowed(key: str, *, now: float | None = None) -> bool:
    current = time.time() if now is None else now
    failures = _login_failures[key]
    while failures and failures[0] <= current - _LOGIN_WINDOW_SECONDS:
        failures.popleft()
    return len(failures) < _LOGIN_MAX_FAILURES


def record_login_failure(key: str, *, now: float | None = None) -> None:
    _login_failures[key].append(time.time() if now is None else now)


def clear_login_failures(key: str) -> None:
    _login_failures.pop(key, None)
