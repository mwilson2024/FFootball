"""Convert a DraftDesk auction CSV into an MFL offline draftResults import.

This script is intentionally standalone and uses only the Python standard library so it can be
copied to a disposable VM. It is preview-only unless ``--send`` is supplied. Sending requires an
MFL account with commissioner access and an additional typed confirmation.

Example preview:

    python import_auction_csv_as_draft.py mfl_auction_results_48465_2026.csv

Example commissioner import:

    set MFL_USERNAME=commissioner_name
    set MFL_PASSWORD=commissioner_password
    python import_auction_csv_as_draft.py mfl_auction_results_48465_2026.csv --send

MFL documents draftResults imports as destructive to existing draft results. The script therefore
downloads and stores the current MFL draftResults response before sending anything.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener
from xml.etree import ElementTree as ET

REQUIRED_COLUMNS = {
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
}
VALID_STATUSES = {"ROSTER", "TAXI_SQUAD", "INJURED_RESERVE"}
STATUS_ALIASES = {
    "R": "ROSTER",
    "ROSTER": "ROSTER",
    "TS": "TAXI_SQUAD",
    "TAXI": "TAXI_SQUAD",
    "TAXI_SQUAD": "TAXI_SQUAD",
    "IR": "INJURED_RESERVE",
    "INJURED_RESERVE": "INJURED_RESERVE",
}


class ImportValidationError(ValueError):
    pass


class MFLImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class DraftPick:
    league_id: str
    season: int
    round: int
    pick: int
    overall_pick: int
    franchise_id: str
    franchise_name: str
    player_id: str
    player_name: str
    position: str
    nfl_team: str
    status: str
    auction_value: str
    timestamp: int


@dataclass(frozen=True)
class ImportPlan:
    source_file: str
    source_sha256: str
    league_id: str
    season: int
    unit: str
    teams_per_round: int
    picks: list[DraftPick]
    warnings: list[str]


def _required(value: str | None, label: str, row_number: int) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ImportValidationError(f"Row {row_number}: {label} is required")
    return cleaned


def _positive_int(value: str | None, label: str, row_number: int) -> int:
    cleaned = _required(value, label, row_number)
    try:
        parsed = int(cleaned)
    except ValueError as exc:
        raise ImportValidationError(f"Row {row_number}: {label} must be an integer") from exc
    if parsed <= 0:
        raise ImportValidationError(f"Row {row_number}: {label} must be positive")
    return parsed


def _money(value: str | None, row_number: int) -> str:
    cleaned = _required(value, "auction_value", row_number)
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ImportValidationError(f"Row {row_number}: auction_value is not numeric") from exc
    if amount < 0:
        raise ImportValidationError(f"Row {row_number}: auction_value cannot be negative")
    return format(amount.quantize(Decimal("0.01")), "f")


def load_plan(
    csv_path: Path,
    *,
    league_id: str | None = None,
    season: int | None = None,
    teams_per_round: int | None = None,
    unit: str = "LEAGUE",
) -> ImportPlan:
    content = csv_path.read_bytes()
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        columns = set(reader.fieldnames or [])
        missing = sorted(REQUIRED_COLUMNS - columns)
        if missing:
            raise ImportValidationError(f"CSV is missing required columns: {', '.join(missing)}")
        raw_rows = [
            dict(row) for row in reader if any((value or "").strip() for value in row.values())
        ]
    if not raw_rows:
        raise ImportValidationError("CSV contains no purchase rows")

    csv_leagues = {
        _required(row.get("league_id"), "league_id", index) for index, row in enumerate(raw_rows, 2)
    }
    csv_seasons = {
        _positive_int(row.get("season"), "season", index) for index, row in enumerate(raw_rows, 2)
    }
    if len(csv_leagues) != 1:
        raise ImportValidationError(f"CSV contains multiple league IDs: {sorted(csv_leagues)}")
    if len(csv_seasons) != 1:
        raise ImportValidationError(f"CSV contains multiple seasons: {sorted(csv_seasons)}")
    selected_league = league_id or next(iter(csv_leagues))
    selected_season = season or next(iter(csv_seasons))
    if selected_league not in csv_leagues:
        raise ImportValidationError(
            f"Requested league {selected_league} does not match CSV league "
            f"{next(iter(csv_leagues))}"
        )
    if selected_season not in csv_seasons:
        raise ImportValidationError(
            f"Requested season {selected_season} does not match CSV season "
            f"{next(iter(csv_seasons))}"
        )

    franchise_ids = {
        _required(row.get("franchise_id"), "franchise_id", index)
        for index, row in enumerate(raw_rows, 2)
    }
    inferred_team_count = len(franchise_ids)
    selected_team_count = teams_per_round or inferred_team_count
    if selected_team_count <= 0:
        raise ImportValidationError("teams_per_round must be positive")
    if teams_per_round is not None and teams_per_round != inferred_team_count:
        raise ImportValidationError(
            f"--teams-per-round={teams_per_round} does not match "
            f"{inferred_team_count} unique CSV franchises"
        )

    parsed_rows: list[tuple[int, dict[str, str], int]] = []
    for row_number, row in enumerate(raw_rows, 2):
        order = _positive_int(row.get("purchase_order"), "purchase_order", row_number)
        parsed_rows.append((order, row, row_number))
    parsed_rows.sort(key=lambda item: item[0])
    orders = [item[0] for item in parsed_rows]
    expected_orders = list(range(1, len(parsed_rows) + 1))
    if orders != expected_orders:
        missing_orders = sorted(set(expected_orders) - set(orders))
        duplicate_orders = sorted(order for order, count in Counter(orders).items() if count > 1)
        raise ImportValidationError(
            "purchase_order must be continuous from 1 through the row count; "
            f"missing={missing_orders[:10]}, duplicates={duplicate_orders[:10]}"
        )

    player_ids: list[str] = []
    franchise_names: dict[str, str] = {}
    values_by_franchise: Counter[str] = Counter()
    players_by_franchise: Counter[str] = Counter()
    # The CSV has no timestamps. End the deterministic one-second sequence at the file mtime.
    source_finished_at = datetime.fromtimestamp(csv_path.stat().st_mtime, UTC)
    source_started_at = source_finished_at - timedelta(seconds=max(len(parsed_rows) - 1, 0))
    picks: list[DraftPick] = []
    for order, row, row_number in parsed_rows:
        player_id = _required(row.get("player_id"), "player_id", row_number)
        franchise_id = _required(row.get("franchise_id"), "franchise_id", row_number)
        franchise_name = _required(row.get("franchise_name"), "franchise_name", row_number)
        existing_name = franchise_names.setdefault(franchise_id, franchise_name)
        if existing_name != franchise_name:
            raise ImportValidationError(
                f"Row {row_number}: franchise {franchise_id} has inconsistent names"
            )
        status_raw = _required(row.get("status"), "status", row_number).upper()
        status = STATUS_ALIASES.get(status_raw)
        if status not in VALID_STATUSES:
            raise ImportValidationError(f"Row {row_number}: unsupported roster status {status_raw}")
        auction_value = _money(row.get("auction_value"), row_number)
        player_ids.append(player_id)
        players_by_franchise[franchise_id] += 1
        values_by_franchise[franchise_id] += Decimal(auction_value)
        picks.append(
            DraftPick(
                league_id=selected_league,
                season=selected_season,
                round=(order - 1) // selected_team_count + 1,
                pick=(order - 1) % selected_team_count + 1,
                overall_pick=order,
                franchise_id=franchise_id,
                franchise_name=franchise_name,
                player_id=player_id,
                player_name=_required(row.get("player_name"), "player_name", row_number),
                position=_required(row.get("position"), "position", row_number).upper(),
                nfl_team=(row.get("nfl_team") or "").strip().upper(),
                status=status,
                auction_value=auction_value,
                timestamp=int((source_started_at + timedelta(seconds=order - 1)).timestamp()),
            )
        )
    duplicate_players = sorted(player for player, count in Counter(player_ids).items() if count > 1)
    if duplicate_players:
        raise ImportValidationError(f"Duplicate player IDs: {duplicate_players[:10]}")

    warnings = [
        "Auction values are retained in audit files but intentionally omitted from "
        "MFL draftResults XML.",
        "The source CSV has no pick timestamps; deterministic timestamps were "
        "synthesized from file mtime.",
        "MFL draftResults import completely replaces any existing MFL draft results "
        "for this league.",
    ]
    roster_counts = set(players_by_franchise.values())
    if len(roster_counts) > 1:
        warnings.append(
            "Franchise roster counts are uneven: "
            + ", ".join(
                f"{franchise_id}={players_by_franchise[franchise_id]}"
                for franchise_id in sorted(players_by_franchise)
            )
        )
    return ImportPlan(
        source_file=str(csv_path.resolve()),
        source_sha256=hashlib.sha256(content).hexdigest(),
        league_id=selected_league,
        season=selected_season,
        unit=unit,
        teams_per_round=selected_team_count,
        picks=picks,
        warnings=warnings,
    )


def build_draft_xml(plan: ImportPlan) -> bytes:
    root = ET.Element("draftResults")
    unit = ET.SubElement(root, "draftUnit", {"unit": plan.unit})
    for item in plan.picks:
        ET.SubElement(
            unit,
            "draftPick",
            {
                "round": str(item.round),
                "pick": str(item.pick),
                "franchise": item.franchise_id,
                "player": item.player_id,
                "timestamp": str(item.timestamp),
                "status": item.status,
            },
        )
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def validate_draft_xml(payload: bytes, expected_picks: int) -> None:
    root = ET.fromstring(payload)
    if root.tag != "draftResults":
        raise ImportValidationError("MFL XML root must be draftResults")
    units = root.findall("draftUnit")
    if len(units) != 1 or not units[0].attrib.get("unit"):
        raise ImportValidationError("MFL XML requires one draftUnit with a unit attribute")
    picks = root.findall("./draftUnit/draftPick")
    if len(picks) != expected_picks:
        raise ImportValidationError(f"MFL XML has {len(picks)} picks; expected {expected_picks}")
    required = {"round", "pick", "franchise", "player", "timestamp", "status"}
    for index, pick in enumerate(picks, 1):
        missing = required - set(pick.attrib)
        if missing:
            raise ImportValidationError(f"XML pick {index} is missing: {sorted(missing)}")
        if pick.attrib["status"] not in VALID_STATUSES:
            raise ImportValidationError(f"XML pick {index} has invalid status")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def write_artifacts(plan: ImportPlan, output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"mfl_draft_results_{plan.league_id}_{plan.season}"
    xml = build_draft_xml(plan)
    validate_draft_xml(xml, len(plan.picks))
    xml_path = output_dir / f"{prefix}.xml"
    audit_path = output_dir / f"{prefix}.audit.json"
    normalized_path = output_dir / f"{prefix}.normalized.csv"
    checksum_path = output_dir / f"{prefix}.sha256"
    _atomic_write(xml_path, xml)

    franchise_summary: list[dict[str, Any]] = []
    for franchise_id in sorted({pick.franchise_id for pick in plan.picks}):
        team_picks = [pick for pick in plan.picks if pick.franchise_id == franchise_id]
        franchise_summary.append(
            {
                "franchise_id": franchise_id,
                "franchise_name": team_picks[0].franchise_name,
                "players": len(team_picks),
                "auction_spend_retained_for_audit_only": format(
                    sum((Decimal(pick.auction_value) for pick in team_picks), Decimal("0")),
                    "f",
                ),
                "position_counts": dict(
                    sorted(Counter(pick.position for pick in team_picks).items())
                ),
            }
        )
    audit = {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "preview_only_until_explicit_send",
        "source_file": plan.source_file,
        "source_sha256": plan.source_sha256,
        "league_id": plan.league_id,
        "season": plan.season,
        "mfl_import_type": "draftResults",
        "draft_unit": plan.unit,
        "teams_per_round": plan.teams_per_round,
        "pick_count": len(plan.picks),
        "round_count": max((pick.round for pick in plan.picks), default=0),
        "franchise_count": len(franchise_summary),
        "franchises": franchise_summary,
        "warnings": plan.warnings,
        "picks": [asdict(pick) for pick in plan.picks],
    }
    _atomic_write(audit_path, json.dumps(audit, indent=2).encode("utf-8"))

    fieldnames = list(DraftPick.__dataclass_fields__)
    import io

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(asdict(pick) for pick in plan.picks)
    _atomic_write(normalized_path, output.getvalue().encode("utf-8"))
    checksums = []
    for path in (xml_path, audit_path, normalized_path):
        checksums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
    _atomic_write(checksum_path, ("\n".join(checksums) + "\n").encode("utf-8"))
    return {
        "xml": xml_path,
        "audit": audit_path,
        "normalized_csv": normalized_path,
        "checksums": checksum_path,
    }


def _response_error(payload: bytes) -> str | None:
    text = payload.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        if text.startswith("{"):
            data = json.loads(text)
            error = data.get("error") if isinstance(data, dict) else None
            if isinstance(error, dict):
                return str(error.get("$t") or error.get("message") or error)
            if error:
                return str(error)
        if text.startswith("<"):
            root = ET.fromstring(payload)
            error = root if root.tag == "error" else root.find(".//error")
            if error is not None:
                return (error.text or "MFL returned an error").strip()
    except (ValueError, ET.ParseError):
        return None
    return None


def _response_is_html(payload: bytes) -> bool:
    text = payload.lstrip().lower()
    return text.startswith(b"<!doctype html") or text.startswith(b"<html")


def _league_host_from_export(payload: bytes) -> str | None:
    """Read MFL's assigned league host from a league-specific XML export."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    for element in root.iter():
        for value in element.attrib.values():
            if not value.startswith(("https://", "http://")):
                continue
            host = (urlparse(value).hostname or "").lower()
            if host.endswith(".myfantasyleague.com") and host != "api.myfantasyleague.com":
                return host
    return None


