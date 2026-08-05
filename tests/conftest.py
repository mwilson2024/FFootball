from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Franchise, League, Player


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    Base.metadata.drop_all(engine)


@pytest.fixture
def seeded(db: Session) -> Session:
    db.add(
        League(
            id="00999",
            season=2026,
            league_type="auction",
            name="Test League",
            roster_size=4,
            starting_budget=Decimal("20"),
            minimum_bid=Decimal("1"),
            settings_json={"auction_unit": "LEAGUE"},
            scoring_rules_json={},
            lineup_json={"QB": 1, "RB": 1},
            warnings_json=[],
        )
    )
    db.add_all(
        [
            Franchise(
                id="0001",
                league_id="00999",
                name="Alpha",
                starting_budget=Decimal("20"),
                roster_slots=4,
            ),
            Franchise(
                id="0002",
                league_id="00999",
                name="Beta",
                starting_budget=Decimal("20"),
                roster_slots=4,
            ),
            Player(id="0001234", name="Leading Zero", position="RB", nfl_team="BUF"),
            Player(id="99", name="Quarter Back", position="QB", nfl_team="NYJ"),
        ]
    )
    db.commit()
    return db
