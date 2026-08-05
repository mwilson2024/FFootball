import csv
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuctionAuditEvent, AuctionPurchase, Franchise, League, Player

CSV_HEADERS = [
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


def _amount(value: Decimal) -> str:
    return format(value, "f")


def export_rows(db: Session, league_id: str) -> tuple[League, list[dict[str, str]]]:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise ValueError("League does not exist")
    purchases = list(
        db.scalars(
            select(AuctionPurchase)
            .where(AuctionPurchase.league_id == league_id, AuctionPurchase.active.is_(True))
            .order_by(AuctionPurchase.purchase_order)
        )
    )
    player_ids = [purchase.player_id for purchase in purchases]
    if len(player_ids) != len(set(player_ids)):
        raise ValueError("Duplicate player IDs cannot be exported")
    rows: list[dict[str, str]] = []
    for purchase in purchases:
        player = db.get(Player, purchase.player_id)
        franchise = db.scalar(
            select(Franchise).where(
                Franchise.league_id == league_id, Franchise.id == purchase.franchise_id
            )
        )
        if player is None or franchise is None:
            raise ValueError("Purchase references missing player or franchise")
        rows.append(
            {
                "league_id": league.id,
                "season": str(league.season),
                "franchise_id": franchise.id,
                "franchise_name": franchise.name,
                "player_id": player.id,
                "player_name": player.name,
                "position": player.position,
                "nfl_team": player.nfl_team or "",
                "auction_value": _amount(Decimal(purchase.amount)),
                "status": purchase.status,
                "purchase_order": str(purchase.purchase_order),
            }
        )
    return league, rows


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _manifest(path: Path, league: League, row_count: int) -> Path:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    content = json.dumps(
        {
            "file": path.name,
            "sha256": digest,
            "league_id": league.id,
            "season": league.season,
            "rows": row_count,
            "created_at": datetime.now(UTC).isoformat(),
        },
        indent=2,
    ).encode()
    _atomic_write(manifest_path, content)
    _atomic_write(path.with_suffix(path.suffix + ".sha256"), f"{digest}  {path.name}\n".encode())
    return manifest_path


def export_csv(db: Session, league_id: str, directory: Path) -> Path:
    league, rows = export_rows(db, league_id)
    path = directory / f"mfl_auction_results_{league.id}_{league.season}.csv"
    import io

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_HEADERS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    _atomic_write(path, output.getvalue().encode("utf-8"))
    _manifest(path, league, len(rows))
    db.add(
        AuctionAuditEvent(league_id=league.id, action="export_csv", after_json={"file": path.name})
    )
    db.commit()
    return path


def build_xml(db: Session, league_id: str) -> tuple[League, bytes, int]:
    league, rows = export_rows(db, league_id)
    unit_name = str(league.settings_json.get("auction_unit", "LEAGUE"))
    root = ET.Element("auctionResults")
    unit = ET.SubElement(root, "auctionUnit", {"unit": unit_name})
    now = str(int(datetime.now(UTC).timestamp()))
    for row in rows:
        attributes = {
            "player": row["player_id"],
            "franchise": row["franchise_id"],
            "winningBid": row["auction_value"],
            "timeStarted": now,
            "lastBidTime": now,
        }
        if row["status"] != "ROSTER":
            attributes["status"] = row["status"]
        ET.SubElement(unit, "auction", attributes)
    ET.indent(root, space="  ")
    payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    validate_xml(payload)
    return league, payload, len(rows)


def validate_xml(payload: bytes) -> None:
    root = ET.fromstring(payload)
    if root.tag != "auctionResults":
        raise ValueError("Invalid MFL XML root")
    units = root.findall("auctionUnit")
    if not units or any("unit" not in unit.attrib for unit in units):
        raise ValueError("MFL XML requires an auctionUnit")
    allowed_status = {"ROSTER", "TAXI_SQUAD", "INJURED_RESERVE"}
    for auction in root.findall("./auctionUnit/auction"):
        required = {"player", "franchise", "winningBid", "timeStarted", "lastBidTime"}
        if not required.issubset(auction.attrib):
            raise ValueError("MFL auction element is missing required attributes")
        Decimal(auction.attrib["winningBid"])
        if auction.attrib.get("status", "ROSTER") not in allowed_status:
            raise ValueError("Invalid MFL roster status")


def export_xml(db: Session, league_id: str, directory: Path) -> Path:
    league, payload, count = build_xml(db, league_id)
    path = directory / f"mfl_auction_results_{league.id}_{league.season}.xml"
    _atomic_write(path, payload)
    _manifest(path, league, count)
    db.add(
        AuctionAuditEvent(league_id=league.id, action="export_xml", after_json={"file": path.name})
    )
    db.commit()
    return path
