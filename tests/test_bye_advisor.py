from decimal import Decimal

from sqlalchemy.orm import Session

from app.bye_advisor import bye_week_advice
from app.models import (
    Player,
    RankingSnapshot,
    RosterAssignment,
    SourcePlayerValue,
    UserLeagueSetting,
)
from app.sources import initialize_sources


def _ranking(db: Session, player_id: str, overall: int) -> None:
    db.add(
        RankingSnapshot(
            league_id="00999",
            player_id=player_id,
            overall_rank=overall,
            position_rank=overall,
            tier=1,
            custom_score=Decimal(100 - overall),
            projected_points=Decimal(20 - overall),
            value_over_replacement=Decimal(20 - overall),
        )
    )


def _schedule(db: Session, player_id: str, team: str, opponent: str, matchup_score: int) -> None:
    db.add(
        SourcePlayerValue(
            source_id="nflverse",
            league_id=None,
            player_id=player_id,
            value_type="schedule",
            raw_value_json={
                "team": team,
                "schedule_rank": 10,
                "schedule_rank_label": "10 of 32",
                "games": [
                    {
                        "week": 5,
                        "opponent": opponent,
                        "home_away": "home",
                        "offense_matchup_score": matchup_score,
                        "defense_matchup_score": 20,
                    }
                ],
            },
            normalized_value=Decimal("10"),
            snapshot_id=f"schedule-{player_id}",
        )
    )


def test_bye_advisor_uses_selected_roster_rank_and_weekly_matchup(seeded: Session) -> None:
    initialize_sources(seeded)
    rostered = seeded.get(Player, "99")
    assert rostered is not None
    rostered.bye_week = 5
    seeded.add_all(
        [
            Player(id="free-hard", name="Higher Rank", position="QB", nfl_team="BUF"),
            Player(id="free-easy", name="Better Matchup", position="QB", nfl_team="MIA"),
            RosterAssignment(
                league_id="00999",
                franchise_id="0001",
                player_id="99",
                status="ROSTER",
            ),
            UserLeagueSetting(
                username="wilsonmw",
                league_id="00999",
                franchise_id="0001",
                auction_strategy_json={"template": "balanced"},
            ),
        ]
    )
    _ranking(seeded, "99", 1)
    _ranking(seeded, "free-hard", 2)
    _ranking(seeded, "free-easy", 3)
    _schedule(seeded, "free-hard", "BUF", "NYJ", 18)
    _schedule(seeded, "free-easy", "MIA", "NE", 30)
    seeded.commit()

    result = bye_week_advice(seeded, "00999", 5)

    assert result["configured"] is True
    assert result["franchise"]["name"] == "Alpha"
    assert result["week_summary"][4]["bye_count"] == 1
    assert result["conflicts"][0]["player_name"] == "Quarter Back"
    recommendations = result["conflicts"][0]["recommendations"]
    assert recommendations[0]["player_name"] == "Better Matchup"
    assert recommendations[0]["opponent"] == "NE"
    assert recommendations[0]["matchup_label"] == "Great"
    assert recommendations[1]["overall_rank"] < recommendations[0]["overall_rank"]


def test_bye_advisor_requests_team_selection_when_missing(seeded: Session) -> None:
    result = bye_week_advice(seeded, "00999", 5)

    assert result["configured"] is False
    assert result["franchise"] is None
    assert result["conflicts"] == []


def test_bye_advisor_can_use_an_explicit_roster_from_the_draft_viewer(
    seeded: Session,
) -> None:
    rostered = seeded.get(Player, "99")
    assert rostered is not None
    rostered.bye_week = 5
    seeded.add(
        RosterAssignment(
            league_id="00999",
            franchise_id="0001",
            player_id="99",
            status="ROSTER",
        )
    )
    _ranking(seeded, "99", 1)
    seeded.commit()

    result = bye_week_advice(seeded, "00999", 5, franchise_id="0001")

    assert result["configured"] is True
    assert result["franchise"] == {"id": "0001", "name": "Alpha"}
    assert result["week_summary"][4]["bye_count"] == 1


def test_bye_advisor_can_compare_one_selected_roster_player(seeded: Session) -> None:
    initialize_sources(seeded)
    seeded.add_all(
        [
            RosterAssignment(
                league_id="00999",
                franchise_id="0001",
                player_id="99",
                status="ROSTER",
            ),
            UserLeagueSetting(
                username="wilsonmw",
                league_id="00999",
                franchise_id="0001",
                auction_strategy_json={"template": "balanced"},
            ),
        ]
    )
    _ranking(seeded, "99", 1)
    seeded.commit()

    result = bye_week_advice(seeded, "00999", 5, "99")

    assert result["selected_player_id"] == "99"
    assert result["conflicts"][0]["selected_manually"] is True
    assert result["conflicts"][0]["is_bye"] is False
