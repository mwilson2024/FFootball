import json
from datetime import UTC, datetime
from decimal import Decimal
from random import SystemRandom
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AppSetting,
    AuctionAuditEvent,
    AuctionNominationState,
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


def _nomination_baseline_key(league_id: str) -> str:
    return f"auction_nomination_baseline:{league_id}"


def _nomination_purchase_baseline(db: Session, league_id: str) -> set[str]:
    setting = db.get(AppSetting, _nomination_baseline_key(league_id))
    if setting is None:
        return set()
    try:
        value = json.loads(setting.value)
    except (json.JSONDecodeError, TypeError):
        return set()
    return {str(item) for item in value} if isinstance(value, list) else set()


def _set_nomination_purchase_baseline(db: Session, league_id: str) -> None:
    key = _nomination_baseline_key(league_id)
    setting = db.get(AppSetting, key)
    if setting is None:
        setting = AppSetting(key=key, value="[]")
        db.add(setting)
    purchase_ids = list(
        db.scalars(
            select(AuctionPurchase.id).where(
                AuctionPurchase.league_id == league_id,
                AuctionPurchase.active.is_(True),
            )
        )
    )
    setting.value = json.dumps(sorted(str(item) for item in purchase_ids))
    setting.updated_at = datetime.now(UTC)


def _nomination_franchises(db: Session, league_id: str) -> list[Franchise]:
    return list(
        db.scalars(
            select(Franchise)
            .where(Franchise.league_id == league_id)
            .order_by(Franchise.name, Franchise.id)
        )
    )


def _nomination_state_row(db: Session, league_id: str) -> AuctionNominationState:
    if db.scalar(select(League.id).where(League.id == league_id)) is None:
        raise AuctionValidationError("League does not exist")
    franchises = _nomination_franchises(db, league_id)
    valid_ids = {item.id for item in franchises}
    state = db.get(AuctionNominationState, league_id)
    if state is None:
        state = AuctionNominationState(
            league_id=league_id,
            order_json=[item.id for item in franchises],
        )
        db.add(state)
        db.commit()
        db.refresh(state)
        return state
    stored = [str(item) for item in (state.order_json or [])]
    order = [item for item in stored if item in valid_ids]
    order.extend(item.id for item in franchises if item.id not in order)
    if order != stored:
        state.order_json = order
        state.cursor = 0
        _set_nomination_purchase_baseline(db, league_id)
        state.updated_at = datetime.now(UTC)
        db.commit()
        db.refresh(state)
    return state


def _snake_slot(order: list[str], cursor: int) -> tuple[str | None, int, str]:
    if not order:
        return None, 0, "forward"
    round_index, offset = divmod(max(0, cursor), len(order))
    direction = "forward" if round_index % 2 == 0 else "reverse"
    index = offset if direction == "forward" else len(order) - 1 - offset
    return order[index], round_index + 1, direction


def _next_open_snake_slot(
    order: list[str], cursor: int, completed: set[str]
) -> tuple[str | None, int, str, int]:
    start = max(0, cursor)
    for candidate in range(start, start + max(1, len(order) * 2)):
        franchise_id, round_number, direction = _snake_slot(order, candidate)
        if franchise_id is not None and franchise_id not in completed:
            return franchise_id, round_number, direction, candidate
    return None, 0, "forward", start


def _synced_roster_counts(db: Session, league_id: str) -> dict[str, int]:
    return {
        str(franchise_id): int(count)
        for franchise_id, count in db.execute(
            select(RosterAssignment.franchise_id, func.count())
            .where(RosterAssignment.league_id == league_id)
            .group_by(RosterAssignment.franchise_id)
        )
    }


def _completed_from_counts(franchises: list[Franchise], roster_counts: dict[str, int]) -> set[str]:
    return {
        franchise.id
        for franchise in franchises
        if roster_counts.get(franchise.id, 0) >= franchise.roster_slots
    }


