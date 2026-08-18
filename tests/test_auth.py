from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.auth import (
    SESSION_COOKIE,
    make_session_token,
    mfl_league_ids,
    mfl_memberships,
    read_session_token,
)
from app.db import get_db
from app.main import app
from app.models import UserMFLMembership


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


def test_mfl_membership_parser_keeps_franchise_association():
    payload = {
        "leagues": {
            "league": [
                {"id": "59885", "name": "ADFL", "franchise_id": "0002"},
                {
                    "league_id": "48465",
                    "name": "TMFL",
                    "franchise": {"id": "0007"},
                    "url": "https://www49.myfantasyleague.com/2026/home/48465",
                },
            ]
        }
    }

    assert mfl_memberships(payload) == [
        {
            "league_id": "59885",
            "league_name": "ADFL",
            "franchise_id": "0002",
            "source_url": None,
        },
        {
            "league_id": "48465",
            "league_name": "TMFL",
            "franchise_id": "0007",
            "source_url": "https://www49.myfantasyleague.com/2026/home/48465",
        },
    ]


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


def test_login_accepts_and_stores_the_users_own_mfl_leagues(seeded, monkeypatch):
    class FakeMFLClient:
        def __init__(self, _settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def authenticate(self, username, password):
            assert username == "new-user"
            assert password == "test-password"

        async def export(self, export_type, *, force=False):
            assert export_type == "myleagues"
            assert force is True
            return type(
                "Result",
                (),
                {
                    "payload": {
                        "leagues": {
                            "league": {
                                "id": "77777",
                                "name": "User's Own League",
                                "franchise_id": "0007",
                            }
                        }
                    }
                },
            )()

    def override_db():
        yield seeded

    monkeypatch.setattr(main_module, "MFLClient", FakeMFLClient)
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            response = client.post(
                "/login",
                data={"username": "new-user", "password": "test-password", "next": "/"},
                follow_redirects=False,
            )
            token = client.cookies.get(SESSION_COOKIE)

        assert response.status_code == 303
        assert token is not None
        session = read_session_token(main_module.SESSION_SIGNING_SECRET, token)
        assert session is not None
        assert set(session.league_ids) == {"77777"}
        membership = seeded.scalar(
            select(UserMFLMembership).where(UserMFLMembership.username == "new-user")
        )
        assert membership is not None
        assert membership.league_id == "77777"
        assert membership.franchise_id == "0007"
    finally:
        app.dependency_overrides.clear()
