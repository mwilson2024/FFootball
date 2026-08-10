from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auction import add_purchase
from app.catalog import (
    draftable_positions,
    player_detail,
    player_filters,
    query_players,
    roster_overview,
)
from app.consensus import build_consensus, parse_ranking_csv
from app.draft import (
    add_pick,
    draft_state,
    remove_pick,
    set_draft_live,
    undo_draft,
    update_pick,
)
from app.main import _purchase_json, app
from app.models import (
    DataSource,
    DraftPick,
    MFLSnapshot,
    PersonalPlayerPreference,
    Player,
    RankingSnapshot,
    RosterAssignment,
    SourcePlayerValue,
)
from app.schemas import DraftPickCreate, DraftPickUpdate, PurchaseCreate
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
    defense = next(row for row in rows if row["player_id"] == "defense")
    top_skill = next(row for row in rows if row["position"] != "DEF")
    assert defense["tier"] >= 6
    assert Decimal(top_skill["suggested_auction_value"]) > Decimal("1")
    assert Decimal(top_skill["max_recommended_bid"]) >= Decimal(
        top_skill["suggested_auction_value"]
    )
    assert player_filters(seeded, "00999")["positions"] == ["QB", "RB", "DEF"]
    defense_detail = player_detail(seeded, "00999", "defense")
    assert defense_detail is not None
    assert defense_detail["profile"]["external_links"][0] == {
        "label": "FantasyPros defense news",
        "url": "https://www.fantasypros.com/nfl/news/buffalo-defense.php",
        "guessed": False,
    }


def test_dynamic_bid_reacts_to_selected_players_and_remaining_money(seeded: Session) -> None:
    initialize_sources(seeded)
    seeded.add_all(
        [
            Player(id=f"depth-{index}", name=f"Depth Player {index}", position="RB", nfl_team="BUF")
            for index in range(3, 13)
        ]
    )
    add_rank(seeded, "0001234", 1)
    add_rank(seeded, "99", 2)
    for index in range(3, 13):
        add_rank(seeded, f"depth-{index}", index)
    seeded.commit()
    before = {row["player_id"]: row for row in query_players(seeded, "00999")["items"]}

    add_purchase(
        seeded,
        PurchaseCreate(
            league_id="00999",
            player_id="0001234",
            franchise_id="0001",
            amount=Decimal("17"),
            status="ROSTER",
        ),
    )
    after = {row["player_id"]: row for row in query_players(seeded, "00999")["items"]}

    assert after["0001234"]["dynamic_bid"] is None
    assert after["99"]["dynamic_bid"] != before["99"]["dynamic_bid"]
    assert after["99"]["suggested_auction_value"] == before["99"]["suggested_auction_value"]


def test_auction_purchase_payload_includes_player_and_franchise_names(seeded: Session) -> None:
    purchase = add_purchase(
        seeded,
        PurchaseCreate(
            league_id="00999",
            player_id="0001234",
            franchise_id="0001",
            amount=Decimal("5"),
            status="ROSTER",
        ),
    )

    payload = _purchase_json(seeded, purchase)

    assert payload["player_name"] == "Leading Zero"
    assert payload["player_team"] == "BUF"
    assert payload["franchise_name"] == "Alpha"


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


def test_draft_live_status_is_shared_and_survives_a_pick(seeded: Session) -> None:
    assert set_draft_live(seeded, "00999", True)["is_live"] is True
    add_pick(
        seeded,
        DraftPickCreate(
            league_id="00999",
            player_id="0001234",
            franchise_id="0001",
            overall_pick=1,
        ),
    )

    assert draft_state(seeded, "00999")["live"] == {"is_live": True, "status": "live"}
    assert set_draft_live(seeded, "00999", False) == {
        "is_live": False,
        "status": "paused",
    }


def test_draft_state_uses_mfl_traded_pick_order_and_current_drafter(seeded: Session) -> None:
    now = datetime.now(UTC)
    seeded.add(
        MFLSnapshot(
            league_id="00999",
            season=2026,
            export_type="draftResults",
            source_url="https://api.myfantasyleague.com/2026/export",
            parameters_json={},
            payload_json={
                "draftResults": {
                    "draftUnit": {
                        "draftPick": [
                            {"round": "1", "pick": "1", "franchise": "0001", "player": ""},
                            {"round": "1", "pick": "2", "franchise": "0002", "player": ""},
                        ]
                    }
                }
            },
            fetched_at=now,
            expires_at=now + timedelta(minutes=15),
        )
    )
    seeded.commit()

    before = draft_state(seeded, "00999")
    assert [slot["franchise_id"] for slot in before["draft_order"]] == ["0001", "0002"]
    assert before["current_drafter"]["franchise_name"] == "Alpha"

    add_pick(
        seeded,
        DraftPickCreate(
            league_id="00999",
            player_id="0001234",
            franchise_id="0001",
            round=1,
            pick=1,
            overall_pick=1,
        ),
    )
    after = draft_state(seeded, "00999")
    assert after["draft_order"][0]["completed"] is True
    assert after["current_drafter"]["franchise_name"] == "Beta"


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
    detail = player_detail(seeded, "00999", "0001234")
    assert detail is not None
    assert detail["profile"]["fantasypros"]["rank_ecr"] == 3
    assert detail["profile"]["external_links"][0]["url"].startswith(
        "https://www.fantasypros.com/nfl/players/"
    )


