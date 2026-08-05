from decimal import Decimal

import httpx
import pytest
from sqlalchemy.orm import Session

from app.catalog import draftable_positions, player_filters, query_players, roster_overview
from app.consensus import build_consensus, parse_ranking_csv
from app.draft import add_pick, draft_state, remove_pick, undo_draft, update_pick
from app.main import app
from app.models import (
    DataSource,
    DraftPick,
    PersonalPlayerPreference,
    RankingSnapshot,
    RosterAssignment,
)
from app.schemas import DraftPickCreate, DraftPickUpdate
from app.settings_store import setup_status
from app.sources import (
    initialize_sources,
    normalize_player_name,
    sync_fantasypros,
    sync_gng,
    sync_nflverse,
)


def add_rank(
    db: Session,
    player_id: str,
    overall: int,
    *,
    mfl_rank: int | None = None,
    adp: Decimal | None = None,
) -> None:
    db.add(
        RankingSnapshot(
            league_id="00999",
            player_id=player_id,
            overall_rank=overall,
            position_rank=overall,
            tier=1,
            custom_score=Decimal(100 - overall),
            value_over_replacement=Decimal(20 - overall),
            mfl_rank=mfl_rank,
            adp=adp,
        )
    )


def test_consensus_ignores_missing_ranks_and_preserves_all_players(seeded: Session) -> None:
    initialize_sources(seeded)
    add_rank(seeded, "0001234", 1, mfl_rank=2, adp=Decimal("4"))
    add_rank(seeded, "99", 2)
    seeded.commit()

    rows = {row["player_id"]: row for row in build_consensus(seeded, "00999")}

    assert set(rows) == {"0001234", "99"}
    assert rows["0001234"]["source_count"] == 3
    assert rows["99"]["source_count"] == 1
    assert rows["99"]["average_rank"] == 2


def test_combined_catalog_filters_and_roster_status(seeded: Session) -> None:
    initialize_sources(seeded)
    add_rank(seeded, "0001234", 1, mfl_rank=1)
    add_rank(seeded, "99", 2, mfl_rank=2)
    seeded.add(
        RosterAssignment(
            league_id="00999",
            franchise_id="0001",
            player_id="99",
            status="ROSTER",
            salary=Decimal("3"),
        )
    )
    seeded.commit()

    result = query_players(
        seeded,
        "00999",
        position="QB",
        nfl_team="NYJ",
        availability="rostered",
        owner="0001",
    )
    roster = roster_overview(seeded, "00999")

    assert [item["player_id"] for item in result["items"]] == ["99"]
    assert roster["teams"][0]["players"][0]["salary"] == "3.00"
    assert roster["teams"][0]["slots_remaining"] == 3


def test_player_pool_only_contains_positions_draftable_in_selected_league(
    seeded: Session,
) -> None:
    from app.models import League, Player

    league = seeded.get(League, ("00999", 2026))
    assert league is not None
    league.lineup_json = {"QB": 1, "RB": 1, "WR+TE": 3, "DEF": 1, "FLEX": 1}
    seeded.add_all(
        [
            Player(id="defense", name="Team Defense", position="DEF", nfl_team="BUF"),
            Player(id="safety", name="Individual Safety", position="S", nfl_team="BUF"),
            Player(id="kicker", name="Kicker", position="PK", nfl_team="BUF"),
            Player(id="coach", name="Coach", position="COACH", nfl_team="BUF"),
        ]
    )
    seeded.commit()

    assert draftable_positions(seeded, "00999") == {"QB", "RB", "WR", "TE", "DEF"}
    rows = query_players(seeded, "00999", per_page=500)["items"]
    assert {row["player_id"] for row in rows} == {"0001234", "99", "defense"}
    assert player_filters(seeded, "00999")["positions"] == ["QB", "RB", "DEF"]


def test_real_draft_add_edit_delete_and_undo(seeded: Session) -> None:
    pick = add_pick(
        seeded,
        DraftPickCreate(
            league_id="00999",
            player_id="0001234",
            franchise_id="0001",
            overall_pick=1,
        ),
    )
    updated = update_pick(
        seeded,
        pick.id,
        DraftPickUpdate(
            franchise_id="0002",
            round=1,
            pick=1,
            overall_pick=1,
            version=pick.version,
        ),
    )
    assert updated.franchise_id == "0002"
    remove_pick(seeded, pick.id)
    assert seeded.get(DraftPick, pick.id) is None
    undo_draft(seeded, "00999")
    assert seeded.get(DraftPick, pick.id) is not None
    assert draft_state(seeded, "00999")["picks"][0]["player_id"] == "0001234"


