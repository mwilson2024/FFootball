from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

from app.catalog import draftable_consensus
from app.draft import draft_intelligence
from app.draft_analysis import build_draft_analysis
from app.main import _queue_round_power_refresh
from app.models import (
    AuctionLiveState,
    AuctionPurchase,
    DataSource,
    League,
    MFLSnapshot,
    PowerRankingSnapshot,
    RankingSnapshot,
    RosterAssignment,
    SourcePlayerValue,
)
from app.power_cache import (
    cached_draft_analysis,
    cached_power_rankings,
    refresh_power_snapshot,
    round_refresh_due,
    stored_team_power,
)
from app.projections import build_projection_board, lineup_projection, score_historical_stats
from app.realtime import LeagueEventBroker
from app.sources import (
    LOCAL_PROJECTION_SPECS,
    initialize_sources,
    sync_local_projection_source,
)
from app.users import save_source_setting


def _ranking(db: Session, player_id: str, rank: int, points: str = "10") -> None:
    db.add(
        RankingSnapshot(
            league_id="00999",
            player_id=player_id,
            overall_rank=rank,
            position_rank=rank,
            tier=1,
            custom_score=Decimal(points),
            projected_points=Decimal(points),
            replacement_points=Decimal("0"),
            value_over_replacement=Decimal(points),
            adp=Decimal(rank),
            source_summary_json={"projection_note": "market proxy"},
        )
    )


def test_historical_stats_are_recalculated_with_imported_mfl_rules() -> None:
    rules = {
        "RB:RY": {"positions": "RB", "event": "RY", "points": "*0.1", "range": "0-999"},
        "RB:#R": {"positions": "RB", "event": "#R", "points": "*6", "range": "0-20"},
        "RB:CC": {"positions": "RB", "event": "CC", "points": "*1", "range": "0-200"},
    }
    points, mapped = score_historical_stats(
        {"rushing_yards": "1105", "rushing_tds": "10", "receptions": "52"},
        rules,
        "RB",
    )
    assert points == 222.5
    assert mapped == 3


def test_lineup_projection_honors_combined_mfl_position_slots() -> None:
    board = {
        "wr": {"position": "WR"},
        "te": {"position": "TE"},
        "rb": {"position": "RB"},
    }
    projections = {
        "wr": {"median": 200},
        "te": {"median": 150},
        "rb": {"median": 175},
    }

    result = lineup_projection({"wr", "te", "rb"}, board, projections, {"WR+TE": 2, "FLEX": 1})

    assert result["missing_starters"] == {}
    assert result["projected_starter_points"] == 525


def test_projection_board_exposes_season_distribution_and_provenance(seeded: Session) -> None:
    league = seeded.get_one(League, ("00999", 2026))
    league.scoring_rules_json = {
        "RB:RY": {"positions": "RB", "event": "RY", "points": "*0.1", "range": "0-999"},
        "RB:#R": {"positions": "RB", "event": "#R", "points": "*6", "range": "0-20"},
    }
    seeded.add(DataSource(id="nflverse", name="nflverse", kind="historical"))
    _ranking(seeded, "0001234", 1, "20")
    seeded.add(
        SourcePlayerValue(
            source_id="nflverse",
            player_id="0001234",
            value_type="player_stats",
            raw_value_json={
                "season": 2025,
                "stats": {"rushing_yards": "1105", "rushing_tds": "10", "carries": "201"},
            },
            snapshot_id="stats-2025",
        )
    )
    seeded.commit()

    board = draftable_consensus(seeded, "00999")
    projection = build_projection_board(seeded, league, board)["0001234"]

    assert projection["median"] > 100
    assert projection["floor"] < projection["median"] < projection["ceiling"]
    assert projection["workload"] > 50
    assert projection["mapped_scoring_rules"] == 2
    assert "Imported MFL scoring rules" in projection["sources"]