def _reconciled_nomination_position(
    db: Session, league_id: str, order: list[str]
) -> tuple[int, set[str]]:
    """Replay active purchases so removals and undo/redo cannot desynchronize the turn."""
    franchises = _nomination_franchises(db, league_id)
    roster_counts = _synced_roster_counts(db, league_id)
    synchronized_player_ids = set(
        db.scalars(
            select(RosterAssignment.player_id).where(RosterAssignment.league_id == league_id)
        )
    )
    baseline_ids = _nomination_purchase_baseline(db, league_id)
    purchases = list(
        db.scalars(
            select(AuctionPurchase)
            .where(
                AuctionPurchase.league_id == league_id,
                AuctionPurchase.active.is_(True),
            )
            .order_by(
                AuctionPurchase.purchase_order, AuctionPurchase.created_at, AuctionPurchase.id
            )
        )
    )
    cursor = 0
    for purchase in purchases:
        if purchase.id in baseline_ids and purchase.player_id not in synchronized_player_ids:
            roster_counts[purchase.franchise_id] = roster_counts.get(purchase.franchise_id, 0) + 1
    for purchase in purchases:
        if purchase.id in baseline_ids:
            continue
        completed = _completed_from_counts(franchises, roster_counts)
        current_id, _, _, current_cursor = _next_open_snake_slot(order, cursor, completed)
        if current_id is None:
            break
        if purchase.player_id not in synchronized_player_ids:
            roster_counts[purchase.franchise_id] = roster_counts.get(purchase.franchise_id, 0) + 1
        cursor = current_cursor + 1
    completed = _completed_from_counts(franchises, roster_counts)
    _, _, _, current_cursor = _next_open_snake_slot(order, cursor, completed)
    return current_cursor, completed


def auction_progress(db: Session, league_id: str) -> dict[str, int | None]:
    franchises = _nomination_franchises(db, league_id)
    team_count = len(franchises)
    total_capacity = sum(max(0, item.roster_slots) for item in franchises)
    synchronized_player_ids = set(
        db.scalars(
            select(RosterAssignment.player_id).where(RosterAssignment.league_id == league_id)
        )
    )
    auction_player_ids = set(
        db.scalars(
            select(AuctionPurchase.player_id).where(
                AuctionPurchase.league_id == league_id, AuctionPurchase.active.is_(True)
            )
        )
    )
    synchronized_players = len(synchronized_player_ids)
    auction_purchases = len(auction_player_ids)
    filled_slots = len(synchronized_player_ids | auction_player_ids)
    remaining_slots = max(0, total_capacity - filled_slots)
    return {
        "total_capacity": total_capacity,
        "synchronized_players": synchronized_players,
        "auction_purchases": auction_purchases,
        "team_count": team_count,
        "filled_slots": filled_slots,
        "remaining_slots": remaining_slots,
        "overall_pick": filled_slots + 1 if remaining_slots else None,
        "auction_pick": auction_purchases + 1 if remaining_slots else None,
        "auction_round": auction_purchases // team_count + 1
        if remaining_slots and team_count
        else None,
    }


def nomination_state(db: Session, league_id: str) -> dict[str, Any]:
    state = _nomination_state_row(db, league_id)
    order = [str(item) for item in (state.order_json or [])]
    reconciled_cursor, completed = _reconciled_nomination_position(db, league_id, order)
    current_id, round_number, direction, current_cursor = _next_open_snake_slot(
        order, reconciled_cursor, completed
    )
    if current_cursor != state.cursor:
        state.cursor = current_cursor
        state.updated_at = datetime.now(UTC)
        db.commit()
    next_id, next_round, next_direction, _ = (
        _next_open_snake_slot(order, current_cursor + 1, completed)
        if current_id is not None
        else (None, 0, "forward", current_cursor)
    )
    names = {item.id: item.name for item in _nomination_franchises(db, league_id)}
    progress = auction_progress(db, league_id)
    return {
        "order": [
            {
                "franchise_id": franchise_id,
                "franchise_name": names.get(franchise_id, franchise_id),
                "position": index,
                "is_current": franchise_id == current_id,
                "is_next": franchise_id == next_id,
                "is_complete": franchise_id in completed,
            }
            for index, franchise_id in enumerate(order, start=1)
        ],
        "cursor": state.cursor,
        "round": round_number,
        "direction": direction,
        "current_franchise_id": current_id,
        "current_franchise_name": names.get(current_id) if current_id else None,
        "next_franchise_id": next_id,
        "next_franchise_name": names.get(next_id) if next_id else None,
        "next_round": next_round,
        "next_direction": next_direction,
        "updated_at": state.updated_at,
        "updated_by": state.updated_by,
        "completed_count": len(completed),
        "all_complete": bool(order) and len(completed) == len(order),
        **progress,
    }


