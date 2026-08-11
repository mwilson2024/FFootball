from __future__ import annotations

import hashlib
from contextvars import ContextVar, Token

_username: ContextVar[str] = ContextVar("draftdesk_username", default="wilsonmw")
_league_ids: ContextVar[frozenset[str] | None] = ContextVar("draftdesk_league_ids", default=None)


def normalize_username(value: str) -> str:
    return value.strip().casefold()


def active_username() -> str:
    return _username.get()


def personal_source_prefix(username: str | None = None) -> str:
    normalized = normalize_username(username or active_username())
    owner_token = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    return f"personal_{owner_token}_"


def is_personal_source_id(source_id: str) -> bool:
    return source_id.startswith("personal_") or source_id.startswith("user_")


def source_visible_to_user(source_id: str, username: str | None = None) -> bool:
    if source_id.startswith("personal_"):
        return source_id.startswith(personal_source_prefix(username))
    # Older imports did not record an owner. Hide them rather than expose one
    # account's rankings to every other account.
    if source_id.startswith("user_"):
        return False
    return True


def set_active_username(value: str) -> Token[str]:
    return _username.set(normalize_username(value))


def reset_active_username(token: Token[str]) -> None:
    _username.reset(token)


def active_league_ids() -> set[str] | None:
    value = _league_ids.get()
    return set(value) if value is not None else None


def set_active_league_ids(value: tuple[str, ...] | None) -> Token[frozenset[str] | None]:
    return _league_ids.set(frozenset(value) if value is not None else None)


def reset_active_league_ids(token: Token[frozenset[str] | None]) -> None:
    _league_ids.reset(token)
