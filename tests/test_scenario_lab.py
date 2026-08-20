from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.catalog import draftable_consensus
from app.draft import draft_intelligence
from app.draft_analysis import build_draft_analysis
from app.models import (
    DataSource,
    League,
    MFLSnapshot,
    RankingSnapshot,
    RosterAssignment,
    SourcePlayerValue,
)
from app.projections import build_projection_board, lineup_projection, score_historical_stats
from app.realtime import LeagueEventBroker


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


def test_scenario_lab_and_post_draft_analysis_use_real_order(seeded: Session) -> None:
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
