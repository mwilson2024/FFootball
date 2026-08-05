from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_PATH = Path("data/logs/sync_warnings.log")


def warning_logger() -> logging.Logger:
    logger = logging.getLogger("mfl_draft_manager.sync_warnings")
    if logger.handlers:
        return logger
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    return logger


def log_sync_warnings(league_id: str, season: int, warnings: list[str]) -> None:
    logger = warning_logger()
    for message in warnings:
        logger.warning("league=%s season=%s | %s", league_id, season, message)