class MFLSession:
    def __init__(self, season: int, timeout: int = 30) -> None:
        self.season = season
        self.timeout = timeout
        self.cookie_jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookie_jar))
        self.cookie: str | None = None

    def _open(self, request: Request) -> bytes:
        if self.cookie:
            request.add_header("Cookie", f"MFL_USER_ID={self.cookie}")
        request.add_header("User-Agent", "DraftDeskOfflineDraftImporter/1.0")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = response.read()
        except HTTPError as exc:
            payload = exc.read()
            detail = _response_error(payload) or payload.decode("utf-8", errors="replace")[:500]
            raise MFLImportError(f"MFL returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise MFLImportError(f"Could not reach MFL: {exc.reason}") from exc
        error = _response_error(payload)
        if error:
            raise MFLImportError(error)
        return payload

    def login(self, username: str, password: str) -> None:
        body = urlencode({"USERNAME": username, "PASSWORD": password, "XML": "1"}).encode()
        request = Request(
            f"https://api.myfantasyleague.com/{self.season}/login",
            data=body,
            method="POST",
        )
        payload = self._open(request)
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise MFLImportError("MFL returned an unreadable login response") from exc
        status = root if root.tag == "status" else root.find(".//status")
        cookie = status.attrib.get("MFL_USER_ID") if status is not None else None
        if status is not None and not cookie and status.attrib.get("cookie_name") == "MFL_USER_ID":
            cookie = status.attrib.get("cookie_value")
        if not cookie:
            cookie = next(
                (item.value for item in self.cookie_jar if item.name == "MFL_USER_ID"), None
            )
        if not cookie:
            raise MFLImportError("MFL login succeeded without returning an MFL_USER_ID cookie")
        self.cookie = cookie

    def export_raw(self, export_type: str, league_id: str) -> bytes:
        query = urlencode({"TYPE": export_type, "L": league_id})
        request = Request(
            f"https://api.myfantasyleague.com/{self.season}/export?{query}",
            method="GET",
        )
        # Export errors are useful backup evidence, so return their body instead of rejecting it.
        if self.cookie:
            request.add_header("Cookie", f"MFL_USER_ID={self.cookie}")
        request.add_header("User-Agent", "DraftDeskOfflineDraftImporter/1.0")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.read()
        except HTTPError as exc:
            return exc.read()

    def import_draft_results(
        self,
        league_id: str,
        payload_xml: bytes,
        *,
        league_host: str,
    ) -> bytes:
        body = urlencode(
            {
                "TYPE": "draftResults",
                "L": league_id,
                "DATA": payload_xml.decode("utf-8"),
            }
        ).encode()
        request = Request(
            f"https://{league_host}/{self.season}/import",
            data=body,
            method="POST",
        )
        return self._open(request)


def _xml_pick_count(payload: bytes) -> int | None:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    if root.tag == "error" or root.find(".//error") is not None:
        return None
    return len(root.findall(".//draftPick"))


def send_plan(
    plan: ImportPlan,
    artifacts: dict[str, Path],
    output_dir: Path,
    *,
    username: str,
    password: str,
    timeout: int,
    assume_yes: bool,
) -> dict[str, Any]:
    if not assume_yes:
        expected = f"IMPORT DRAFT RESULTS {plan.league_id}"
        print("\nDANGER: MFL will delete and replace all existing draft results for this league.")
        confirmation = input(f"Type {expected} to continue: ").strip()
        if confirmation != expected:
            raise MFLImportError("Confirmation did not match; nothing was sent")
    session = MFLSession(plan.season, timeout=timeout)
    session.login(username, password)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = output_dir / f"mfl_before_draft_results_{plan.league_id}_{stamp}.xml"
    current = session.export_raw("draftResults", plan.league_id)
    _atomic_write(backup_path, current)
    current_error = _response_error(current)
    if current_error and "not been set up" not in current_error.casefold():
        raise MFLImportError(
            f"Pre-import draftResults check failed: {current_error}. Nothing was sent."
        )
    league_host = _league_host_from_export(current)
    if not league_host:
        routing = session.export_raw("league", plan.league_id)
        routing_path = output_dir / f"mfl_league_routing_{plan.league_id}_{stamp}.xml"
        _atomic_write(routing_path, routing)
        league_host = _league_host_from_export(routing)
    if not league_host:
        raise MFLImportError(
            "Could not determine this league's assigned MFL server. Nothing was sent."
        )

    xml = artifacts["xml"].read_bytes()
    response = session.import_draft_results(
        plan.league_id,
        xml,
        league_host=league_host,
    )
    response_path = output_dir / f"mfl_import_response_{plan.league_id}_{stamp}.txt"
    _atomic_write(response_path, response)
    if _response_is_html(response):
        raise MFLImportError(
            "MFL returned an HTML page instead of an import response; the import was not "
            f"accepted. Response saved at {response_path}"
        )
    verify = session.export_raw("draftResults", plan.league_id)
    verify_path = output_dir / f"mfl_after_draft_results_{plan.league_id}_{stamp}.xml"
    _atomic_write(verify_path, verify)
    observed_picks = _xml_pick_count(verify)
    verification = (
        "matched" if observed_picks == len(plan.picks) else "pending_mfl_cache_or_mismatch"
    )
    receipt = {
        "sent_at": datetime.now(UTC).isoformat(),
        "league_id": plan.league_id,
        "season": plan.season,
        "league_host": league_host,
        "import_type": "draftResults",
        "expected_picks": len(plan.picks),
        "observed_export_picks": observed_picks,
        "verification": verification,
        "note": "MFL documents draftResults exports as potentially delayed by up to 15 minutes.",
        "source_sha256": plan.source_sha256,
        "xml_sha256": hashlib.sha256(xml).hexdigest(),
        "files": {
            "before_backup": str(backup_path),
            "response": str(response_path),
            "after_export": str(verify_path),
        },
    }
    receipt_path = output_dir / f"mfl_import_receipt_{plan.league_id}_{stamp}.json"
    _atomic_write(receipt_path, json.dumps(receipt, indent=2).encode("utf-8"))
    return {**receipt, "receipt": str(receipt_path)}


def _print_summary(plan: ImportPlan, artifacts: dict[str, Path]) -> None:
    counts = Counter(pick.franchise_id for pick in plan.picks)
    spent: Counter[str] = Counter()
    names: dict[str, str] = {}
    for pick in plan.picks:
        spent[pick.franchise_id] += Decimal(pick.auction_value)
        names[pick.franchise_id] = pick.franchise_name
    print(f"League: {plan.league_id} · Season: {plan.season}")
    print(
        f"Picks: {len(plan.picks)} · Rounds: {max(pick.round for pick in plan.picks)} "
        f"· Franchises: {len(counts)} · Picks per round: {plan.teams_per_round}"
    )
    print("\nFranchise validation:")
    for franchise_id in sorted(counts):
        print(
            f"  {franchise_id}  {names[franchise_id]:<24} "
            f"players={counts[franchise_id]:>2}  audit spend=${spent[franchise_id]:.2f}"
        )
    print("\nWarnings:")
    for warning in plan.warnings:
        print(f"  - {warning}")
    print("\nGenerated files:")
    for label, path in artifacts.items():
        print(f"  {label}: {path}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely convert a DraftDesk auction CSV to MFL draftResults XML."
    )
    parser.add_argument("csv_file", type=Path, help="DraftDesk auction-results CSV")
    parser.add_argument("--output-dir", type=Path, help="Artifact directory")
    parser.add_argument("--league-id", help="Must match the CSV league ID")
    parser.add_argument("--season", type=int, help="Must match the CSV season")
    parser.add_argument("--teams-per-round", type=int, help="Defaults to unique CSV franchises")
    parser.add_argument("--unit", default="LEAGUE", help="MFL draft unit (default: LEAGUE)")
    parser.add_argument("--send", action="store_true", help="Actually send to MFL")
    parser.add_argument("--username", help="MFL commissioner username; or set MFL_USERNAME")
    parser.add_argument("--timeout", type=int, default=30, help="Network timeout in seconds")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip typed confirmation; intended only for controlled automation",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    csv_path = args.csv_file.resolve()
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else csv_path.parent / f"mfl_draft_import_{csv_path.stem}"
    )
    try:
        plan = load_plan(
            csv_path,
            league_id=args.league_id,
            season=args.season,
            teams_per_round=args.teams_per_round,
            unit=args.unit,
        )
        artifacts = write_artifacts(plan, output_dir)
        _print_summary(plan, artifacts)
        if not args.send:
            print("\nPREVIEW ONLY: nothing was sent to MFL. Add --send after reviewing the files.")
            return 0
        username = (
            args.username
            or os.environ.get("MFL_USERNAME")
            or input("MFL commissioner username: ").strip()
        )
        password = os.environ.get("MFL_PASSWORD") or input(
            "MFL commissioner password (visible while typing; not stored): "
        )
        if not username or not password:
            raise MFLImportError("Commissioner username and password are required")
        receipt = send_plan(
            plan,
            artifacts,
            output_dir,
            username=username,
            password=password,
            timeout=args.timeout,
            assume_yes=args.yes,
        )
        if receipt["verification"] == "matched":
            print("\nMFL import verified: all draft results are present.")
        else:
            print("\nMFL returned no import error; export verification is still pending.")
        print(f"Verification: {receipt['verification']}")
        print(f"Receipt: {receipt['receipt']}")
        return 0
    except (OSError, ImportValidationError, MFLImportError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
