from fastapi.testclient import TestClient

import app.main as main_module
from app.auth import SESSION_COOKIE, make_session_token
from app.db import get_db
from app.main import app


def test_league_and_auction_state_are_json_serializable(seeded):
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
        assert leagues.status_code == 200
        assert leagues.json()[0]["id"] == "00999"
        assert league.status_code == 200
        assert league.json()["minimum_bid"] == "1.00"
        assert auction.status_code == 200
        assert auction.json()["league"]["id"] == "00999"
    finally:
        app.dependency_overrides.clear()
