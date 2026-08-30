from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.auth import SESSION_COOKIE, clear_login_failures, login_rate_key, make_session_token
from app.config import Settings
from app.db import get_db
from app.main import app
from app.models import AuctionPurchase, Franchise, ImportRecord, League
from scripts.import_auction_csv_as_draft import MFLImportError


def _complete_small_auction(seeded) -> None:
    league = seeded.get(League, ("00999", 2026))
    league.roster_size = 1
    for franchise in seeded.scalars(select(Franchise).where(Franchise.league_id == league.id)):
        franchise.roster_slots = 1
    seeded.add_all(
        [
            AuctionPurchase(
                league_id=league.id,
                franchise_id="0001",
                player_id="0001234",
                amount="9",
                status="ROSTER",
                purchase_order=1,
            ),
            AuctionPurchase(
                league_id=league.id,
                franchise_id="0002",
                player_id="99",
                amount="8",
                status="ROSTER",
                purchase_order=2,
            ),
        ]
    )
    seeded.commit()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        mfl_season=2026,
        mfl_enable_imports=True,
        export_directory=tmp_path / "exports",
        audit_directory=tmp_path / "audit",
    )


def _admin_session(client: TestClient):
    token, session = make_session_token(
        main_module.SESSION_SIGNING_SECRET,
        "wilsonmw",
        {"00999"},
        max_age_seconds=3600,
    )
    client.cookies.set(SESSION_COOKIE, token)
    return session


def test_web_draft_results_import_reauthenticates_current_user_and_logs_success(
    seeded,
    tmp_path,
    monkeypatch,
) -> None:
    _complete_small_auction(seeded)
    settings = _settings(tmp_path)
    captured: dict[str, str] = {}

    def override_db():
        yield seeded

    def fake_send(plan, artifacts, output_directory, **kwargs):
        captured["username"] = kwargs["username"]
        captured["password"] = kwargs["password"]
        response_path = output_directory / "mfl-response.xml"
        response_path.write_text("<status>OK</status>", encoding="utf-8")
        receipt_path = output_directory / "receipt.json"
        receipt_path.write_text("{}", encoding="utf-8")
        return {
            "verification": "matched",
            "expected_picks": len(plan.picks),
            "observed_export_picks": len(plan.picks),
            "league_host": "www49.myfantasyleague.com",
            "source_sha256": plan.source_sha256,
            "xml_sha256": "abc123",
            "receipt": str(receipt_path),
            "files": {"response": str(response_path)},
        }

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(main_module, "runtime_settings", lambda _db: settings)
    monkeypatch.setattr(main_module, "send_plan", fake_send)
    clear_login_failures(login_rate_key("testclient", "wilsonmw"))
    try:
        with TestClient(app) as client:
            session = _admin_session(client)
            preview = client.get("/api/auction/draft-results-import-preview?league_id=00999")
            payload = preview.json()
            response = client.post(
                "/api/auction/push-as-draft-results",
                headers={"X-CSRF-Token": session.csrf_token},
                json={
                    "league_id": "00999",
                    "confirmation_token": payload["confirmation_token"],
                    "confirmation_text": payload["confirmation_text"],
                    "password": "visible-but-never-stored",
                },
            )

        assert preview.status_code == 200
        assert payload["ready"] is True
        assert payload["reauthentication_username"] == "wilsonmw"
        assert response.status_code == 200
        assert response.json()["verification"] == "matched"
        assert captured == {
            "username": "wilsonmw",
            "password": "visible-but-never-stored",
        }
        record = seeded.scalar(select(ImportRecord).where(ImportRecord.league_id == "00999"))
        assert record is not None
        assert record.response_text == "<status>OK</status>"
        audit = (settings.audit_directory / "mfl-imports.jsonl").read_text(encoding="utf-8")
        assert "draft_results_import" in audit
        assert "visible-but-never-stored" not in audit
    finally:
        app.dependency_overrides.clear()


def test_web_draft_results_import_logs_mfl_permission_failure_without_password(
    seeded,
    tmp_path,
    monkeypatch,
) -> None:
    _complete_small_auction(seeded)
    settings = _settings(tmp_path)

    def override_db():
        yield seeded

    def denied(*_args, **_kwargs):
        raise MFLImportError("API requires commissioner access for league id 00999")

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(main_module, "runtime_settings", lambda _db: settings)
    monkeypatch.setattr(main_module, "send_plan", denied)
    clear_login_failures(login_rate_key("testclient", "wilsonmw"))
    try:
        with TestClient(app) as client:
            session = _admin_session(client)
            preview = client.get("/api/auction/draft-results-import-preview?league_id=00999").json()
            response = client.post(
                "/api/auction/push-as-draft-results",
                headers={"X-CSRF-Token": session.csrf_token},
                json={
                    "league_id": "00999",
                    "confirmation_token": preview["confirmation_token"],
                    "confirmation_text": preview["confirmation_text"],
                    "password": "must-not-appear-in-audit",
                },
            )

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "mfl_commissioner_access_required"
        audit = (settings.audit_directory / "mfl-imports.jsonl").read_text(encoding="utf-8")
        assert "draft_results_import_failed" in audit
        assert "API requires commissioner access" in audit
        assert "must-not-appear-in-audit" not in audit
    finally:
        app.dependency_overrides.clear()
