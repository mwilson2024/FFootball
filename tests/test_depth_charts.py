from sqlalchemy.orm import Session

from app.catalog import player_detail
from app.depth_charts import depth_chart_overview, normalize_team_code
from app.models import Player


def test_depth_chart_lists_all_teams_and_selects_cached_team(seeded: Session) -> None:
    running_back = seeded.get_one(Player, "0001234")
    quarterback = seeded.get_one(Player, "99")
    quarterback.nfl_team = "BUF"
    running_back.metadata_json = {"sleeper": {"depth_chart_position": "RB", "depth_chart_order": 1}}
    quarterback.metadata_json = {"sleeper": {"depth_chart_position": "QB", "depth_chart_order": 2}}
    seeded.commit()

    result = depth_chart_overview(seeded, "BUF")

    assert len(result["teams"]) == 32
    assert result["selected_team"] == {
        "code": "BUF",
        "name": "Buffalo Bills",
        "espn_code": "buf",
        "espn_url": "https://www.espn.com/nfl/team/depth/_/name/buf",
    }
    assert [row["player_id"] for row in result["players"]] == ["99", "0001234"]
    assert result["players"][0]["depth_order"] == 2
    assert result["players"][1]["depth_order"] == 1


def test_player_profile_exposes_bye_week(seeded: Session) -> None:
    player = seeded.get_one(Player, "0001234")
    player.bye_week = 7
    seeded.commit()

    detail = player_detail(seeded, "00999", "0001234")

    assert detail is not None
    assert detail["bye_week"] == 7


def test_depth_chart_normalizes_external_team_codes() -> None:
    assert normalize_team_code("gb") == "GBP"
    assert normalize_team_code("wsh") == "WAS"
