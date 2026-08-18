from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
from app.auth import SESSION_COOKIE, make_session_token
from app.db import get_db
from app.main import app
from app.models import (
    AuctionPurchase,
    DraftPick,
    MFLSnapshot,
    MockDraftPick,
    Player,
    UserLeagueSetting,
)
from app.users import bootstrap_user


def _draft_order_snapshot(seeded) -> None:
    now = datetime.now(UTC)
    seeded.add(
        MFLSnapshot(
            league_id="00999",
            season=2026,
            export_type="draftResults",
            source_url="https://api.myfantasyleague.com/2026/export",
            parameters_json={},
            payload_json={
                "draftResults": {
                    "draftUnit": {
                        "draftPick": [
                            {"round": "1", "pick": "1", "franchise": "0001"},
                            {"round": "1", "pick": "2", "franchise": "0002"},
                        ]
                    }
                }
            },
            fetched_at=now,
            expires_at=now + timedelta(minutes=15),
        )
    )
    seeded.commit()


def _session(username: str, leagues: set[str] | None = None):
    return make_session_token(
        main_module.SESSION_SIGNING_SECRET,
        username,
        leagues or {"00999"},
        max_age_seconds=3600,
    )


def test_real_draft_is_locked_and_shared_mock_picks_are_isolated(seeded) -> None:
    _draft_order_snapshot(seeded)
    bootstrap_user(seeded, "tester", admin_usernames={"wilsonmw"})

    def override_db():
        yield seeded

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            user_token, user_session = _session("tester")
            client.cookies.set(SESSION_COOKIE, user_token)
            locked = client.get("/api/draft/state?league_id=00999")
            real_pick = client.post(
                "/api/draft/picks",
                headers={"X-CSRF-Token": user_session.csrf_token},
                json={
                    "league_id": "00999",
                    "player_id": "0001234",
                    "franchise_id": "0001",
                    "overall_pick": 1,
                },
            )

            admin_token, admin_session = _session("wilsonmw")
            client.cookies.set(SESSION_COOKIE, admin_token)
            enabled = client.put(
                "/api/admin/mock-draft?league_id=00999",
                headers={"X-CSRF-Token": admin_session.csrf_token},
                json={"enabled": True},
            )

            client.cookies.set(SESSION_COOKIE, user_token)
            mock_state = client.get("/api/draft/state?league_id=00999")
            first = client.post(
                "/api/draft/picks",
                headers={"X-CSRF-Token": user_session.csrf_token},
                json={
                    "league_id": "00999",
                    "player_id": "0001234",
                    "overall_pick": 1,
                    "is_mock": True,
                },
            )
            stale = client.post(
                "/api/draft/picks",
                headers={"X-CSRF-Token": user_session.csrf_token},
                json={
                    "league_id": "00999",
                    "player_id": "99",
                    "overall_pick": 1,
                    "is_mock": True,
                },
            )
            after = client.get("/api/draft/state?league_id=00999")
            real_count_after_mock = seeded.scalar(select(func.count(DraftPick.id)))
            mock_count_after_mock = seeded.scalar(select(func.count(MockDraftPick.id)))

            client.cookies.set(SESSION_COOKIE, admin_token)
            real_live = client.put(
                "/api/draft/live?league_id=00999",
                headers={"X-CSRF-Token": admin_session.csrf_token},
                json={"is_live": True},
            )
            real_state = client.get("/api/draft/state?league_id=00999")
            created_real_pick = client.post(
                "/api/draft/picks",
                headers={"X-CSRF-Token": admin_session.csrf_token},
                json={
                    "league_id": "00999",
                    "player_id": "99",
                    "franchise_id": "0001",
                    "round": 1,
                    "pick": 1,
                    "overall_pick": 1,
                },
            )
            imported = client.post(
                "/api/draft/reconcile?league_id=00999&apply=true",
                headers={"X-CSRF-Token": admin_session.csrf_token},
                json={
                    "draftResults": {
                        "draftUnit": {
                            "draftPick": {
                                "round": "1",
                                "pick": "1",
                                "overallPick": "1",
                                "franchise": "0001",
                                "player": "99",
                            }
                        }
                    }
                },
            )
            imported_pick = seeded.scalar(select(DraftPick).where(DraftPick.player_id == "99"))
            assert imported_pick is not None

            client.cookies.set(SESSION_COOKIE, user_token)
            user_edit = client.patch(
                f"/api/draft/picks/{imported_pick.id}",
                headers={"X-CSRF-Token": user_session.csrf_token},
                json={
                    "player_id": "0001234",
                    "franchise_id": "0001",
                    "round": 1,
                    "pick": 1,
                    "overall_pick": 1,
                    "version": imported_pick.version,
                },
            )

            client.cookies.set(SESSION_COOKIE, admin_token)
            admin_edit = client.patch(
                f"/api/draft/picks/{imported_pick.id}",
                headers={"X-CSRF-Token": admin_session.csrf_token},
                json={
                    "player_id": "0001234",
                    "franchise_id": "0002",
                    "round": 1,
                    "pick": 1,
                    "overall_pick": 1,
                    "version": imported_pick.version,
                },
            )

        assert locked.json()["permissions"]["can_make_pick"] is False
        assert real_pick.status_code == 409
        assert real_pick.json()["detail"]["code"] == "draft_locked"
        assert enabled.status_code == 200
        assert mock_state.json()["mode"] == "mock"
        assert mock_state.json()["permissions"]["can_make_pick"] is True
        assert first.status_code == 201
        assert first.json()["source"] == "mock"
        assert first.json()["franchise_id"] == "0001"
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "mock_pick_moved"
        assert after.json()["current_drafter"]["franchise_id"] == "0002"
        assert real_count_after_mock == 0
        assert mock_count_after_mock == 1
        assert real_live.status_code == 200
        assert real_state.json()["mode"] == "real"
        assert real_state.json()["live"]["is_live"] is True
        assert real_state.json()["mock"]["enabled"] is False
        assert real_state.json()["permissions"]["can_make_pick"] is False
        assert "every 30 seconds" in real_state.json()["permissions"]["locked_reason"]
        assert created_real_pick.status_code == 409
        assert created_real_pick.json()["detail"]["code"] == "mfl_companion_only"
        assert imported.status_code == 200
        assert imported.json()["applied_count"] == 1
        assert imported_pick.source == "mfl"
        assert user_edit.status_code == 403
        assert admin_edit.status_code == 200
        assert admin_edit.json()["player_id"] == "0001234"
        assert admin_edit.json()["franchise_id"] == "0002"
    finally:
        app.dependency_overrides.clear()


