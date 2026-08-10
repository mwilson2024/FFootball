import json
from decimal import Decimal

from sqlalchemy.orm import Session

from app.audit_backup import append_audit_event
from app.catalog import player_detail, query_players
from app.models import Player, RankingSnapshot, RosterAssignment, SourcePlayerValue
from app.power_rankings import build_power_rankings
from app.sources import initialize_sources


def _rank(db: Session, player_id: str, rank: int, value: int) -> None:
    db.add(
        RankingSnapshot(
            league_id="00999",
            player_id=player_id,
            overall_rank=rank,
            position_rank=rank,
            tier=1,
            custom_score=Decimal(value),
            projected_points=Decimal(value),
            value_over_replacement=Decimal(value),
        )
    )


def test_external_audit_log_is_append_only_and_hash_chained(tmp_path) -> None:
    first = append_audit_event(
        tmp_path,
        stream="draft-picks",
        action="create",
        league_id="00999",
        actor="commissioner",
        entity_id="pick-1",
        after={"player_id": "0001234"},
    )
    second = append_audit_event(
        tmp_path,
        stream="draft-picks",
        action="delete",
        league_id="00999",
        actor="commissioner",
        entity_id="pick-1",
        before={"player_id": "0001234"},
    )

    records = [
        json.loads(line) for line in (tmp_path / "draft-picks.jsonl").read_text().splitlines()
    ]
    assert len(records) == 2
    assert records[0]["event_hash"] == first["event_hash"]
    assert records[1]["previous_hash"] == first["event_hash"]
    assert records[1]["event_hash"] == second["event_hash"]


def test_availability_exposes_team_name_but_filters_with_team_id(seeded: Session) -> None:
    seeded.add(
        RosterAssignment(
            league_id="00999",
            franchise_id="0001",
            player_id="0001234",
            status="ROSTER",
        )
    )
    seeded.commit()

    all_players = query_players(seeded, "00999", availability="all", per_page=100)["items"]
    rostered = next(row for row in all_players if row["player_id"] == "0001234")
    team_players = query_players(seeded, "00999", owner="0001", per_page=100)["items"]

    assert rostered["owner_id"] == "0001"
    assert rostered["rostered_by"] == "Alpha"
    assert [row["player_id"] for row in team_players] == ["0001234"]


def test_player_profile_lists_source_rank_depth_and_all_stats(seeded: Session) -> None:
    initialize_sources(seeded)
    _rank(seeded, "0001234", 1, 25)
    player = seeded.get_one(Player, "0001234")
    player.metadata_json = {"sleeper": {"depth_chart_position": "RB", "depth_chart_order": 1}}
    seeded.add(
        SourcePlayerValue(
            source_id="nflverse",
            player_id="0001234",
            value_type="player_stats",
            raw_value_json={
                "season": 2025,
                "summary_level": "regular season",
                "team": "BUF",
                "stats": {"carries": "201", "rushing_yards": "1105", "targets": "52"},
            },
            snapshot_id="stats-2025",
        )
    )
    seeded.commit()

    detail = player_detail(seeded, "00999", "0001234")

    assert detail is not None
    assert detail["profile"]["source_rank_details"][0]["source_name"]
    assert detail["profile"]["depth_chart"]["depth_team"] == 1
    assert detail["profile"]["nerdy_stats"]["stats"]["rushing_yards"] == "1105"


def test_power_rankings_reward_starter_strength_and_depth(seeded: Session) -> None:
    seeded.add_all(
        [
            RosterAssignment(
                league_id="00999", franchise_id="0001", player_id="0001234", status="ROSTER"
            ),
            RosterAssignment(
                league_id="00999", franchise_id="0002", player_id="99", status="ROSTER"
            ),
        ]
    )
    _rank(seeded, "0001234", 1, 30)
    _rank(seeded, "99", 2, 10)
    seeded.commit()

    result = build_power_rankings(seeded, "00999")

    assert result["rankings"][0]["franchise_name"] == "Alpha"
    assert result["rankings"][0]["power_score"] == 100.0
    assert result["rankings"][1]["rank"] == 2
