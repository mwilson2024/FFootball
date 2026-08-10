from decimal import Decimal

from fastapi.testclient import TestClient

import app.main as main_module
from app.auth import SESSION_COOKIE, make_session_token
from app.config import get_settings
from app.db import get_db
from app.main import app
from app.models import SourcePlayerValue, UserLeagueSetting
from app.sources import initialize_sources


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
            assistant = client.get("/api/assistant/status?league_id=00999")
        assert leagues.status_code == 200
        assert leagues.json()[0]["id"] == "00999"
        assert league.status_code == 200
        assert league.json()["minimum_bid"] == "1.00"
        assert auction.status_code == 200
        assert auction.json()["league"]["id"] == "00999"
        assert assistant.status_code == 200
        assert assistant.json()["league_name"] == "Test League"
        assert assistant.json()["franchise_id"] == "0001"
        assert assistant.json()["franchise_name"] == "Alpha"
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
        assert response.status_code == 200
        assert 'id="assistant-window"' in response.text
        assert "New chat" in response.text
        assert 'id="assistant-team"' in response.text
        assert '"username": "tester"' in response.text
        assert '"league_id": "00999"' in response.text
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
        assert before.json()["nomination"]["current_franchise_name"] == "Alpha"
        assert response.status_code == 201
        assert after.status_code == 200
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
        assert reset.status_code == 200
        assert reset.json()["reset_count"] == 1
        assert reset.json()["live"]["is_live"] is False
        assert reset.json()["nomination"]["cursor"] == 0
        assert after.json()["purchases"] == []
        assert after.json()["nomination"]["current_franchise_name"] == "Alpha"
    finally:
        app.dependency_overrides.clear()


def test_local_csv_received_data_is_visible_without_unapproved_fields(seeded):
    initialize_sources(seeded)
    seeded.add(
        SourcePlayerValue(
            source_id="local_redraft_csv",
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
            response = client.get("/api/sources/local_redraft_csv/data")

        assert response.status_code == 200
        assert response.json()["total_count"] == 1
        assert response.json()["rows"][0]["raw"]["overall_rank"] == "4"
        assert "api_key" not in response.json()["rows"][0]["raw"]
    finally:
        app.dependency_overrides.clear()