def test_auction_closed_staging_and_live_permissions(seeded) -> None:
    bootstrap_user(seeded, "tester", admin_usernames={"wilsonmw"})

    def override_db():
        yield seeded

    def purchase(player_id: str) -> dict[str, object]:
        return {
            "league_id": "00999",
            "franchise_id": "0001",
            "player_id": player_id,
            "amount": "2",
            "status": "ROSTER",
        }

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            user_token, user_session = _session("tester")
            client.cookies.set(SESSION_COOKIE, user_token)
            closed = client.get("/api/auction/state?league_id=00999")
            closed_pick = client.post(
                "/api/auction/purchases",
                headers={"X-CSRF-Token": user_session.csrf_token},
                json=purchase("0001234"),
            )

            admin_token, admin_session = _session("wilsonmw")
            client.cookies.set(SESSION_COOKIE, admin_token)
            stage = client.put(
                "/api/admin/auction-stage?league_id=00999",
                headers={"X-CSRF-Token": admin_session.csrf_token},
                json={"enabled": True},
            )

            client.cookies.set(SESSION_COOKIE, user_token)
            staged_user_pick = client.post(
                "/api/auction/purchases",
                headers={"X-CSRF-Token": user_session.csrf_token},
                json=purchase("0001234"),
            )

            client.cookies.set(SESSION_COOKIE, admin_token)
            staged_admin_pick = client.post(
                "/api/auction/purchases",
                headers={"X-CSRF-Token": admin_session.csrf_token},
                json=purchase("0001234"),
            )
            rob_off = client.put(
                "/api/admin/auction-mode",
                headers={"X-CSRF-Token": admin_session.csrf_token},
                json={"enabled": False},
            )
            live = client.put(
                "/api/auction/live?league_id=00999",
                headers={"X-CSRF-Token": admin_session.csrf_token},
                json={"is_live": True},
            )

            client.cookies.set(SESSION_COOKIE, user_token)
            live_user_pick = client.post(
                "/api/auction/purchases",
                headers={"X-CSRF-Token": user_session.csrf_token},
                json=purchase("99"),
            )
            final_state = client.get("/api/auction/state?league_id=00999")

        assert closed.json()["phase"] == "closed"
        assert closed.json()["can_record_purchase"] is False
        assert closed_pick.status_code == 409
        assert closed_pick.json()["detail"]["code"] == "auction_closed"
        assert stage.json()["phase"] == "staging"
        assert staged_user_pick.status_code == 403
        assert staged_admin_pick.status_code == 201
        assert rob_off.json()["rob_mode"] is False
        assert live.status_code == 200
        assert live_user_pick.status_code == 201
        assert final_state.json()["phase"] == "live"
        assert final_state.json()["can_record_purchase"] is True
    finally:
        app.dependency_overrides.clear()


