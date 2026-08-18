import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from app.config import get_settings
from app.mfl import MFLError, MFLResponse
from app.models import LeagueType
from app.sync import _is_expected_unavailable, _lineup, _rules, _signals, sync_league


def test_live_mfl_lineup_shape_extracts_flex_without_counting_idp_ranges():
    lineup = _lineup(
        {
            "starters": {
                "iop_starters": "8",
                "idp_starters": "2",
                "count": "10",
                "position": [
                    {"name": "QB", "limit": "1"},
                    {"name": "RB", "limit": "1-3"},
                    {"name": "WR", "limit": "2-4"},
                    {"name": "TE", "limit": "1-2"},
                    {"name": "PK", "limit": "1"},
                    {"name": "DEF", "limit": "1"},
                    {"name": "LB", "limit": "1-2"},
                    {"name": "DB", "limit": "1-2"},
                ],
            }
        }
    )
    assert lineup["QB"] == 1
    assert lineup["FLEX"] == 1


def test_live_mfl_rule_shape_normalizes_receptions_and_te_premium():
    payload = {
        "rules": {
            "positionRules": [
                {
                    "positions": "RB,WR",
                    "rule": [
                        {"event": {"$t": "CC"}, "points": {"$t": "*1"}, "range": {"$t": "0-99"}}
                    ],
                },
                {
                    "positions": "TE",
                    "rule": [
                        {"event": {"$t": "CC"}, "points": {"$t": "*1.5"}, "range": {"$t": "0-99"}}
                    ],
                },
            ]
        }
    }
    rules = _rules(payload, {"CC": "Receptions"})
    assert Decimal(rules["receptions"]) == Decimal("1")
    assert Decimal(rules["te_premium"]) == Decimal("0.5")
    assert rules["RB,WR:CC"]["description"] == "Receptions"


def test_live_mfl_fr_rule_maps_to_offensive_fumble_recovery_length():
    payload = {
        "rules": {
            "positionRules": {
                "positions": "QB|RB|WR|TE|PK",
                "rule": [
                    {"event": {"$t": "FR"}, "points": {"$t": "3"}, "range": {"$t": "1-39"}},
                    {"event": {"$t": "FR"}, "points": {"$t": "4"}, "range": {"$t": "40-49"}},
                ],
            }
        }
    }
    rules = _rules(payload, {})
    assert rules["QB|RB|WR|TE|PK:FR"]["description"] == "Length of Offensive Fumble Recovery TD"
    assert (
        rules["QB|RB|WR|TE|PK:FR:40-49"]["description"] == "Length of Offensive Fumble Recovery TD"
    )


def test_expected_optional_exports_are_not_warnings():
    assert _is_expected_unavailable(
        "auctionResults", MFLError("{'$t': 'Auction has not been setup yet.'}")
    )
    assert _is_expected_unavailable(
        "selectedKeepers", MFLError("{'$t': 'No Select Keepers Event Defined.'}")
    )
    assert not _is_expected_unavailable("rules", MFLError("permission denied"))


def test_weekly_projection_shape_is_read_as_player_signal():
    response = MFLResponse(
        "projectedScores",
        {"projectedScores": {"week": "1", "playerScore": [{"id": "0001234", "score": "12.3"}]}},
        "https://example.test",
        __import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    assert _signals(response, "projectedScores")["0001234"]["score"] == "12.3"


def test_scoring_refresh_forces_only_mfl_rule_exports(seeded):
    calls = []

    class FakeClient:
        async def export(
            self,
            export_type,
            *,
            league_id=None,
            params=None,
            db=None,
            force=False,
        ):
            calls.append((export_type, force))
            payloads = {
                "league": {
                    "league": {
                        "id": "00999",
                        "name": "Test League",
                        "rosterSize": "4",
                        "franchises": {"franchise": []},
                    }
                },
                "rules": {"rules": {"positionRules": []}},
                "allRules": {"allRules": {"rule": []}},
            }
            return MFLResponse(
                export_type,
                payloads.get(export_type, {export_type: {}}),
                "https://api.myfantasyleague.com/2026/export",
                datetime.now(UTC),
            )

    asyncio.run(
        sync_league(
            seeded,
            FakeClient(),
            get_settings(),
            "00999",
            LeagueType.AUCTION,
            force_export_types={"league", "rules", "allRules"},
        )
    )

    forced = {export_type for export_type, force in calls if force}
    assert forced == {"league", "rules", "allRules"}
    assert any(export_type == "players" and not force for export_type, force in calls)
