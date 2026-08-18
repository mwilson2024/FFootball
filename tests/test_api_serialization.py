from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.auth import SESSION_COOKIE, make_session_token
from app.config import get_settings
from app.db import get_db
from app.main import app
from app.models import (
    Franchise,
    League,
    Player,
    RosterAssignment,
    SourcePlayerValue,
    UserLeagueSetting,
    UserMFLMembership,
)
from app.sources import initialize_sources
from app.users import bootstrap_user


def test_league_and_auction_state_are_json_serializable(seeded):
    seeded.add(
        UserLeagueSetting(
            username="tester",
            league_id="00999",
            franchise_id="0001",
            auction_strategy_json={"template": "balanced"},
        )
    )
    seeded.commit()

    def override_db():
        yield seeded

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            token, _ = make_session_token(
                main_module.SESSION_SIGNING_SECRET,
                "tester",
                {"00999"},
                max_age_seconds=3600,
            )
            client.cookies.set(SESSION_COOKIE, token)
            leagues = client.get("/api/leagues")
            league = client.get("/api/leagues/00999")
            auction = client.get("/api/auction/state?league_id=00999")
            draft = client.get("/api/draft/state?league_id=00999")
            assistant = client.get("/api/assistant/status?league_id=00999")
        assert leagues.status_code == 200
        assert leagues.json()[0]["id"] == "00999"
        assert league.status_code == 200
        assert league.json()["minimum_bid"] == "1.00"
        assert auction.status_code == 200
        assert auction.json()["league"]["id"] == "00999"
        assert auction.json()["war_room"]["franchise_name"] == "Alpha"
        assert auction.json()["war_room"]["maximum_bid"] == "17.00"
        assert auction.json()["intelligence"]["market"]["open_slots"] == 8
        assert auction.json()["intelligence"]["opponent_insights"][0]["franchise_name"] == "Beta"
        assert draft.status_code == 200
        assert draft.json()["war_room"]["franchise_id"] == "0001"
        assert "position_runs" in draft.json()["intelligence"]
        assert draft.json()["intelligence"]["opponent_insights"][0]["franchise_name"] == "Beta"
        assert assistant.status_code == 200
        assert assistant.json()["league_name"] == "Test League"
        assert assistant.json()["franchise_id"] == "0001"
        assert assistant.json()["franchise_name"] == "Alpha"
    finally:
        app.dependency_overrides.clear()


def test_pages_default_to_only_the_signed_in_users_leagues(seeded):
    seeded.add(
        League(
            id="00888",
            season=2026,
            league_type="keeper",
            name="User Keeper League",
            roster_size=4,
            starting_budget=None,
            minimum_bid=Decimal("1"),
            settings_json={},
            scoring_rules_json={},
            lineup_json={"QB": 1},
            warnings_json=[],
        )
    )
    seeded.commit()

    def override_db():
        yield seeded

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            keeper_token, _ = make_session_token(
                main_module.SESSION_SIGNING_SECRET,
                "keeper-user",
                {"00888"},
                max_age_seconds=3600,
            )
            client.cookies.set(SESSION_COOKIE, keeper_token)
            players = client.get("/players")
            draft = client.get("/draft")
            no_auction = client.get("/auction")

            auction_token, _ = make_session_token(
                main_module.SESSION_SIGNING_SECRET,
                "auction-user",
                {"00999"},
                max_age_seconds=3600,
            )
            client.cookies.set(SESSION_COOKIE, auction_token)
            auction = client.get("/auction")

        assert players.status_code == 200
        assert 'window.SELECTED_LEAGUE_ID="00888"' in players.text
        assert 'window.SELECTED_LEAGUE_ID="00888"' in draft.text
        assert "No auction league is connected to your account" in no_auction.text
        assert 'window.AUCTION_LEAGUE_ID="00999"' in auction.text
        assert "/api/auction/export.csv?league_id=00999" in auction.text
        assert "User Keeper League" not in auction.text
    finally:
        app.dependency_overrides.clear()


