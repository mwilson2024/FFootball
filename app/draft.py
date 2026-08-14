import csv
import io
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog import draftable_consensus
from app.consensus import availability_maps
from app.models import (
    AuctionPurchase,
    DraftAuditEvent,
    DraftPick,
    DraftSession,
    Franchise,
    League,
    MFLSnapshot,
    Player,
    RosterAssignment,
)
from app.schemas import DraftPickCreate, DraftPickUpdate


class DraftValidationError(ValueError):
    pass


def franchise_position_needs(db: Session, league_id: str, franchise_id: str) -> dict[str, int]:
    """Return open primary starter slots using synced and locally recorded players."""
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    owned_player_ids = set(
        db.scalars(
            select(RosterAssignment.player_id).where(
                RosterAssignment.league_id == league_id,
                RosterAssignment.franchise_id == franchise_id,
            )
        )
    )
    owned_player_ids.update(
        db.scalars(
            select(DraftPick.player_id).where(
                DraftPick.league_id == league_id,
                DraftPick.franchise_id == franchise_id,
            )
        )
    )
    owned_player_ids.update(
        db.scalars(
            select(AuctionPurchase.player_id).where(
                AuctionPurchase.league_id == league_id,
                AuctionPurchase.franchise_id == franchise_id,
                AuctionPurchase.active.is_(True),
            )
        )
    )
    position_counts: dict[str, int] = {}
    if owned_player_ids:
        for position in db.scalars(select(Player.position).where(Player.id.in_(owned_player_ids))):
            normalized = str(position or "").upper()
            position_counts[normalized] = position_counts.get(normalized, 0) + 1
    return {
        str(position).upper(): max(
            0, int(required or 0) - position_counts.get(str(position).upper(), 0)
        )
        for position, required in league.lineup_json.items()
        if str(position).upper() not in {"FLEX", "SUPERFLEX"}
    }


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _number(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _draft_order(
    db: Session, league_id: str, local_picks: list[DraftPick]
) -> tuple[list[dict[str, Any]], str | None]:
    snapshot = db.scalar(
        select(MFLSnapshot)
        .where(
            MFLSnapshot.league_id == league_id,
            MFLSnapshot.export_type == "draftResults",
        )
        .order_by(MFLSnapshot.fetched_at.desc())
    )
    if snapshot is None or not isinstance(snapshot.payload_json, dict):
        return _local_recorded_order(db, league_id, local_picks), None
    root = snapshot.payload_json.get("draftResults", snapshot.payload_json)
    if not isinstance(root, dict):
        return _local_recorded_order(db, league_id, local_picks), snapshot.fetched_at.isoformat()
    units = [item for item in _as_list(root.get("draftUnit")) if isinstance(item, dict)]
    unit = next((item for item in units if item.get("draftPick") is not None), None)
    if unit is None:
        return _local_recorded_order(db, league_id, local_picks), snapshot.fetched_at.isoformat()

    raw_picks = [item for item in _as_list(unit.get("draftPick")) if isinstance(item, dict)]
    if not raw_picks:
        return _local_recorded_order(db, league_id, local_picks), snapshot.fetched_at.isoformat()
    indexed_picks = list(enumerate(raw_picks, start=1))
    has_explicit_overall = all(_number(item.get("overallPick")) for item in raw_picks)
    if has_explicit_overall:
        indexed_picks.sort(key=lambda item: _number(item[1].get("overallPick")) or item[0])
        order_source = "MFL draft order"
    else:
        order_source = "MFL listed order"

    local_by_overall = {
        item.overall_pick: item for item in local_picks if item.overall_pick is not None
    }
    franchise_names = {
        item.id: item.name
        for item in db.scalars(select(Franchise).where(Franchise.league_id == league_id))
    }
    slots: list[dict[str, Any]] = []
    for index, (_, raw) in enumerate(indexed_picks, start=1):
        overall = _number(raw.get("overallPick")) or index
        local = local_by_overall.get(overall)
        franchise_id = str(raw.get("franchise") or (local.franchise_id if local else ""))
        remote_player = raw.get("player")
        remote_player_id = str(remote_player) if remote_player not in (None, "") else None
        player_id = local.player_id if local else remote_player_id
        player = db.get(Player, player_id) if player_id else None
        slots.append(
            {
                "overall_pick": overall,
                "round": _number(raw.get("round")) or (local.round if local else None),
                "pick": _number(raw.get("pick")) or (local.pick if local else None),
                "franchise_id": franchise_id or None,
                "franchise_name": franchise_names.get(franchise_id, franchise_id or "Unknown"),
                "player_id": player_id,
                "player_name": player.name if player else None,
                "completed": bool(player_id),
                "order_source": order_source,
            }
        )
    return slots, snapshot.fetched_at.isoformat()


def _local_recorded_order(
    db: Session, league_id: str, local_picks: list[DraftPick]
) -> list[dict[str, Any]]:
    franchise_names = {
        item.id: item.name
        for item in db.scalars(select(Franchise).where(Franchise.league_id == league_id))
    }
    ordered = sorted(
        local_picks,
        key=lambda item: (item.overall_pick is None, item.overall_pick or 0, item.selected_at),
    )
    slots: list[dict[str, Any]] = []
    for index, local in enumerate(ordered, start=1):
        player = db.get(Player, local.player_id)
        franchise_id = local.franchise_id or ""
        slots.append(
            {
                "overall_pick": local.overall_pick or index,
                "round": local.round,
                "pick": local.pick,
                "franchise_id": franchise_id or None,
                "franchise_name": franchise_names.get(franchise_id, franchise_id or "Unknown"),
                "player_id": local.player_id,
                "player_name": player.name if player else local.player_id,
                "completed": True,
                "order_source": "Locally recorded picks",
            }
        )
    return slots


def get_or_create_session(db: Session, league: League) -> DraftSession:
    session = db.scalar(
        select(DraftSession).where(
            DraftSession.league_id == league.id, DraftSession.season == league.season
        )
    )
    if session is None:
        session = DraftSession(league_id=league.id, season=league.season)
        db.add(session)
        db.commit()
        db.refresh(session)
    return session


def set_draft_live(db: Session, league_id: str, is_live: bool) -> dict[str, Any]:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    session = get_or_create_session(db, league)
    session.status = "live" if is_live else "paused"
    db.commit()
    db.refresh(session)
    return {
        "is_live": session.status == "live",
        "status": session.status,
    }


def pick_json(db: Session, pick: DraftPick) -> dict[str, Any]:
    player = db.get(Player, pick.player_id)
    franchise = (
        db.scalar(
            select(Franchise).where(
                Franchise.league_id == pick.league_id, Franchise.id == pick.franchise_id
            )
        )
        if pick.franchise_id
        else None
    )
    return {
        "id": pick.id,
        "session_id": pick.session_id,
        "league_id": pick.league_id,
        "player_id": pick.player_id,
        "player_name": player.name if player else pick.player_id,
        "position": player.position if player else None,
        "nfl_team": player.nfl_team if player else None,
        "franchise_id": pick.franchise_id,
        "franchise_name": franchise.name if franchise else None,
        "round": pick.round,
        "pick": pick.pick,
        "overall_pick": pick.overall_pick,
        "source": pick.source,
        "selected_at": pick.selected_at.isoformat(),
        "version": pick.version,
    }


def _snapshot(pick: DraftPick) -> dict[str, Any]:
    return {
        "id": pick.id,
        "session_id": pick.session_id,
        "league_id": pick.league_id,
        "player_id": pick.player_id,
        "franchise_id": pick.franchise_id,
        "round": pick.round,
        "pick": pick.pick,
        "overall_pick": pick.overall_pick,
        "source": pick.source,
        "selected_at": pick.selected_at.isoformat(),
        "version": pick.version,
    }


def _restore(db: Session, data: dict[str, Any]) -> DraftPick:
    pick = DraftPick(
        id=str(data["id"]),
        session_id=str(data["session_id"]),
        league_id=str(data["league_id"]),
        player_id=str(data["player_id"]),
        franchise_id=str(data["franchise_id"]) if data.get("franchise_id") else None,
        round=int(data["round"]) if data.get("round") else None,
        pick=int(data["pick"]) if data.get("pick") else None,
        overall_pick=int(data["overall_pick"]) if data.get("overall_pick") else None,
        source=str(data.get("source", "local")),
        selected_at=datetime.fromisoformat(str(data["selected_at"])),
        version=int(data.get("version", 1)),
    )
    db.add(pick)
    return pick


def add_pick(db: Session, payload: DraftPickCreate, *, source: str = "local") -> DraftPick:
    league = db.scalar(select(League).where(League.id == payload.league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    if db.get(Player, payload.player_id) is None:
        raise DraftValidationError("Player does not exist")
    if payload.franchise_id and not db.scalar(
        select(Franchise).where(
            Franchise.league_id == payload.league_id,
            Franchise.id == payload.franchise_id,
        )
    ):
        raise DraftValidationError("Franchise does not exist")
    availability = availability_maps(db, payload.league_id)
    if payload.player_id in availability["unavailable"]:
        raise DraftValidationError("Player is already rostered, kept, purchased, or drafted")
    session = get_or_create_session(db, league)
    overall = payload.overall_pick
    if overall is None:
        current = db.scalar(
            select(func.coalesce(func.max(DraftPick.overall_pick), 0)).where(
                DraftPick.session_id == session.id
            )
        )
        overall = int(current or 0) + 1
    pick = DraftPick(
        session_id=session.id,
        league_id=payload.league_id,
        player_id=payload.player_id,
        franchise_id=payload.franchise_id,
        round=payload.round,
        pick=payload.pick,
        overall_pick=overall,
        source=source,
    )
    try:
        db.add(pick)
        db.flush()
        db.add(
            DraftAuditEvent(
                league_id=payload.league_id,
                action="create",
                entity_id=pick.id,
                after_json=_snapshot(pick),
            )
        )
        if session.status not in {"live", "paused"}:
            session.status = "in_progress"
        current_picks = list(
            db.scalars(
                select(DraftPick)
                .where(DraftPick.session_id == session.id)
                .order_by(DraftPick.overall_pick, DraftPick.selected_at)
            )
        )
        order, _ = _draft_order(db, payload.league_id, current_picks)
        next_slot = next((slot for slot in order if not slot["completed"]), None)
        session.current_round = next_slot["round"] if next_slot else None
        session.current_pick = next_slot["pick"] if next_slot else None
        db.commit()
        db.refresh(pick)
        return pick
    except IntegrityError as exc:
        db.rollback()
        raise DraftValidationError("Player or pick slot was already recorded") from exc


def update_pick(db: Session, pick_id: str, payload: DraftPickUpdate) -> DraftPick:
    pick = db.scalar(select(DraftPick).where(DraftPick.id == pick_id).with_for_update())
    if pick is None:
        raise DraftValidationError("Draft pick does not exist")
    if pick.version != payload.version:
        raise DraftValidationError("Draft pick changed in another session; refresh first")
    if payload.franchise_id and not db.scalar(
        select(Franchise).where(
            Franchise.league_id == pick.league_id,
            Franchise.id == payload.franchise_id,
        )
    ):
        raise DraftValidationError("Franchise does not exist")
    before = _snapshot(pick)
    pick.franchise_id = payload.franchise_id
    pick.round = payload.round
    pick.pick = payload.pick
    pick.overall_pick = payload.overall_pick
    pick.version += 1
    db.add(
        DraftAuditEvent(
            league_id=pick.league_id,
            action="update",
            entity_id=pick.id,
            before_json=before,
            after_json=_snapshot(pick),
        )
    )
    db.commit()
    db.refresh(pick)
    return pick


def remove_pick(db: Session, pick_id: str) -> None:
    pick = db.get(DraftPick, pick_id)
    if pick is None:
        raise DraftValidationError("Draft pick does not exist")
    before = _snapshot(pick)
    league_id = pick.league_id
    db.delete(pick)
    db.add(
        DraftAuditEvent(
            league_id=league_id,
            action="delete",
            entity_id=pick_id,
            before_json=before,
        )
    )
    db.commit()


def undo_draft(db: Session, league_id: str) -> None:
    event = db.scalar(
        select(DraftAuditEvent)
        .where(DraftAuditEvent.league_id == league_id, DraftAuditEvent.undone.is_(False))
        .order_by(DraftAuditEvent.id.desc())
    )
    if event is None:
        raise DraftValidationError("Nothing to undo")
    if event.action == "create" and event.entity_id:
        db.execute(delete(DraftPick).where(DraftPick.id == event.entity_id))
    elif event.action == "delete" and event.before_json:
        _restore(db, event.before_json)
    elif event.action == "update" and event.before_json and event.entity_id:
        current = db.get(DraftPick, event.entity_id)
        if current:
            previous = event.before_json
            current.franchise_id = previous.get("franchise_id")
            current.round = previous.get("round")
            current.pick = previous.get("pick")
            current.overall_pick = previous.get("overall_pick")
            current.version = int(previous.get("version", 1))
    event.undone = True
    db.commit()


def draft_state(db: Session, league_id: str) -> dict[str, Any]:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    session = get_or_create_session(db, league)
    picks = list(
        db.scalars(
            select(DraftPick)
            .where(DraftPick.session_id == session.id)
            .order_by(DraftPick.overall_pick, DraftPick.selected_at)
        )
    )
    board = draftable_consensus(db, league_id)
    queue = sorted(
        (
            row
            for row in board
            if row["preference"]["queue_order"] is not None or row["preference"]["target"]
        ),
        key=lambda row: (
            row["preference"]["queue_order"] is None,
            row["preference"]["queue_order"] or 99999,
            row["consensus_rank"],
        ),
    )
    tiers: dict[str, int] = {}
    for row in board:
        if row["available"] and row["tier"] is not None:
            key = f"{row['position']}:T{row['tier']}"
            tiers[key] = tiers.get(key, 0) + 1
    order, order_fetched_at = _draft_order(db, league_id, picks)
    current_drafter = next((slot for slot in order if not slot["completed"]), None)
    return {
        "session": {
            "id": session.id,
            "league_id": session.league_id,
            "season": session.season,
            "status": session.status,
            "current_round": current_drafter["round"] if current_drafter else None,
            "current_pick": current_drafter["pick"] if current_drafter else None,
            "source": session.source,
            "synced_at": session.synced_at.isoformat() if session.synced_at else None,
        },
        "live": {
            "is_live": session.status == "live",
            "status": session.status,
        },
        "picks": [pick_json(db, item) for item in picks],
        "draft_order": order,
        "current_drafter": current_drafter,
        "order_source": order[0]["order_source"] if order else "Unavailable",
        "order_fetched_at": order_fetched_at,
        "queue": [
            {
                "player_id": row["player_id"],
                "player_name": row["player_name"],
                "position": row["position"],
                "queue_order": row["preference"]["queue_order"],
                "target": row["preference"]["target"],
            }
            for row in queue
        ],
        "tier_counts": tiers,
    }


def recommendations(
    db: Session, league_id: str, franchise_id: str | None = None, limit: int = 12
) -> list[dict[str, Any]]:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    needs = franchise_position_needs(db, league_id, franchise_id) if franchise_id else {}
    rows = [row for row in draftable_consensus(db, league_id) if row["available"]]
    for row in rows:
        need = needs.get(str(row["position"]).upper(), 0)
        base = float(row["consensus_score"])
        row["recommendation_score"] = round(base + need * 0.12, 4)
        reasons = []
        if need:
            reasons.append(f"roster needs {need} more {row['position']} starter(s)")
        if row["tier"] is not None:
            reasons.append(f"Tier {row['tier']}")
        if row["adp"] and row["consensus_rank"] < float(row["adp"]):
            reasons.append("value ahead of market ADP")
        if row["preference"]["target"]:
            reasons.append("marked as a target")
        row["recommendation_reason"] = "; ".join(reasons) or "best remaining league value"
    rows.sort(
        key=lambda row: (
            -row["recommendation_score"],
            row["league_adjusted_rank"] or 99999,
            row["player_id"],
        )
    )
    return rows[:limit]


def _walk_draft_rows(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if ("player" in value or "player_id" in value) and (
            "franchise" in value or "franchise_id" in value
        ):
            rows.append(value)
        for child in value.values():
            rows.extend(_walk_draft_rows(child))
    elif isinstance(value, list):
        for child in value:
            rows.extend(_walk_draft_rows(child))
    return rows


def reconcile_preview(db: Session, league_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    local = {
        item.player_id: item
        for item in db.scalars(select(DraftPick).where(DraftPick.league_id == league_id))
    }
    remote_rows = _walk_draft_rows(payload)
    additions: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for index, row in enumerate(remote_rows, 1):
        player_id = str(row.get("player", row.get("player_id", "")))
        franchise_id = str(row.get("franchise", row.get("franchise_id", "")))
        if not player_id:
            continue
        existing = local.get(player_id)
        normalized = {
            "player_id": player_id,
            "franchise_id": franchise_id or None,
            "round": int(row["round"]) if row.get("round") else None,
            "pick": int(row["pick"]) if row.get("pick") else None,
            "overall_pick": int(row.get("overallPick", row.get("overall_pick", index))),
        }
        if existing is None:
            additions.append(normalized)
        elif existing.franchise_id != normalized["franchise_id"]:
            conflicts.append(
                {
                    "player_id": player_id,
                    "local_franchise_id": existing.franchise_id,
                    "mfl_franchise_id": normalized["franchise_id"],
                }
            )
    return {"additions": additions, "conflicts": conflicts, "remote_count": len(remote_rows)}


def apply_reconciliation(db: Session, league_id: str, preview: dict[str, Any]) -> int:
    if preview["conflicts"]:
        raise DraftValidationError("Resolve MFL conflicts before applying reconciliation")
    applied = 0
    for row in preview["additions"]:
        try:
            add_pick(
                db,
                DraftPickCreate(league_id=league_id, **row),
                source="mfl",
            )
            applied += 1
        except DraftValidationError:
            continue
    league = db.scalar(select(League).where(League.id == league_id))
    if league:
        session = get_or_create_session(db, league)
        session.synced_at = datetime.now(UTC)
        session.source = "mfl"
        db.commit()
    return applied


def export_draft_csv(db: Session, league_id: str, directory: Path) -> Path:
    league = db.scalar(select(League).where(League.id == league_id))
    if league is None:
        raise DraftValidationError("League does not exist")
    session = get_or_create_session(db, league)
    picks = list(
        db.scalars(
            select(DraftPick)
            .where(DraftPick.session_id == session.id)
            .order_by(DraftPick.overall_pick, DraftPick.selected_at)
        )
    )
    output = io.StringIO(newline="")
    headers = [
        "league_id",
        "season",
        "overall_pick",
        "round",
        "pick",
        "franchise_id",
        "player_id",
        "player_name",
        "position",
        "nfl_team",
        "source",
    ]
    writer = csv.DictWriter(output, fieldnames=headers, lineterminator="\n")
    writer.writeheader()
    for item in picks:
        row = pick_json(db, item)
        writer.writerow(
            {
                "league_id": league_id,
                "season": league.season,
                "overall_pick": item.overall_pick,
                "round": item.round,
                "pick": item.pick,
                "franchise_id": item.franchise_id or "",
                "player_id": item.player_id,
                "player_name": row["player_name"],
                "position": row["position"],
                "nfl_team": row["nfl_team"] or "",
                "source": item.source,
            }
        )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"mfl_draft_results_{league_id}_{league.season}.csv"
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(output.getvalue().encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path
