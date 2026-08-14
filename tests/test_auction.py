from decimal import Decimal

import pytest

from app.auction import (
    AuctionValidationError,
    add_purchase,
    advance_nomination,
    delete_purchase,
    franchise_budget,
    nomination_state,
    redo,
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

    add_purchase(seeded, sale(franchise="0002", amount="1"))
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
    add_purchase(seeded, sale(amount="1"))
    advance_nomination(seeded, "00999", actor="admin")
    assert nomination_state(seeded, "00999")["current_franchise_name"] == "Gamma"


def test_pick_progress_and_turn_reconcile_after_purchase_removal(seeded):
    franchises = seeded.query(Franchise).filter_by(league_id="00999").all()
    for franchise in franchises:
        franchise.roster_slots = 60
    for index in range(106):
        player_id = f"keeper-{index}"
        seeded.add(Player(id=player_id, name=f"Rostered Player {index}", position="WR"))
        seeded.add(
            RosterAssignment(
                league_id="00999",
                franchise_id=franchises[index % len(franchises)].id,
                player_id=player_id,
                status=RosterStatus.ROSTER.value,
            )
        )
    seeded.commit()

    initial = nomination_state(seeded, "00999")
    assert initial["total_capacity"] == 120
    assert initial["synchronized_players"] == 106
    assert initial["overall_pick"] == 107
    assert initial["auction_pick"] == 1
    assert initial["current_franchise_name"] == "Alpha"

    purchase = add_purchase(seeded, sale(amount="1"))
    advance_nomination(seeded, "00999", actor="admin")
    advanced = nomination_state(seeded, "00999")
    assert advanced["overall_pick"] == 108
    assert advanced["auction_pick"] == 2
    assert advanced["current_franchise_name"] == "Beta"

    seeded.add(
        RosterAssignment(
            league_id="00999",
            franchise_id="0001",
            player_id=purchase.player_id,
            status=RosterStatus.ROSTER.value,
        )
    )
    seeded.commit()
    synchronized = nomination_state(seeded, "00999")
    assert synchronized["synchronized_players"] == 107
    assert synchronized["auction_purchases"] == 1
    assert synchronized["filled_slots"] == 107
    assert synchronized["overall_pick"] == 108

    synced_assignment = seeded.query(RosterAssignment).filter_by(player_id=purchase.player_id).one()
    seeded.delete(synced_assignment)
    seeded.commit()

    delete_purchase(seeded, purchase.id)
    reconciled = nomination_state(seeded, "00999")
    assert reconciled["overall_pick"] == 107
    assert reconciled["auction_pick"] == 1
    assert reconciled["current_franchise_name"] == "Alpha"
    assert reconciled["cursor"] == 0


def test_reordering_mid_auction_starts_a_new_nomination_baseline(seeded):
    original = add_purchase(seeded, sale(amount="1"))
    advance_nomination(seeded, "00999", actor="admin")
    assert nomination_state(seeded, "00999")["current_franchise_name"] == "Beta"

    reordered = set_nomination_order(seeded, "00999", ["0002", "0001"], actor="admin")

    assert reordered["current_franchise_name"] == "Beta"
    assert reordered["cursor"] == 0
    assert reordered["auction_pick"] == 2

    delete_purchase(seeded, original.id)
    replacement = add_purchase(seeded, sale(player="99", franchise="0002", amount="1"))
    advance_nomination(seeded, "00999", actor="admin")
    replaced = nomination_state(seeded, "00999")

    assert replacement.purchase_order == 1
    assert replaced["auction_pick"] == 2
    assert replaced["current_franchise_name"] == "Alpha"


def test_reordering_still_skips_a_team_filled_by_baseline_purchases(seeded):
    alpha = seeded.query(Franchise).filter_by(league_id="00999", id="0001").one()
    alpha.roster_slots = 2
    seeded.commit()
    add_purchase(seeded, sale(player="0001234", amount="1"))
    add_purchase(seeded, sale(player="99", amount="1"))

    reordered = set_nomination_order(seeded, "00999", ["0001", "0002"], actor="admin")

    alpha_state = next(row for row in reordered["order"] if row["franchise_id"] == "0001")
    assert alpha_state["is_complete"] is True
    assert reordered["current_franchise_name"] == "Beta"


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


def test_undo_and_redo_reconcile_nomination_turn_and_pick(seeded):
    add_purchase(seeded, sale(amount="1"))
    advance_nomination(seeded, "00999", actor="admin")
    sold = nomination_state(seeded, "00999")
    assert sold["cursor"] == 1
    assert sold["overall_pick"] == 2

    undo(seeded, "00999")
    undone = nomination_state(seeded, "00999")
    assert undone["cursor"] == 0
    assert undone["overall_pick"] == 1

    redo(seeded, "00999")
    redone = nomination_state(seeded, "00999")
    assert redone["cursor"] == 1
    assert redone["overall_pick"] == 2
