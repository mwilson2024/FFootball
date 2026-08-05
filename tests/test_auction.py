from decimal import Decimal

import pytest

from app.auction import AuctionValidationError, add_purchase, franchise_budget, undo
from app.models import Franchise, League, RosterStatus
from app.schemas import PurchaseCreate


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