@pytest.mark.asyncio
async def test_fantasypros_dynasty_rankings_are_scoped_to_adfl(seeded: Session) -> None:
    from app.models import League

    initialize_sources(seeded)
    seeded.add(
        League(
            id="adfl",
            season=2026,
            league_type="keeper",
            name="ADFL",
            roster_size=20,
            starting_budget=None,
            minimum_bid=Decimal("1"),
            settings_json={},
            scoring_rules_json={"receptions": "1"},
            lineup_json={"QB": 1, "RB": 2, "WR": 2, "TE": 1},
            warnings_json=[],
        )
    )
    seeded.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["type"] == "DYNASTY"
        return httpx.Response(
            200,
            request=request,
            json={
                "last_updated_ts": 67890,
                "players": [
                    {
                        "player_id": 9001,
                        "player_name": "Leading Zero",
                        "player_position_id": "RB",
                        "player_team_id": "BUF",
                        "rank_ecr": 2,
                    }
                ],
            },
        )

    result = await sync_fantasypros(
        seeded,
        "adfl",
        2026,
        {"receptions": "1"},
        "secret-key",
        source_id="fantasypros_dynasty",
        ranking_type="DYNASTY",
        transport=httpx.MockTransport(handler),
    )
    adfl = {row["player_id"]: row for row in build_consensus(seeded, "adfl")}
    tmfl = {row["player_id"]: row for row in build_consensus(seeded, "00999")}

    assert result["ranking_type"] == "DYNASTY"
    assert adfl["0001234"]["source_ranks"]["fantasypros_dynasty"] == "2.000000"
    assert "fantasypros_dynasty" not in tmfl["0001234"]["source_ranks"]


def test_projection_aav_trend_and_schedule_sources_affect_consensus(seeded: Session) -> None:
    initialize_sources(seeded)
    add_rank(seeded, "0001234", 1)
    add_rank(seeded, "99", 2)
    snapshots = list(seeded.query(RankingSnapshot).all())
    for snapshot in snapshots:
        if snapshot.player_id == "0001234":
            snapshot.projected_points = Decimal("20")
            snapshot.mfl_aav = Decimal("40")
        else:
            snapshot.projected_points = Decimal("10")
            snapshot.mfl_aav = Decimal("5")
        snapshot.source_summary_json = {
            "projection_note": "MFL weekly projected score; not a full-season projection"
        }
    seeded.add_all(
        [
            SourcePlayerValue(
                source_id="sleeper",
                league_id=None,
                player_id="0001234",
                value_type="trend_add_24h",
                raw_value_json={"count": 100},
                normalized_value=Decimal("100"),
                snapshot_id="sleeper-test",
            ),
            SourcePlayerValue(
                source_id="nflverse",
                league_id=None,
                player_id="0001234",
                value_type="schedule",
                raw_value_json={"schedule_rank": 4},
                normalized_value=Decimal("4"),
                snapshot_id="schedule-test",
            ),
        ]
    )
    seeded.commit()

    row = next(item for item in build_consensus(seeded, "00999") if item["player_id"] == "0001234")

    assert {"mfl_projection", "mfl_aav", "sleeper", "nflverse"}.issubset(row["source_ranks"])


@pytest.mark.asyncio
async def test_nflverse_sync_handles_new_identity_json_defaults(seeded: Session) -> None:
    initialize_sources(seeded)
    csv_body = (
        "display_name,position,latest_team,gsis_id,espn_id,pfr_id,pff_id,birth_date,"
        "rookie_season,status,years_of_experience,college_name,headshot\n"
        "Leading Zero,RB,BUF,00-0012345,12345,ZeroLe00,9876,2000-01-02,2026,ACT,0,Test U,"
        "https://example.test/player.png\n"
    )
    schedule_body = (
        "game_id,season,game_type,week,gameday,weekday,gametime,away_team,away_score,"
        "home_team,home_score\n"
        "2025_01_BUF_NYJ,2025,REG,1,2025-09-07,Sunday,13:00,BUF,20,NYJ,10\n"
        "2026_01_BUF_NYJ,2026,REG,1,2026-09-13,Sunday,13:00,BUF,,NYJ,\n"
    )
    depth_body = (
        "season,week,dt,club_code,gsis_id,position,depth_position,depth_team\n"
        "2026,1,2026-09-01,BUF,00-0012345,RB,RB,1\n"
    )
    stats_body = (
        "player_id,player_name,season,season_type,team,position,carries,rushing_yards,"
        "fantasy_points_ppr\n"
        "00-0012345,Leading Zero,2025,REG,BUF,RB,201,1105,245.5\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "schedules" in url:
            body = schedule_body
        elif "depth_charts" in url:
            body = depth_body
        elif "stats_player" in url:
            body = stats_body
        else:
            body = csv_body
        return httpx.Response(200, text=body, request=request)

    result = await sync_nflverse(seeded, transport=httpx.MockTransport(handler))
    source = seeded.get(DataSource, "nflverse")

    assert result == {"matched": 1, "unresolved": 0}
    assert source is not None
    assert source.last_success_at is not None
    assert source.last_error is None
    assert seeded.get(Player, "0001234").rookie is True
    schedule = seeded.scalar(
        select(SourcePlayerValue).where(
            SourcePlayerValue.player_id == "0001234",
            SourcePlayerValue.value_type == "schedule",
        )
    )
    assert schedule is not None
    assert schedule.raw_value_json["schedule_rank"] == 1
    assert schedule.raw_value_json["games"][0]["opponent"] == "NYJ"
    assert schedule.raw_value_json["games"][0]["offense_matchup_score"] == 20
    assert schedule.raw_value_json["games"][0]["defense_matchup_score"] == 10
    detail = player_detail(seeded, "00999", "0001234")
    assert detail is not None
    assert detail["profile"]["schedule"]["schedule_rank_label"] == "1 of 2"
    assert detail["profile"]["depth_chart"]["depth_team"] == "1"
    assert detail["profile"]["nerdy_stats"]["stats"]["rushing_yards"] == "1105"
