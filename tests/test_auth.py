from fastapi.testclient import TestClient

import app.main as main_module
from app.auth import SESSION_COOKIE, make_session_token, mfl_league_ids, read_session_token
from app.main import app


def test_signed_session_rejects_tampering_and_expiry():
    secret = "s" * 48
    token, expected = make_session_token(
        secret, "manager", {"59885", "48465"}, max_age_seconds=60, now=1_000
    )
    assert read_session_token(secret, token, now=1_010) == expected
    assert read_session_token(secret, token + "x", now=1_010) is None
    assert read_session_token(secret, token, now=1_061) is None


def test_mfl_membership_parser_handles_single_and_list_leagues():
    payload = {
        "leagues": {
            "league": [
                {"id": "59885", "name": "ADFL"},
                {"league_id": "48465", "name": "TMFL"},
            ]
        }
    }
    assert mfl_league_ids(payload) == {"59885", "48465"}


def test_protected_routes_require_session_and_csrf():
    with TestClient(app) as client:
        assert client.get("/api/leagues").status_code == 401
        assert client.get("/", follow_redirects=False).status_code == 303
        token, session = make_session_token(
            main_module.SESSION_SIGNING_SECRET,
            "manager",
            {"59885", "48465"},
            max_age_seconds=3600,
        )
        client.cookies.set(SESSION_COOKIE, token)
        assert client.post("/logout").status_code == 403
        assert (
            client.post("/logout", headers={"X-CSRF-Token": session.csrf_token}).status_code == 204
        )


def test_railway_healthcheck_host_is_allowed_but_unknown_hosts_are_rejected():
    with TestClient(app) as client:
        response = client.get("/health", headers={"Host": "healthcheck.railway.app"})
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

        assert client.get("/health", headers={"Host": "untrusted.example"}).status_code == 400
