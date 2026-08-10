from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.automation import next_daily_sync
from app.models import DataSource
from app.sources import initialize_sources


def test_next_daily_sync_stays_at_one_am_eastern_across_seasons() -> None:
    summer = next_daily_sync(datetime(2026, 8, 5, 4, 30, tzinfo=UTC))
    winter = next_daily_sync(datetime(2026, 1, 5, 5, 30, tzinfo=UTC))

    assert summer == datetime(2026, 8, 5, 5, 0, tzinfo=UTC)
    assert winter == datetime(2026, 1, 5, 6, 0, tzinfo=UTC)


def test_source_initialization_turns_every_source_on(db: Session) -> None:
    from app.main import update_source
    from app.schemas import SourceUpdate

    initialize_sources(db)
    sleeper = db.get(DataSource, "sleeper")
    nflverse = db.get(DataSource, "nflverse")
    assert sleeper is not None
    assert nflverse is not None
    sleeper.enabled = False
    nflverse.weight = Decimal("0")
    db.commit()

    initialize_sources(db)
    sources = list(db.query(DataSource).all())

    assert sources
    assert all(source.enabled for source in sources)
    assert all(Decimal(source.weight) > 0 for source in sources)
    assert db.get(DataSource, "espn_dynasty_csv") is not None
    assert db.get(DataSource, "fantasypros_dynasty") is None

    response = update_source("sleeper", SourceUpdate(enabled=False, weight=Decimal("0.5")), db)
    assert response["enabled"] is False
    assert Decimal(response["weight"]) == Decimal("0.5")