def test_espn_ppr_projection_csv_feeds_tmfl_season_outcomes(
    seeded: Session, tmp_path, monkeypatch
) -> None:
    initialize_sources(seeded)
    projection_file = tmp_path / "espn-projections.csv"
    projection_file.write_text(
        "player_name,team,position,season_projection,projected_average,espn_player_id\n"
        "Leading Zero,BUF,RB,312.5,18.38,12345\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(LOCAL_PROJECTION_SPECS["espn_ppr_projection_csv"], "path", projection_file)
    result = sync_local_projection_source(seeded, "espn_ppr_projection_csv")
    _ranking(seeded, "0001234", 1, "20")
    seeded.commit()

    league = seeded.get_one(League, ("00999", 2026))
    board = draftable_consensus(seeded, "00999")
    projection = build_projection_board(seeded, league, board)["0001234"]

    assert result["matched"] == 1
    assert result["leagues"] == [{"league_id": "00999", "matched": 1, "unresolved": 0}]
    assert projection["median"] == 312.5
    assert "ESPN 2026 PPR Season Projections (TMFL)" in projection["sources"]
    assert projection["basis"] == "Imported full-season projection"


def test_pff_projection_source_can_be_included_or_disabled(
    seeded: Session, tmp_path, monkeypatch
) -> None:
    initialize_sources(seeded)
    projection_file = tmp_path / "pff-projections.csv"
    projection_file.write_text(
        "player_name,team,position,season_projection,projected_average,projection_floor,"
        "projection_ceiling,pff_player_id,expert_analysis\n"
        'Leading Zero,BUF,RB,340,20,250,430,67890,"PFF role analysis."\n',
        encoding="utf-8",
    )
    monkeypatch.setitem(LOCAL_PROJECTION_SPECS["pff_ppr_projection_csv"], "path", projection_file)
    result = sync_local_projection_source(seeded, "pff_ppr_projection_csv")
    _ranking(seeded, "0001234", 1, "20")
    seeded.commit()

    league = seeded.get_one(League, ("00999", 2026))
    board = draftable_consensus(seeded, "00999")
    included = build_projection_board(seeded, league, board)["0001234"]

    assert result["matched"] == 1
    assert included["median"] == 340
    assert "PFF 2026 PPR Season Projections (TMFL)" in included["sources"]

    save_source_setting(
        seeded, "pff_ppr_projection_csv", enabled=False, weight=Decimal("1")
    )
    disabled = build_projection_board(seeded, league, board)["0001234"]
    assert disabled["median"] != 340
    assert "PFF 2026 PPR Season Projections (TMFL)" not in disabled["sources"]


def test_scenario_lab_and_post_draft_analysis_use_real_order(seeded: Session, monkeypatch) -> None:
    now = datetime.now(UTC)
    _ranking(seeded, "0001234", 1, "20")
    _ranking(seeded, "99", 2, "15")
    seeded.add(
        RosterAssignment(
            league_id="00999", franchise_id="0001", player_id="0001234", status="ROSTER"
        )
    )
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
                            {"round": "2", "pick": "1", "franchise": "0002", "player": ""},
                            {"round": "2", "pick": "2", "franchise": "0001", "player": ""},
                        ]
                    }
                }
            },
            fetched_at=now,
            expires_at=now + timedelta(minutes=15),
        )
    )
    seeded.commit()

    intelligence = draft_intelligence(seeded, "00999", "0001")["intelligence"]
    recommendation = intelligence["recommendations"][0]
    analysis = build_draft_analysis(seeded, "00999", "0001")

    assert recommendation["scenario"]["next_pick"] == 4
    assert recommendation["scenario"]["position_cliff_probability"] >= 5
    assert recommendation["projection"]["model_version"] == "season-outcomes-v1"
    assert analysis["projection_model"] == "season-outcomes-v1"
    assert len(analysis["projected_standings"]) == 2
    assert analysis["selected_team"]["franchise_name"] == "Alpha"
    assert len(analysis["selected_team"]["roster_players"]) == 1
    roster_player = analysis["selected_team"]["roster_players"][0]
    assert roster_player["player_id"] == "0001234"
    assert roster_player["player_name"] == "Leading Zero"
    assert roster_player["lineup_role"] == "Starter · RB"
    assert roster_player["is_starter"] is True
    assert roster_player["median"] is not None
    assert roster_player["roster_status"] == "ROSTER"

    analysis_calls = 0

    def tracked_analysis(*args, **kwargs):
        nonlocal analysis_calls
        analysis_calls += 1
        assert kwargs["include_all_teams"] is True
        assert kwargs["board"]
        return build_draft_analysis(*args, **kwargs)

    monkeypatch.setattr("app.power_cache.build_draft_analysis", tracked_analysis)
    stored = refresh_power_snapshot(seeded, "00999", trigger="test-warmup")
    assert analysis_calls == 1
    assert (
        stored.payload_json["teams"]["0001"]["selected_team"]["roster_players"][0]["player_id"]
        == "0001234"
    )
    assert seeded.get(PowerRankingSnapshot, "00999") is not None

    def unexpected_compute(*args, **kwargs):
        raise AssertionError("stored Power Rankings should not recompute on a team click")

    monkeypatch.setattr("app.power_cache.build_draft_analysis", unexpected_compute)
    monkeypatch.setattr("app.power_cache.build_power_rankings", unexpected_compute)
    cached_team = cached_draft_analysis(seeded, "00999", "0001")
    cached_power = cached_power_rankings(seeded, "00999")
    stored_team = stored_team_power(seeded, "00999", "0001")
    assert cached_team["selected_team"]["franchise_name"] == "Alpha"
    assert cached_team["cache"]["trigger"] == "test-warmup"
    assert cached_power["rankings"][0]["franchise_name"] == "Alpha"
    assert stored_team is not None
    assert stored_team["analysis"]["franchise_name"] == "Alpha"

    seeded.add_all(
        [
            AuctionPurchase(
                league_id="00999",
                franchise_id="0001",
                player_id="0001234",
                amount=Decimal("1"),
                purchase_order=1,
            ),
            AuctionPurchase(
                league_id="00999",
                franchise_id="0002",
                player_id="99",
                amount=Decimal("1"),
                purchase_order=2,
            ),
        ]
    )
    seeded.commit()
    assert round_refresh_due(seeded, "00999", "auction") is True

    background_tasks = BackgroundTasks()
    _queue_round_power_refresh(background_tasks, seeded, "00999", "auction")
    assert len(background_tasks.tasks) == 0

    live = seeded.get(AuctionLiveState, "00999")
    assert live is not None
    live.is_live = True
    seeded.commit()
    _queue_round_power_refresh(background_tasks, seeded, "00999", "auction")
    assert len(background_tasks.tasks) == 1


@pytest.mark.asyncio
async def test_realtime_broker_pushes_one_league_event() -> None:
    broker = LeagueEventBroker()
    stream = broker.stream("00999")
    assert "event: ready" in await anext(stream)
    broker.publish("00999", "draft-picks:create", {"entity_id": "pick-1"})
    event = await anext(stream)
    assert "event: league-update" in event
    assert '"type":"draft-picks:create"' in event
    await stream.aclose()