def test_admin_league_format_change_is_shared_with_every_joined_user(seeded):
    league = seeded.scalar(select(League).where(League.id == "00999"))
    league.league_type = "keeper"
    league.starting_budget = None
    league_franchises = list(
        seeded.scalars(select(Franchise).where(Franchise.league_id == "00999"))
    )
    for franchise in league_franchises:
        franchise.starting_budget = Decimal("0")
    seeded.commit()
    bootstrap_user(seeded, "wilsonmw", admin_usernames={"wilsonmw"})
    bootstrap_user(seeded, "league-member", admin_usernames={"wilsonmw"})

    def override_db():
        yield seeded

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            admin_token, admin_session = make_session_token(
                main_module.SESSION_SIGNING_SECRET,
                "wilsonmw",
                {"00999"},
                max_age_seconds=3600,
            )
            client.cookies.set(SESSION_COOKIE, admin_token)
            changed = client.put(
                "/api/admin/leagues/00999/format",
                headers={"X-CSRF-Token": admin_session.csrf_token},
                json={"league_type": "auction"},
            )

            member_token, member_session = make_session_token(
                main_module.SESSION_SIGNING_SECRET,
                "league-member",
                {"00999"},
                max_age_seconds=3600,
            )
            client.cookies.set(SESSION_COOKIE, member_token)
            member_account = client.get("/api/account")
            forbidden = client.put(
                "/api/admin/leagues/00999/format",
                headers={"X-CSRF-Token": member_session.csrf_token},
                json={"league_type": "keeper"},
            )

        assert changed.status_code == 200
        assert changed.json()["league_type"] == "auction"
        assert changed.json()["applies_to_all_users"] is True
        assert member_account.json()["leagues"][0]["type"] == "auction"
        assert forbidden.status_code == 403
        assert league.league_type == "auction"
        expected_budget = Decimal(changed.json()["starting_budget"])
        assert expected_budget > 0
        assert league.starting_budget == expected_budget
        assert all(item.starting_budget == expected_budget for item in league_franchises)
    finally:
        app.dependency_overrides.clear()


