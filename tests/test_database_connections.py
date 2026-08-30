import pytest
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

import app.main as main_module
from app.config import Settings
from app.db import engine_options


def test_database_pool_allows_25_total_connections() -> None:
    settings = Settings(
        _env_file=None,
        database_pool_size=10,
        database_max_overflow=15,
    )

    options = engine_options(settings)

    assert options["pool_size"] == 10
    assert options["max_overflow"] == 15
    assert options["pool_size"] + options["max_overflow"] == 25
    assert options["pool_pre_ping"] is True


def test_database_pool_rejects_more_than_25_connections() -> None:
    with pytest.raises(ValidationError, match="cannot exceed 25 connections"):
        Settings(
            _env_file=None,
            database_pool_size=20,
            database_max_overflow=6,
        )


def test_live_event_stream_releases_database_before_streaming(monkeypatch) -> None:
    state = {"entered": False, "closed": False}

    class ShortSession:
        def __enter__(self):
            state["entered"] = True
            return object()

        def __exit__(self, *_args):
            state["closed"] = True

    monkeypatch.setattr(main_module, "SessionLocal", ShortSession)
    monkeypatch.setattr(main_module, "_league_or_404", lambda _db, _league_id: object())

    response = main_module.league_event_stream("00999")

    assert isinstance(response, StreamingResponse)
    assert state == {"entered": True, "closed": True}
