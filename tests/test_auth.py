from fastapi.testclient import TestClient

import app.main as main_module
from app.auth import (
    SESSION_COOKIE,
    make_session_token,
    mfl_league_ids,
    mfl_memberships,
    read_session_token,
)
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


def test_only_supported_quick_cards_allow_same_origin_framing():
    with TestClient(app) as client:
        ordinary = client.get("/health")
        assert ordinary.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in ordinary.headers["Content-Security-Policy"]

        token, _ = make_session_token(
            main_module.SESSION_SIGNING_SECRET, "manager", set(), max_age_seconds=3600
        )
        client.cookies.set(SESSION_COOKIE, token)
        embedded = client.get("/player/0001234?quick=1", follow_redirects=False)
        assert embedded.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in embedded.headers["Content-Security-Policy"]

        bye_card = client.get("/bye-advisor?quick=1", follow_redirects=False)
        assert bye_card.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in bye_card.headers["Content-Security-Policy"]

        ordinary_bye_page = client.get("/bye-advisor", follow_redirects=False)
        assert ordinary_bye_page.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in ordinary_bye_page.headers["Content-Security-Policy"]
