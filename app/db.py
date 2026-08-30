from collections.abc import Generator
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Settings, get_settings


class Base(DeclarativeBase):
    pass


def _ensure_sqlite_parent(url: str) -> None:
    prefix = "sqlite:///"
    if url.startswith(prefix) and url != "sqlite:///:memory:":
        Path(url.removeprefix(prefix)).parent.mkdir(parents=True, exist_ok=True)


settings = get_settings()
_ensure_sqlite_parent(settings.database_url)


def engine_options(database_settings: Settings) -> dict[str, Any]:
    options: dict[str, Any] = {
        "pool_size": database_settings.database_pool_size,
        "max_overflow": database_settings.database_max_overflow,
        "pool_timeout": database_settings.database_pool_timeout_seconds,
        "pool_pre_ping": True,
    }
    if database_settings.database_url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
    return options


engine = create_engine(settings.database_url, **engine_options(settings))
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(engine)
