from __future__ import annotations

from contextvars import ContextVar, Token

_username: ContextVar[str] = ContextVar("draftdesk_username", default="wilsonmw")


def normalize_username(value: str) -> str:
    return value.strip().casefold()


def active_username() -> str:
    return _username.get()


def set_active_username(value: str) -> Token[str]:
    return _username.set(normalize_username(value))


def reset_active_username(token: Token[str]) -> None:
    _username.reset(token)
