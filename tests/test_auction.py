from decimal import Decimal

import pytest

from app.auction import (
    AuctionValidationError,
    add_purchase,
    advance_nomination,
    franchise_budget,
    nomination_state,
    reset_auction,
    reset_nomination_cursor,
    set_nomination_order,
    undo,
    update_purchase,
)
from app.models import AuctionPurchase, Franchise, League, Player, RosterAssignment, RosterStatus
from app.schemas import PurchaseCreate, PurchaseUpdate


def sale(player: str = "0001234", franchise: str = "0001", amount: str = "17") -> PurchaseCreate:
    return PurchaseCreate(
        league_id="00999", franchise_id=franchise, player_id=player, amount=Decimal(amount)
    )


def test_legal_purchase_updates_budget_and_maximum(seeded):
    purchase = add_purchase(seeded, sale())
    league = seeded.get(League, ("00999", 2026))
    franchise = seeded.query(Franchise).filter_by(league_id="00999", id="0001").one()
    budget = franchise_budget(seeded, league, franchise)
    assert purchase.player_id == "0001234"
    assert budget["spent"] == Decimal("17")
    assert budget["remaining"] == Decimal("3")
    assert budget["slots_remaining"] == 3
    assert budget["maximum_bid"] == Decimal("1")


def test_auction_purchase_status_is_always_roster(seeded):
    payload = sale()
    payload.status = RosterStatus.TAXI_SQUAD
    assert add_purchase(seeded, payload).status == RosterStatus.ROSTER.value


def test_rejects_illegal_and_duplicate_sales(seeded):
    with pytest.raises(AuctionValidationError, match="at least"):
        add_purchase(seeded, sale(amount="0.50"))
    with pytest.raises(AuctionValidationError, match="maximum"):
        add_purchase(seeded, sale(amount="18"))
    add_purchase(seeded, sale(amount="10"))
    with pytest.raises(AuctionValidationError, match="already sold"):
        add_purchase(seeded, sale(franchise="0002", amount="2"))


def test_decimal_precision_and_undo(seeded):
    with pytest.raises(AuctionValidationError, match="precision"):
        add_purchase(seeded, sale(amount="1.50"))
    add_purchase(seeded, sale(amount="4"))
    undo(seeded, "00999")
    add_purchase(seeded, sale(franchise="0002", amount="3"))


def test_nomination_order_can_be_arranged_and_advances_as_a_snake(seeded):
    initial = nomination_state(seeded, "00999")
    assert initial["current_franchise_name"] == "Alpha"
    assert initial["next_franchise_name"] == "Beta"

    arranged = set_nomination_order(seeded, "00999", ["0002", "0001"], actor="admin")
    assert [item["franchise_id"] for item in arranged["order"]] == ["0002", "0001"]
    assert arranged["current_franchise_name"] == "Beta"
    assert arranged["cursor"] == 0

    advance_nomination(seeded, "00999", actor="admin")
    advanced = nomination_state(seeded, "00999")
    assert advanced["current_franchise_name"] == "Alpha"
    assert advanced["round"] == 1


def test_full_franchise_is_marked_complete_and_skipped_for_nominations(seeded):
    seeded.add(
        Franchise(
            id="0003",
            league_id="00999",
            name="Gamma",
            starting_budget=Decimal("20"),
            roster_slots=4,
        )
    )
    for index in range(4):
        player_id = f"full-{index}"
        seeded.add(Player(id=player_id, name=f"Full Player {index}", position="WR"))
        seeded.add(
            RosterAssignment(
                league_id="00999",
                franchise_id="0002",
                player_id=player_id,
                status=RosterStatus.ROSTER.value,
            )
        )
    seeded.commit()

    state = nomination_state(seeded, "00999")
    beta = next(item for item in state["order"] if item["franchise_id"] == "0002")

    assert beta["is_complete"] is True
    assert state["current_franchise_name"] == "Alpha"
    assert state["next_franchise_name"] == "Gamma"
    advance_nomination(seeded, "00999", actor="admin")
    assert nomination_state(seeded, "00999")["current_franchise_name"] == "Gamma"


def test_admin_can_replace_player_on_existing_purchase(seeded):
    purchase = add_purchase(seeded, sale())

    updated = update_purchase(
        seeded,
        purchase.id,
        PurchaseUpdate(player_id="99", version=purchase.version),
    )

    assert updated.player_id == "99"
    assert updated.version == 2
    assert add_purchase(seeded, sale(amount="1")).player_id == "0001234"


def test_reset_auction_clears_purchases_and_restarts_nominations(seeded):
    add_purchase(seeded, sale())
    advance_nomination(seeded, "00999", actor="admin")
    assert nomination_state(seeded, "00999")["cursor"] == 1

    assert reset_auction(seeded, "00999") == 1
    state = reset_nomination_cursor(seeded, "00999", actor="admin")

    assert seeded.query(AuctionPurchase).filter_by(league_id="00999").count() == 0
    assert state["cursor"] == 0
    assert state["current_franchise_name"] == "Alpha"


def test_reset_and_player_replacement_are_undoable(seeded):
    purchase = add_purchase(seeded, sale())
    update_purchase(
        seeded,
        purchase.id,
        PurchaseUpdate(player_id="99", version=purchase.version),
    )

    undo(seeded, "00999")
    assert seeded.get(AuctionPurchase, purchase.id).player_id == "0001234"

    reset_auction(seeded, "00999")
    undo(seeded, "00999")
    restored = seeded.get(AuctionPurchase, purchase.id)
    assert restored is not None
    assert restored.player_id == "0001234"