def set_nomination_order(
    db: Session, league_id: str, franchise_ids: list[str], *, actor: str | None = None
) -> dict[str, Any]:
    state = _nomination_state_row(db, league_id)
    available = {item.id for item in _nomination_franchises(db, league_id)}
    requested = [str(item) for item in franchise_ids]
    if len(requested) != len(set(requested)):
        raise AuctionValidationError("Each franchise may appear only once in nomination order")
    if set(requested) != available:
        raise AuctionValidationError("Nomination order must include every franchise exactly once")
    state.order_json = requested
    state.cursor = 0
    _set_nomination_purchase_baseline(db, league_id)
    state.updated_by = actor
    state.updated_at = datetime.now(UTC)
    db.commit()
    return nomination_state(db, league_id)


def shuffle_nomination_order(
    db: Session, league_id: str, *, actor: str | None = None
) -> dict[str, Any]:
    franchise_ids = [item.id for item in _nomination_franchises(db, league_id)]
    SystemRandom().shuffle(franchise_ids)
    return set_nomination_order(db, league_id, franchise_ids, actor=actor)


def advance_nomination(db: Session, league_id: str, *, actor: str | None = None) -> None:
    state = _nomination_state_row(db, league_id)
    order = [str(item) for item in (state.order_json or [])]
    state.cursor, _ = _reconciled_nomination_position(db, league_id, order)
    state.updated_by = actor
    state.updated_at = datetime.now(UTC)
    db.commit()


def reset_nomination_cursor(
    db: Session, league_id: str, *, actor: str | None = None
) -> dict[str, Any]:
    state = _nomination_state_row(db, league_id)
    state.cursor = 0
    _set_nomination_purchase_baseline(db, league_id)
    state.updated_by = actor
    state.updated_at = datetime.now(UTC)
    db.commit()
    return nomination_state(db, league_id)


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
    synced_player_ids = set(
        db.scalars(
            select(RosterAssignment.player_id).where(RosterAssignment.league_id == league.id)
        )
    )
    local_player_ids = set(
        db.scalars(
            select(AuctionPurchase.player_id).where(
                AuctionPurchase.league_id == league.id,
                AuctionPurchase.franchise_id == franchise.id,
                AuctionPurchase.active.is_(True),
            )
        )
    )
    local_count = len(local_player_ids - synced_player_ids)
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
    if payload.amount != payload.amount.to_integral_value():
        raise AuctionValidationError("Bid must be a whole dollar amount")
    if not _precision_valid(payload.amount, minimum_bid, league.settings_json.get("precision")):
        raise AuctionValidationError("Bid has more precision than this league permits")
    budget = franchise_budget(db, league, franchise)
    keeps_existing_slot = False
    if ignore_purchase_id:
        current = db.get(AuctionPurchase, ignore_purchase_id)
        if current and current.franchise_id == franchise.id:
            keeps_existing_slot = True
            budget["maximum_bid"] = Decimal(budget["maximum_bid"]) + Decimal(current.amount)
    if budget["slots_remaining"] <= 0 and not keeps_existing_slot:
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
        player_id=update.player_id or purchase.player_id,
        amount=update.amount if update.amount is not None else purchase.amount,
        status=update.status or RosterStatus(purchase.status),
    )
    _validate(db, payload, ignore_purchase_id=purchase.id)
    purchase.franchise_id = payload.franchise_id
    purchase.player_id = payload.player_id
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
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AuctionValidationError("Replacement player is already sold") from exc
    db.refresh(purchase)
    return purchase


def reset_auction(db: Session, league_id: str) -> int:
    purchases = list(
        db.scalars(select(AuctionPurchase).where(AuctionPurchase.league_id == league_id))
    )
    if purchases:
        db.add(
            AuctionAuditEvent(
                league_id=league_id,
                action="reset",
                before_json={"purchases": [_purchase_dict(item) for item in purchases]},
            )
        )
        db.execute(delete(AuctionPurchase).where(AuctionPurchase.league_id == league_id))
        db.commit()
    return len(purchases)


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
            purchase.player_id = str(previous["player_id"])
            purchase.amount = Decimal(str(previous["amount"]))
            purchase.status = str(previous["status"])
            purchase.version = int(previous["version"])
    elif event.action == "reset" and event.before_json:
        for purchase_data in event.before_json.get("purchases", []):
            _restore(db, purchase_data)
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
            purchase.player_id = str(after["player_id"])
            purchase.amount = Decimal(str(after["amount"]))
            purchase.status = str(after["status"])
            purchase.version = int(after["version"])
    elif event.action == "reset":
        db.execute(delete(AuctionPurchase).where(AuctionPurchase.league_id == league_id))
    event.undone = False
    db.commit()