def test_user_csv_preview_import_and_queue_persist(seeded: Session, tmp_path) -> None:
    initialize_sources(seeded)
    content = b"player_name,team,position,overall_rank\nLeading Zero,BUF,RB,1\n"
    preview = parse_ranking_csv(
        seeded,
        "00999",
        content,
        "Legal personal sheet",
        confirm=False,
        import_directory=tmp_path,
    )
    imported = parse_ranking_csv(
        seeded,
        "00999",
        content,
        "Legal personal sheet",
        confirm=True,
        import_directory=tmp_path,
    )
    seeded.add(
        PersonalPlayerPreference(league_id="00999", player_id="0001234", queue_order=1, target=True)
    )
    seeded.commit()

    assert preview["ready"] is True
    assert imported["checksum"]
    assert seeded.get(DataSource, imported["source_id"]) is not None
    assert draft_state(seeded, "00999")["queue"][0]["player_id"] == "0001234"


def test_setup_redacts_secrets_and_no_mock_routes(seeded: Session, monkeypatch) -> None:
    monkeypatch.setattr("app.settings_store._secret", lambda _: "top-secret")
    status = setup_status(seeded)
    routes = {route.path for route in app.routes}

    assert "top-secret" not in repr(status)
    assert status["keeper_api_key_configured"] is True
    assert all("mock" not in path and "simulate" not in path for path in routes)
    assert "/api/draft/picks" in routes
    assert normalize_player_name("Smith, John Jr.") == "johnsmith"


@pytest.mark.asyncio
async def test_gng_and_fantasypros_rankings_preserve_provenance(seeded: Session) -> None:
    initialize_sources(seeded)
    fantasypros_source = seeded.get(DataSource, "fantasypros")
    assert fantasypros_source is not None
    fantasypros_source.enabled = True
    seeded.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.thegng.us":
            return httpx.Response(
                200,
                json={
                    "source": "The GNG, Pigskin rankings",
                    "license": "CC BY 4.0",
                    "url": "https://www.thegng.us/ranks",
                    "board_version": "test-gng-v1",
                    "generated_at": "2026-08-05T12:00:00Z",
                    "players": [
                        {
                            "rank": 7,
                            "player": "Leading Zero",
                            "position": "RB",
                            "team": "BUF",
                            "tier": "starter",
                        }
                    ],
                },
            )
        assert request.headers["x-api-key"] == "secret-key"
        return httpx.Response(
            200,
            json={
                "last_updated_ts": 12345,
                "players": [
                    {
                        "player_id": 9001,
                        "player_name": "Leading Zero",
                        "player_position_id": "RB",
                        "player_team_id": "BUF",
                        "rank_ecr": 3,
                        "rank_min": 1,
                        "rank_max": 8,
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    gng = await sync_gng(seeded, "00999", {"receptions": "0.5"}, transport=transport)
    fantasypros = await sync_fantasypros(
        seeded,
        "00999",
        2026,
        {"receptions": "0.5"},
        "secret-key",
        transport=transport,
    )
    rows = {row["player_id"]: row for row in build_consensus(seeded, "00999")}

    assert gng["matched"] == 1
    assert fantasypros["matched"] == 1
    assert rows["0001234"]["source_ranks"]["gng"] == "7.000000"
    assert rows["0001234"]["source_ranks"]["fantasypros"] == "3.000000"


@pytest.mark.asyncio
async def test_nflverse_sync_handles_new_identity_json_defaults(seeded: Session) -> None:
    initialize_sources(seeded)
    csv_body = (
        "display_name,position,latest_team,gsis_id,espn_id,pfr_id,pff_id,birth_date,"
        "rookie_season,status,years_of_experience,college_name,headshot\n"
        "Leading Zero,RB,BUF,00-0012345,12345,ZeroLe00,9876,2000-01-02,2026,ACT,0,Test U,"
        "https://example.test/player.png\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=csv_body, request=request)

    result = await sync_nflverse(seeded, transport=httpx.MockTransport(handler))
    source = seeded.get(DataSource, "nflverse")

    assert result == {"matched": 1, "unresolved": 0}
    assert source is not None
    assert source.last_success_at is not None
    assert source.last_error is None
