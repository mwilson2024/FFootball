from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AuctionAuditEvent,
    AuctionPurchase,
    Franchise,
    League,
    Player,
    RosterAssignment,
    RosterStatus,
)
from app.schemas import PurchaseCreate, PurchaseUpdate


class AuctionValidationError(ValueError):
    pass


def _purchase_dict(purchase: AuctionPurchase) -> dict[str, Any]:
    return {
        "id": purchase.id,
        "league_id": purchase.league_id,
        "franchise_id": purchase.franchise_id,
        "player_id": purchase.player_id,
        "amount": str(purchase.amount),
        "status": purchase.status,
        "purchase_order": purchase.purchase_order,
        "source": purchase.source,
        "version": purchase.version,
    }


def _precision_valid(
    amount: Decimal, minimum_bid: Decimal, configured_precision: object | None = None
) -> bool:
    if configured_precision not in (None, ""):
        amount_exponent = amount.normalize().as_tuple().exponent
        if not isinstance(amount_exponent, int):
            return False
        try:
            return max(0, -amount_exponent) <= int(str(configured_precision))
        except ValueError:
            return False
    minimum_exponent = minimum_bid.normalize().as_tuple().exponent
    amount_exponent = amount.normalize().as_tuple().exponent
    if not isinstance(minimum_exponent, int) or not isinstance(amount_exponent, int):
        return False
    allowed = max(0, -minimum_exponent)
    precision = max(0, -amount_exponent)
    return precision <= allowed


def franchise_budget(db: Session, league: League, franchise: Franchise) -> dict[str, Any]:
    spent = db.scalar(
        select(func.coalesce(func.sum(AuctionPurchase.amount), 0)).where(
            AuctionPurchase.league_id == league.id,
            AuctionPurchase.franchise_id == franchise.id,
            AuctionPurchase.active.is_(True),
        )
    )
    spent = Decimal(str(spent or 0))
    local_count = (
        db.scalar(
            select(func.count())
            .select_from(AuctionPurchase)
            .where(
                AuctionPurchase.league_id == league.id,
                AuctionPurchase.franchise_id == franchise.id,
                AuctionPurchase.active.is_(True),
            )
        )
        or 0
    )
    synced_count = (
        db.scalar(
            select(func.count())
            .select_from(RosterAssignment)
            .where(
                RosterAssignment.league_id == league.id,
                RosterAssignment.franchise_id == franchise.id,
            )
        )
        or 0
    )
    used = int(local_count) + int(synced_count)
    remaining = Decimal(franchise.starting_budget) - spent
    slots_remaining = max(0, franchise.roster_slots - used)
    reserve = max(0, slots_remaining - 1) * Decimal(league.minimum_bid)
    maximum_bid = max(Decimal("0"), remaining - reserve)
    return {
        "franchise_id": franchise.id,
        "name": franchise.name,
        "starting_budget": franchise.starting_budget,
        "spent": spent,
        "remaining": remaining,
        "slots_used": used,
        "roster_slots": franchise.roster_slots,
        "slots_remaining": slots_remaining,
        "maximum_bid": maximum_bid,
    }


def _validate(
    db: Session,
    payload: PurchaseCreate,
    *,
    ignore_purchase_id: str | None = None,
) -> tuple[League, Franchise]:
    league = db.scalar(select(League).where(League.id == payload.league_id))
    if league is None:
        raise AuctionValidationError("League does not exist")
    franchise = db.scalar(
        select(Franchise).where(
            Franchise.league_id == payload.league_id, Franchise.id == payload.franchise_id
        )
    )
    if franchise is None:
        raise AuctionValidationError("Franchise does not exist")
    if db.get(Player, payload.player_id) is None:
        raise AuctionValidationError("Player does not exist")
    rostered = db.scalar(
        select(RosterAssignment).where(
            RosterAssignment.league_id == payload.league_id,
            RosterAssignment.player_id == payload.player_id,
        )
    )
    if rostered:
        raise AuctionValidationError("Player is already rostered")
    sold_query = select(AuctionPurchase).where(
        AuctionPurchase.league_id == payload.league_id,
        AuctionPurchase.player_id == payload.player_id,
        AuctionPurchase.active.is_(True),
    )
    if ignore_purchase_id:
        sold_query = sold_query.where(AuctionPurchase.id != ignore_purchase_id)
    if db.scalar(sold_query):
        raise AuctionValidationError("Player is already sold")
    minimum_bid = Decimal(league.minimum_bid)
    if payload.amount < minimum_bid:
        raise AuctionValidationError(f"Bid must be at least {minimum_bid}")
    if not _precision_valid(payload.amount, minimum_bid, league.settings_json.get("precision")):
        raise AuctionValidationError("Bid has more precision than this league permits")
    budget = franchise_budget(db, league, franchise)
    if ignore_purchase_id:
        current = db.get(AuctionPurchase, ignore_purchase_id)
        if current and current.franchise_id == franchise.id:
            budget["maximum_bid"] = Decimal(budget["maximum_bid"]) + Decimal(current.amount)
    if budget["slots_remaining"] <= 0 and not ignore_purchase_id:
        raise AuctionValidationError("Franchise has no open roster slot")
    if payload.amount > Decimal(budget["maximum_bid"]):
        raise AuctionValidationError(f"Bid exceeds maximum legal bid of {budget['maximum_bid']}")
    synced_at = league.synced_at
    if synced_at and synced_at.tzinfo is None:
        synced_at = synced_at.replace(tzinfo=UTC)
    if synced_at and datetime.now(UTC) - synced_at > __import__("datetime").timedelta(hours=24):
        raise AuctionValidationError(
            "League data is more than 24 hours stale; synchronize before selling"
        )
    return league, franchise


