from __future__ import annotations

from contextvars import ContextVar, Token

_username: ContextVar[str] = ContextVar("draftdesk_username", default="wilsonmw")
_league_ids: ContextVar[frozenset[str] | None] = ContextVar("draftdesk_league_ids", default=None)


def normalize_username(value: str) -> str:
    return value.strip().casefold()


def active_username() -> str:
    return _username.get()


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