def test_scoring_refresh_forces_mfl_rules_and_preserves_league_format(seeded, monkeypatch):
    captured = {}

    class FakeMFLClient:
        def __init__(self, _settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def fake_sync(
        _db,
        _client,
        _settings,
        league_id,
        league_type,
        *,
        force_export_types=None,
    ):
        captured.update(
            {
                "league_id": league_id,
                "league_type": league_type,
                "force_export_types": force_export_types,
            }
        )
        return {"league_id": league_id, "warnings": []}

    def override_db():
        yield seeded

    monkeypatch.setattr(main_module, "MFLClient", FakeMFLClient)
    monkeypatch.setattr(main_module, "sync_league", fake_sync)
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            token, session = make_session_token(
                main_module.SESSION_SIGNING_SECRET,
                "league-member",
                {"00999"},
                max_age_seconds=3600,
            )
            client.cookies.set(SESSION_COOKIE, token)
            response = client.post(
                "/api/leagues/00999/scoring-rules/sync",
                headers={"X-CSRF-Token": session.csrf_token},
            )

        assert response.status_code == 200
        assert response.json()["sync"]["league_id"] == "00999"
        assert captured == {
            "league_id": "00999",
            "league_type": main_module.LeagueType.AUCTION,
            "force_export_types": {"league", "rules", "allRules"},
        }
    finally:
        app.dependency_overrides.clear()


def test_existing_shared_format_cannot_be_overwritten_when_another_user_joins(
    seeded, monkeypatch
):
    seeded.add(
        UserMFLMembership(
            username="league-member",
            season=2026,
            league_id="00999",
            league_name="Test League",
            franchise_id="0001",
        )
    )
    seeded.commit()
    captured = {}

    class FakeMFLClient:
        def __init__(self, _settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def fake_sync(_db, _client, _settings, league_id, league_type):
        captured.update({"league_id": league_id, "league_type": league_type})
        return {}

    def override_db():
        yield seeded

    monkeypatch.setattr(main_module, "MFLClient", FakeMFLClient)
    monkeypatch.setattr(main_module, "sync_league", fake_sync)
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            token, session = make_session_token(
                main_module.SESSION_SIGNING_SECRET,
                "league-member",
                {"00999"},
                max_age_seconds=3600,
            )
            client.cookies.set(SESSION_COOKIE, token)
            response = client.post(
                "/api/account/leagues",
                headers={"X-CSRF-Token": session.csrf_token},
                json={"league_id": "00999", "league_type": "keeper"},
            )

        assert response.status_code == 200
        assert response.json()["league_type"] == "auction"
        assert captured == {"league_id": "00999", "league_type": main_module.LeagueType.AUCTION}
    finally:
        app.dependency_overrides.clear()


def test_authenticated_pages_render_persistent_chat_shell(seeded, monkeypatch):
    monkeypatch.setattr(get_settings(), "openai_api_key", "test-key")

    def override_db():
        yield seeded

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            token, _ = make_session_token(
                main_module.SESSION_SIGNING_SECRET,
                "tester",
                {"00999"},
                max_age_seconds=3600,
            )
            client.cookies.set(SESSION_COOKIE, token)
            response = client.get("/cheat-sheet?league_id=00999")
            links = client.get("/links")
        assert response.status_code == 200
        assert 'id="assistant-window"' in response.text
        assert "New chat" in response.text
        assert 'id="assistant-team"' in response.text
        assert '"username": "tester"' in response.text
        assert '"league_id": "00999"' in response.text
        assert links.status_code == 200
        assert "PFF Fantasy Draft Rankings" in links.text
        assert "2026 Salary-Cap Draft Strategy" in links.text
    finally:
        app.dependency_overrides.clear()


def test_recording_purchase_advances_shared_nomination_state(seeded):
    def override_db():
        yield seeded

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            token, session = make_session_token(
                main_module.SESSION_SIGNING_SECRET,
                "wilsonmw",
                {"00999"},
                max_age_seconds=3600,
            )
            client.cookies.set(SESSION_COOKIE, token)
            before = client.get("/api/auction/state?league_id=00999")
            staged = client.put(
                "/api/admin/auction-stage?league_id=00999",
                headers={"X-CSRF-Token": session.csrf_token},
                json={"enabled": True},
            )
            response = client.post(
                "/api/auction/purchases",
                headers={"X-CSRF-Token": session.csrf_token},
                json={
                    "league_id": "00999",
                    "franchise_id": "0001",
                    "player_id": "0001234",
                    "amount": "2",
                    "status": "ROSTER",
                },
            )
            after = client.get("/api/auction/state?league_id=00999")

        assert before.status_code == 200
        assert staged.status_code == 200
        assert before.json()["nomination"]["current_franchise_name"] == "Alpha"
        assert response.status_code == 201
        assert after.status_code == 200
        assert after.json()["purchases"][0]["player_position"] == "RB"
        assert after.json()["war_room"]["configured"] is False
        assert after.json()["intelligence"]["latest_purchase"]["player_name"] == "Leading Zero"
        assert after.json()["intelligence"]["recent_position_counts"] == {"RB": 1}
        assert after.json()["nomination"]["current_franchise_name"] == "Beta"
        assert after.json()["nomination"]["cursor"] == 1
    finally:
        app.dependency_overrides.clear()


def test_admin_can_reset_local_auction_and_nomination_state(seeded):
    def override_db():
        yield seeded

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            token, session = make_session_token(
                main_module.SESSION_SIGNING_SECRET,
                "wilsonmw",
                {"00999"},
                max_age_seconds=3600,
            )
            client.cookies.set(SESSION_COOKIE, token)
            staged = client.put(
                "/api/admin/auction-stage?league_id=00999",
                headers={"X-CSRF-Token": session.csrf_token},
                json={"enabled": True},
            )
            created = client.post(
                "/api/auction/purchases",
                headers={"X-CSRF-Token": session.csrf_token},
                json={
                    "league_id": "00999",
                    "franchise_id": "0001",
                    "player_id": "0001234",
                    "amount": "2",
                    "status": "ROSTER",
                },
            )
            reset = client.post(
                "/api/auction/reset?league_id=00999",
                headers={"X-CSRF-Token": session.csrf_token},
            )
            after = client.get("/api/auction/state?league_id=00999")

        assert created.status_code == 201
        assert staged.status_code == 200
        assert reset.status_code == 200
        assert reset.json()["reset_count"] == 1
        assert reset.json()["live"]["is_live"] is False
        assert reset.json()["nomination"]["cursor"] == 0
        assert after.json()["purchases"] == []
        assert after.json()["nomination"]["current_franchise_name"] == "Alpha"
    finally:
        app.dependency_overrides.clear()


def test_rob_mode_controls_who_can_record_auction_purchases(seeded):
    bootstrap_user(seeded, "tester", admin_usernames={"wilsonmw"})

    def override_db():
        yield seeded

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            user_token, user_session = make_session_token(
                main_module.SESSION_SIGNING_SECRET,
                "tester",
                {"00999"},
                max_age_seconds=3600,
            )
            client.cookies.set(SESSION_COOKIE, user_token)
            admin_list_blocked = client.get("/api/admin/users")
            commissioner_blocked = client.get("/api/admin/commissioner-imports")
            blocked = client.post(
                "/api/auction/purchases",
                headers={"X-CSRF-Token": user_session.csrf_token},
                json={
                    "league_id": "00999",
                    "franchise_id": "0001",
                    "player_id": "0001234",
                    "amount": "2",
                    "status": "ROSTER",
                },
            )

            admin_token, admin_session = make_session_token(
                main_module.SESSION_SIGNING_SECRET,
                "wilsonmw",
                {"00999"},
                max_age_seconds=3600,
            )
            client.cookies.set(SESSION_COOKIE, admin_token)
            live = client.put(
                "/api/auction/live?league_id=00999",
                headers={"X-CSRF-Token": admin_session.csrf_token},
                json={"is_live": True},
            )
            mode = client.put(
                "/api/admin/auction-mode",
                headers={"X-CSRF-Token": admin_session.csrf_token},
                json={"enabled": False},
            )
            commissioner = client.put(
                "/api/admin/commissioner-imports",
                headers={"X-CSRF-Token": admin_session.csrf_token},
                json={"enabled": True},
            )

            client.cookies.set(SESSION_COOKIE, user_token)
            allowed = client.post(
                "/api/auction/purchases",
                headers={"X-CSRF-Token": user_session.csrf_token},
                json={
                    "league_id": "00999",
                    "franchise_id": "0001",
                    "player_id": "0001234",
                    "amount": "2",
                    "status": "ROSTER",
                },
            )

        assert admin_list_blocked.status_code == 403
        assert commissioner_blocked.status_code == 403
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "auction_closed"
        assert live.status_code == 200
        assert mode.status_code == 200
        assert mode.json() == {"rob_mode": False}
        assert commissioner.status_code == 200
        assert commissioner.json()["enabled"] is True
        assert allowed.status_code == 201
    finally:
        app.dependency_overrides.clear()


def test_admin_can_promote_existing_users_but_cannot_demote_self(seeded):
    bootstrap_user(seeded, "alice", display_name="Alice", admin_usernames={"wilsonmw"})

    def override_db():
        yield seeded

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            admin_token, admin_session = make_session_token(
                main_module.SESSION_SIGNING_SECRET,
                "wilsonmw",
                {"00999"},
                max_age_seconds=3600,
            )
            client.cookies.set(SESSION_COOKIE, admin_token)
            users = client.get("/api/admin/users")
            promoted = client.put(
                "/api/admin/users/alice/role",
                headers={"X-CSRF-Token": admin_session.csrf_token},
                json={"is_admin": True},
            )

            alice_token, alice_session = make_session_token(
                main_module.SESSION_SIGNING_SECRET,
                "alice",
                {"00999"},
                max_age_seconds=3600,
            )
            client.cookies.set(SESSION_COOKIE, alice_token)
            alice_users = client.get("/api/admin/users")
            self_demotion = client.put(
                "/api/admin/users/alice/role",
                headers={"X-CSRF-Token": alice_session.csrf_token},
                json={"is_admin": False},
            )

        assert users.status_code == 200
        assert any(row["username"] == "alice" for row in users.json())
        assert promoted.status_code == 200
        assert promoted.json()["is_admin"] is True
        assert alice_users.status_code == 200
        assert self_demotion.status_code == 409
        assert self_demotion.json()["detail"]["code"] == "self_demotion_blocked"
    finally:
        app.dependency_overrides.clear()


def test_local_csv_received_data_is_visible_without_unapproved_fields(seeded):
    initialize_sources(seeded)
    seeded.add(
        SourcePlayerValue(
            source_id="fantasypros_redraft_csv",
            league_id="00999",
            player_id="0001234",
            value_type="rank",
            raw_value_json={
                "player_name": "Leading Zero",
                "team": "BUF",
                "position": "RB",
                "overall_rank": "4",
                "position_rank": "2",
                "tier": 1,
                "source_file": "redraft.csv",
                "api_key": "must-never-be-returned",
            },
            normalized_value=Decimal("4"),
            snapshot_id="local-csv-test",
        )
    )
    seeded.commit()

    def override_db():
        yield seeded

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            token, _ = make_session_token(
                main_module.SESSION_SIGNING_SECRET,
                "tester",
                {"00999"},
                max_age_seconds=3600,
            )
            client.cookies.set(SESSION_COOKIE, token)
            response = client.get("/api/sources/fantasypros_redraft_csv/data")
            download = client.get("/api/sources/fantasypros_redraft_csv/download.csv")
            source_list = client.get("/api/sources")

        assert response.status_code == 200
        assert response.json()["total_count"] == 1
        assert response.json()["rows"][0]["raw"]["overall_rank"] == "4"
        assert "api_key" not in response.json()["rows"][0]["raw"]
        assert download.status_code == 200
        assert "FantasyPros_2026_Draft_ALL_Rankings.csv" in download.headers["content-disposition"]
        fantasypros = next(
            item for item in source_list.json() if item["id"] == "fantasypros_redraft_csv"
        )
        assert fantasypros["name"] == "FantasyPros 2026 Redraft Rankings"
        assert fantasypros["visibility"] == "shared"
        assert "FantasyPros_2026_Draft_ALL_Rankings.csv" in fantasypros["attribution"]
    finally:
        app.dependency_overrides.clear()


def test_chatgpt_player_comparison_uses_server_board_values(seeded, monkeypatch):
    initialize_sources(seeded)
    captured = {}

    async def fake_assistant(db, settings, league_id, message, history):
        captured.update({"league_id": league_id, "message": message, "history": history})
        return "Choose Leading Zero because the live league values favor the running back."

    monkeypatch.setattr(main_module, "ask_assistant", fake_assistant)

    def override_db():
        yield seeded

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            token, session = make_session_token(
                main_module.SESSION_SIGNING_SECRET,
                "tester",
                {"00999"},
                max_age_seconds=3600,
            )
            client.cookies.set(SESSION_COOKIE, token)
            response = client.post(
                "/api/leagues/00999/compare/chatgpt",
                headers={"X-CSRF-Token": session.csrf_token},
                json={"player_ids": ["0001234", "99"]},
            )

        assert response.status_code == 200
        assert response.json()["answer"].startswith("Choose Leading Zero")
        assert [item["player_id"] for item in response.json()["players"]] == [
            "0001234",
            "99",
        ]
        assert captured["league_id"] == "00999"
        assert "Leading Zero" in captured["message"]
        assert "Quarter Back" in captured["message"]
        assert captured["history"] == []
    finally:
        app.dependency_overrides.clear()


def test_player_comparison_recommends_team_need_then_overall_rank(seeded):
    initialize_sources(seeded)
    seeded.add_all(
        [
            Player(id="owned-qb", name="Roster Quarterback", position="QB", nfl_team="BUF"),
            RosterAssignment(
                league_id="00999",
                franchise_id="0001",
                player_id="owned-qb",
            ),
            UserLeagueSetting(
                username="tester",
                league_id="00999",
                franchise_id="0001",
                auction_strategy_json={"template": "balanced"},
            ),
        ]
    )
    seeded.commit()

    def override_db():
        yield seeded

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            token, session = make_session_token(
                main_module.SESSION_SIGNING_SECRET,
                "tester",
                {"00999"},
                max_age_seconds=3600,
            )
            client.cookies.set(SESSION_COOKIE, token)
            response = client.post(
                "/api/leagues/00999/compare/recommendation",
                headers={"X-CSRF-Token": session.csrf_token},
                json={"player_ids": ["0001234", "99"]},
            )

        assert response.status_code == 200, response.text
        result = response.json()
        assert result["franchise_name"] == "Alpha"
        assert result["needs"] == {"QB": 0, "RB": 1}
        assert result["recommended_player_id"] == "0001234"
        assert result["candidates"][0]["player_name"] == "Leading Zero"
        assert result["candidates"][0]["need_slots"] == 1
        assert "fills 1 open RB starter slot" in result["reason"]
    finally:
        app.dependency_overrides.clear()
