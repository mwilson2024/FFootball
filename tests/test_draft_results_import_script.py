import csv
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from scripts.import_auction_csv_as_draft import (
    ImportValidationError,
    _league_host_from_export,
    _response_is_html,
    build_draft_xml,
    load_plan,
    validate_draft_xml,
    write_artifacts,
)

HEADERS = [
    "league_id",
    "season",
    "franchise_id",
    "franchise_name",
    "player_id",
    "player_name",
    "position",
    "nfl_team",
    "auction_value",
    "status",
    "purchase_order",
]


def _csv(path: Path, rows: list[list[str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(HEADERS)
        writer.writerows(rows)
    return path


def test_auction_csv_maps_to_mfl_draft_rounds_and_preserves_audit(tmp_path) -> None:
    source = _csv(
        tmp_path / "auction.csv",
        [
            [
                "48465",
                "2026",
                "0001",
                "Alpha",
                "00101",
                "Player One",
                "RB",
                "BUF",
                "10",
                "ROSTER",
                "1",
            ],
            [
                "48465",
                "2026",
                "0002",
                "Beta",
                "00102",
                "Player Two",
                "WR",
                "DET",
                "11",
                "ROSTER",
                "2",
            ],
            [
                "48465",
                "2026",
                "0002",
                "Beta",
                "00103",
                "Player Three",
                "QB",
                "NEP",
                "12",
                "ROSTER",
                "3",
            ],
            [
                "48465",
                "2026",
                "0001",
                "Alpha",
                "00104",
                "Player Four",
                "TE",
                "DAL",
                "13",
                "ROSTER",
                "4",
            ],
        ],
    )

    plan = load_plan(source)
    payload = build_draft_xml(plan)
    validate_draft_xml(payload, 4)
    picks = ET.fromstring(payload).findall("./draftUnit/draftPick")

    assert plan.teams_per_round == 2
    assert [(pick.attrib["round"], pick.attrib["pick"]) for pick in picks] == [
        ("1", "1"),
        ("1", "2"),
        ("2", "1"),
        ("2", "2"),
    ]
    assert picks[0].attrib["player"] == "00101"
    assert picks[0].attrib["franchise"] == "0001"
    assert "winningBid" not in picks[0].attrib
    artifacts = write_artifacts(plan, tmp_path / "out")
    assert artifacts["xml"].exists()
    assert artifacts["audit"].read_text(encoding="utf-8").count('"auction_value"') == 4
    assert artifacts["normalized_csv"].exists()
    assert artifacts["checksums"].exists()


def test_duplicate_player_is_rejected(tmp_path) -> None:
    source = _csv(
        tmp_path / "duplicate.csv",
        [
            [
                "48465",
                "2026",
                "0001",
                "Alpha",
                "00101",
                "Player One",
                "RB",
                "BUF",
                "10",
                "ROSTER",
                "1",
            ],
            [
                "48465",
                "2026",
                "0002",
                "Beta",
                "00101",
                "Player One",
                "RB",
                "BUF",
                "11",
                "ROSTER",
                "2",
            ],
        ],
    )

    with pytest.raises(ImportValidationError, match="Duplicate player IDs"):
        load_plan(source)


def test_non_contiguous_purchase_order_is_rejected(tmp_path) -> None:
    source = _csv(
        tmp_path / "orders.csv",
        [
            [
                "48465",
                "2026",
                "0001",
                "Alpha",
                "00101",
                "Player One",
                "RB",
                "BUF",
                "10",
                "ROSTER",
                "1",
            ],
            [
                "48465",
                "2026",
                "0002",
                "Beta",
                "00102",
                "Player Two",
                "WR",
                "DET",
                "11",
                "ROSTER",
                "3",
            ],
        ],
    )

    with pytest.raises(ImportValidationError, match="continuous"):
        load_plan(source)


def test_league_host_is_discovered_from_draft_export() -> None:
    payload = b"""<?xml version="1.0"?><draftResults><draftUnit
        static_url="https://www49.myfantasyleague.com/fflnetdynamic2026/48465_LEAGUE_draft_results.xml"
    /></draftResults>"""

    assert _league_host_from_export(payload) == "www49.myfantasyleague.com"


def test_html_response_is_not_treated_as_import_success() -> None:
    assert _response_is_html(b"<!DOCTYPE html><html><title>MFL Developers Program</title></html>")
    assert not _response_is_html(b'<?xml version="1.0"?><status>OK</status>')
