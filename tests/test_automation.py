import asyncio
import inspect
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation import (
    LIVE_DRAFT_SYNC_SECONDS,
    live_draft_sync_loop,
    next_daily_sync,
    sync_live_draft_sessions,
)
from app.draft import set_draft_live
from app.mfl import MFLResponse
from app.models import DataSource, DraftPick, LeagueType
from app.sources import LOCAL_PROJECTION_SPECS, LOCAL_RANKING_SPECS, initialize_sources
from app.users import save_draft_mode


def test_next_daily_sync_stays_at_one_am_eastern_across_seasons() -> None:
    summer = next_daily_sync(datetime(2026, 8, 5, 4, 30, tzinfo=UTC))
    winter = next_daily_sync(datetime(2026, 1, 5, 5, 30, tzinfo=UTC))

    assert summer == datetime(2026, 8, 5, 5, 0, tzinfo=UTC)
    assert winter == datetime(2026, 1, 5, 6, 0, tzinfo=UTC)


def test_source_initialization_applies_source_defaults(db: Session) -> None:
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
    assert db.get(DataSource, "league_model").enabled is False
    assert db.get(DataSource, "mfl_projection").enabled is False
    assert all(
        source.enabled
        for source in sources
        if source.id not in {"league_model", "mfl_projection"}
    )
    assert all(Decimal(source.weight) > 0 for source in sources)
    assert db.get(DataSource, "espn_dynasty_csv") is not None
    assert db.get(DataSource, "espn_ppr_projection_csv") is not None
    assert db.get(DataSource, "pff_ppr_projection_csv") is not None
    assert db.get(DataSource, "fantasypros_redraft_csv") is not None
    assert db.get(DataSource, "fantasypros_dynasty_csv") is not None
    assert db.get(DataSource, "fantasysharks_dynasty_csv") is not None
    assert db.get(DataSource, "pff_rankings_csv") is not None
    assert db.get(DataSource, "local_redraft_csv") is None
    assert db.get(DataSource, "fantasypros_dynasty") is None

    response = update_source("sleeper", SourceUpdate(enabled=False, weight=Decimal("0.5")), db)
    assert response["enabled"] is False
    assert Decimal(response["weight"]) == Decimal("0.5")


def test_shared_ranking_files_are_mapped_to_the_intended_league_types() -> None:
    expected = {
        "espn_ppr_csv": ("NFL26_CS_PPR(new).csv", {LeagueType.AUCTION}),
        "espn_dynasty_csv": ("NFL26_CS_Dyn(new).csv", {LeagueType.KEEPER}),
        "fantasypros_redraft_csv": (
            "FantasyPros_2026_Draft_ALL_Rankings.csv",
            {LeagueType.AUCTION},
        ),
        "fantasypros_dynasty_csv": (
            "FantasyPros_2026_Dynasty_ALL_Rankings.csv",
            {LeagueType.KEEPER},
        ),
        "fantasysharks_dynasty_csv": (
            "fantasysharks_2026_rankings_dyn.csv",
            {LeagueType.KEEPER},
        ),
        "pff_rankings_csv": (
            "PFF_2026_Fantasy_Rankings.csv",
            {LeagueType.AUCTION, LeagueType.KEEPER},
        ),
    }

    for source_id, (filename, league_types) in expected.items():
        assert LOCAL_RANKING_SPECS[source_id]["path"].name == filename
        assert LOCAL_RANKING_SPECS[source_id]["league_types"] == league_types

    projection = LOCAL_PROJECTION_SPECS["espn_ppr_projection_csv"]
    assert projection["path"].name == "ESPN_2026_PPR_Projections.csv"
    assert projection["league_types"] == {LeagueType.AUCTION}
    pff_projection = LOCAL_PROJECTION_SPECS["pff_ppr_projection_csv"]
    assert pff_projection["path"].name == "PFF_2026_PPR_Projections.csv"
    assert pff_projection["league_types"] == {LeagueType.AUCTION}


def test_live_draft_sync_imports_mfl_picks_and_is_idempotent(seeded: Session) -> None:
    set_draft_live(seeded, "00999", True)
    calls = []

    class FakeClient:
        async def export(self, export_type, *, league_id=None, db=None, force=False):
            calls.append((export_type, league_id, force))
            return MFLResponse(
                export_type,
                {
                    "draftResults": {
                        "draftUnit": {
                            "draftPick": {
                                "round": "1",
                                "pick": "1",
                                "overallPick": "1",
                                "franchise": "0001",
                                "player": "0001234",
                            }
                        }
                    }
                },
                "https://api.myfantasyleague.com/2026/export",
                datetime.now(UTC),
            )

    first = asyncio.run(sync_live_draft_sessions(seeded, FakeClient()))
    second = asyncio.run(sync_live_draft_sessions(seeded, FakeClient()))
    imported = seeded.scalar(select(DraftPick).where(DraftPick.player_id == "0001234"))

    assert LIVE_DRAFT_SYNC_SECONDS == 30
    assert inspect.signature(live_draft_sync_loop).parameters["interval_seconds"].default == 30
    assert calls == [
        ("draftResults", "00999", True),
        ("draftResults", "00999", True),
    ]
    assert first[0]["applied_count"] == 1
    assert second[0]["applied_count"] == 0
    assert imported is not None
    assert imported.source == "mfl"


def test_live_draft_sync_does_not_contact_mfl_while_draft_is_paused(seeded: Session) -> None:
    class FailClient:
        async def export(self, *_args, **_kwargs):
            raise AssertionError("MFL must not be called for a paused draft")

    assert asyncio.run(sync_live_draft_sessions(seeded, FailClient())) == []


def test_live_draft_sync_does_not_contact_mfl_in_local_mode(seeded: Session) -> None:
    set_draft_live(seeded, "00999", True)
    save_draft_mode(seeded, "00999", "local")

    class FailClient:
        async def export(self, *_args, **_kwargs):
            raise AssertionError("MFL must not be called for a local real-time draft")

    assert asyncio.run(sync_live_draft_sessions(seeded, FailClient())) == []