def add_purchase(db: Session, payload: PurchaseCreate) -> AuctionPurchase:
    _validate(db, payload)
    order = db.scalar(
        select(func.coalesce(func.max(AuctionPurchase.purchase_order), 0)).where(
            AuctionPurchase.league_id == payload.league_id
        )
    )
    purchase = AuctionPurchase(
        league_id=payload.league_id,
        franchise_id=payload.franchise_id,
        player_id=payload.player_id,
        amount=payload.amount,
        status=RosterStatus.ROSTER.value,
        purchase_order=int(order or 0) + 1,
    )
    try:
        db.add(purchase)
        db.flush()
        db.add(
            AuctionAuditEvent(
                league_id=payload.league_id,
                action="create",
                entity_id=purchase.id,
                after_json=_purchase_dict(purchase),
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AuctionValidationError("Player was sold by another request") from exc
    db.refresh(purchase)
    return purchase


def update_purchase(db: Session, purchase_id: str, update: PurchaseUpdate) -> AuctionPurchase:
    purchase = db.scalar(
        select(AuctionPurchase).where(AuctionPurchase.id == purchase_id).with_for_update()
    )
    if not purchase or not purchase.active:
        raise AuctionValidationError("Purchase does not exist")
    if purchase.version != update.version:
        raise AuctionValidationError("Purchase changed in another session; reload and try again")
    before = _purchase_dict(purchase)
    payload = PurchaseCreate(
        league_id=purchase.league_id,
        franchise_id=update.franchise_id or purchase.franchise_id,
        player_id=purchase.player_id,
        amount=update.amount if update.amount is not None else purchase.amount,
        status=update.status or RosterStatus(purchase.status),
    )
    _validate(db, payload, ignore_purchase_id=purchase.id)
    purchase.franchise_id = payload.franchise_id
    purchase.amount = payload.amount
    purchase.status = payload.status.value
    purchase.version += 1
    purchase.updated_at = datetime.now(UTC)
    db.add(
        AuctionAuditEvent(
            league_id=purchase.league_id,
            action="update",
            entity_id=purchase.id,
            before_json=before,
            after_json=_purchase_dict(purchase),
        )
    )
    db.commit()
    db.refresh(purchase)
    return purchase


def delete_purchase(db: Session, purchase_id: str) -> None:
    purchase = db.get(AuctionPurchase, purchase_id)
    if not purchase or not purchase.active:
        raise AuctionValidationError("Purchase does not exist")
    snapshot = _purchase_dict(purchase)
    league_id = purchase.league_id
    db.delete(purchase)
    db.add(
        AuctionAuditEvent(
            league_id=league_id,
            action="delete",
            entity_id=purchase_id,
            before_json=snapshot,
        )
    )
    db.commit()


def _restore(db: Session, data: dict[str, Any]) -> AuctionPurchase:
    purchase = AuctionPurchase(
        id=str(data["id"]),
        league_id=str(data["league_id"]),
        franchise_id=str(data["franchise_id"]),
        player_id=str(data["player_id"]),
        amount=Decimal(str(data["amount"])),
        status=str(data["status"]),
        purchase_order=int(data["purchase_order"]),
        source=str(data.get("source", "local")),
        version=int(data.get("version", 1)),
    )
    db.add(purchase)
    return purchase


def undo(db: Session, league_id: str) -> None:
    event = db.scalar(
        select(AuctionAuditEvent)
        .where(AuctionAuditEvent.league_id == league_id, AuctionAuditEvent.undone.is_(False))
        .order_by(AuctionAuditEvent.id.desc())
    )
    if event is None:
        raise AuctionValidationError("Nothing to undo")
    if event.action == "create" and event.entity_id:
        db.execute(delete(AuctionPurchase).where(AuctionPurchase.id == event.entity_id))
    elif event.action == "delete" and event.before_json:
        _restore(db, event.before_json)
    elif event.action == "update" and event.before_json and event.entity_id:
        purchase = db.get(AuctionPurchase, event.entity_id)
        if purchase:
            previous = event.before_json
            purchase.franchise_id = str(previous["franchise_id"])
            purchase.amount = Decimal(str(previous["amount"]))
            purchase.status = str(previous["status"])
            purchase.version = int(previous["version"])
    event.undone = True
    db.commit()


def redo(db: Session, league_id: str) -> None:
    event = db.scalar(
        select(AuctionAuditEvent)
        .where(AuctionAuditEvent.league_id == league_id, AuctionAuditEvent.undone.is_(True))
        .order_by(AuctionAuditEvent.id.desc())
    )
    if event is None:
        raise AuctionValidationError("Nothing to redo")
    if event.action == "create" and event.after_json:
        _restore(db, event.after_json)
    elif event.action == "delete" and event.entity_id:
        db.execute(delete(AuctionPurchase).where(AuctionPurchase.id == event.entity_id))
    elif event.action == "update" and event.after_json and event.entity_id:
        purchase = db.get(AuctionPurchase, event.entity_id)
        if purchase:
            after = event.after_json
            purchase.franchise_id = str(after["franchise_id"])
            purchase.amount = Decimal(str(after["amount"]))
            purchase.status = str(after["status"])
            purchase.version = int(after["version"])
    event.undone = False
    db.commit()