def test_interactive_auction_enforces_turns_shared_bids_and_admin_award(seeded) -> None:
    bootstrap_user(seeded, "tester", admin_usernames={"wilsonmw"})
    bootstrap_user(seeded, "bidder", admin_usernames={"wilsonmw"})
    seeded.add_all(
        [
            UserLeagueSetting(
                username="tester",
                league_id="00999",
                franchise_id="0001",
                auction_strategy_json={"template": "balanced"},
            ),
            UserLeagueSetting(
                username="bidder",
                league_id="00999",
                franchise_id="0002",
                auction_strategy_json={"template": "balanced"},
            ),
        ]
    )
    player = seeded.get(Player, "0001234")
    player.metadata_json = {"nflverse": {"headshot": "https://example.com/player.png"}}
    seeded.commit()

    def override_db():
        yield seeded

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            admin_token, admin_session = _session("wilsonmw")
            client.cookies.set(SESSION_COOKIE, admin_token)
            enabled = client.put(
                "/api/admin/interactive-auction?league_id=00999",
                headers={"X-CSRF-Token": admin_session.csrf_token},
                json={"enabled": True},
            )
            live = client.put(
                "/api/auction/live?league_id=00999",
                headers={"X-CSRF-Token": admin_session.csrf_token},
                json={"is_live": True},
            )

            bidder_token, bidder_session = _session("bidder")
            client.cookies.set(SESSION_COOKIE, bidder_token)
            bidder_before = client.get("/api/auction/state?league_id=00999")
            wrong_turn = client.post(
                "/api/auction/interactive/nominate",
                headers={"X-CSRF-Token": bidder_session.csrf_token},
                json={"league_id": "00999", "player_id": "0001234"},
            )

            user_token, user_session = _session("tester")
            client.cookies.set(SESSION_COOKIE, user_token)
            user_before = client.get("/api/auction/state?league_id=00999")
            manual_blocked = client.post(
                "/api/auction/purchases",
                headers={"X-CSRF-Token": user_session.csrf_token},
                json={
                    "league_id": "00999",
                    "franchise_id": "0001",
                    "player_id": "0001234",
                    "amount": "1",
                    "status": "ROSTER",
                },
            )
            nominated = client.post(
                "/api/auction/interactive/nominate",
                headers={"X-CSRF-Token": user_session.csrf_token},
                json={"league_id": "00999", "player_id": "0001234"},
            )

            client.cookies.set(SESSION_COOKIE, bidder_token)
            bid = client.post(
                "/api/auction/interactive/bids",
                headers={"X-CSRF-Token": bidder_session.csrf_token},
                json={"league_id": "00999", "amount": "2"},
            )
            non_admin_award = client.post(
                "/api/admin/interactive-auction/award?league_id=00999",
                headers={"X-CSRF-Token": bidder_session.csrf_token},
            )

            client.cookies.set(SESSION_COOKIE, admin_token)
            awarded = client.post(
                "/api/admin/interactive-auction/award?league_id=00999",
                headers={"X-CSRF-Token": admin_session.csrf_token},
            )
            after = client.get("/api/auction/state?league_id=00999")

        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
        assert live.status_code == 200
        assert bidder_before.json()["interactive_bidding"]["can_nominate"] is False
        assert wrong_turn.status_code == 403
        assert wrong_turn.json()["detail"]["code"] == "not_current_nominator"
        assert user_before.json()["interactive_bidding"]["can_nominate"] is True
        assert manual_blocked.status_code == 409
        assert manual_blocked.json()["detail"]["code"] == "interactive_auction_required"
        assert nominated.status_code == 201
        assert nominated.json()["player"]["headshot_url"] == "https://example.com/player.png"
        assert nominated.json()["high_bid_franchise_id"] == "0001"
        assert nominated.json()["current_bid"] == "1.00"
        assert bid.status_code == 201
        assert bid.json()["high_bid_franchise_id"] == "0002"
        assert bid.json()["current_bid"] == "2.00"
        assert non_admin_award.status_code == 403
        assert awarded.status_code == 201
        assert awarded.json()["franchise_id"] == "0002"
        assert awarded.json()["amount"] == "2.00"
        assert after.json()["interactive_bidding"]["active"] is False
        assert after.json()["nomination"]["current_franchise_id"] == "0002"
        assert seeded.scalar(select(func.count(AuctionPurchase.id))) == 1
    finally:
        app.dependency_overrides.clear()


def test_presence_heartbeat_marks_logged_in_user_online_for_admin(seeded) -> None:
    bootstrap_user(seeded, "tester", admin_usernames={"wilsonmw"})

    def override_db():
        yield seeded

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            user_token, user_session = _session("tester")
            client.cookies.set(SESSION_COOKIE, user_token)
            heartbeat = client.post(
                "/api/presence", headers={"X-CSRF-Token": user_session.csrf_token}
            )

            admin_token, _ = _session("wilsonmw")
            client.cookies.set(SESSION_COOKIE, admin_token)
            users = client.get("/api/admin/users")

        tester = next(row for row in users.json() if row["username"] == "tester")
        assert heartbeat.status_code == 204
        assert users.status_code == 200
        assert tester["is_online"] is True
        assert tester["last_seen_at"] is not None
    finally:
        app.dependency_overrides.clear()
