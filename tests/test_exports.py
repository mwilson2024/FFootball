import csv
from datetime import UTC, datetime
from decimal import Decimal
from xml.etree import ElementTree as ET

import app.exports as exports_module
from app.auction import add_purchase
from app.exports import CSV_HEADERS, build_xml, export_csv, validate_xml
from app.schemas import PurchaseCreate


def test_csv_round_trip_preserves_ids(seeded, tmp_path):
    add_purchase(
        seeded,
        PurchaseCreate(
            league_id="00999", franchise_id="0001", player_id="0001234", amount=Decimal("7")
        ),
    )
    path = export_csv(seeded, "00999", tmp_path)
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert list(rows[0]) == CSV_HEADERS
    assert rows[0]["league_id"] == "00999"
    assert rows[0]["franchise_id"] == "0001"
    assert rows[0]["player_id"] == "0001234"
    assert path.with_suffix(".csv.sha256").exists()
    assert path.with_suffix(".csv.manifest.json").exists()


def test_xml_matches_captured_schema(seeded):
    add_purchase(
        seeded,
        PurchaseCreate(
            league_id="00999", franchise_id="0001", player_id="0001234", amount=Decimal("7")
        ),
    )
    _, payload, count = build_xml(seeded, "00999")
    validate_xml(payload)
    root = ET.fromstring(payload)
    auction = root.find("./auctionUnit/auction")
    assert count == 1
    assert root.tag == "auctionResults"
    assert auction is not None
    assert auction.attrib["player"] == "0001234"
    assert auction.attrib["franchise"] == "0001"
    assert auction.attrib["winningBid"] == "7.00"


def test_xml_preview_is_stable_when_auction_has_not_changed(seeded, monkeypatch):
    purchase = add_purchase(
        seeded,
        PurchaseCreate(
            league_id="00999", franchise_id="0001", player_id="0001234", amount=Decimal("7")
        ),
    )
    purchase.created_at = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    purchase.updated_at = datetime(2026, 8, 29, 12, 5, tzinfo=UTC)
    seeded.commit()

    _, preview, _ = build_xml(seeded, "00999")

    class LaterClock:
        @staticmethod
        def now(_timezone):
            return datetime(2030, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(exports_module, "datetime", LaterClock)
    _, confirmation, _ = build_xml(seeded, "00999")

    assert confirmation == preview
    auction = ET.fromstring(confirmation).find("./auctionUnit/auction")
    assert auction is not None
    assert auction.attrib["timeStarted"] == str(int(purchase.created_at.timestamp()))
    assert auction.attrib["lastBidTime"] == str(int(purchase.updated_at.timestamp()))


def test_captured_fixture_is_valid():
    payload = __import__("pathlib").Path("tests/fixtures/mfl_2026_auction_results.xml").read_bytes()
    validate_xml(payload)
