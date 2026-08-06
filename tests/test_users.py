from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.consensus import build_consensus
from app.main import update_preference
from app.models import DataSource, UserPlayerPreference
from app.schemas import PreferenceUpdate
from app.sources import initialize_sources
from app.user_context import reset_active_username, set_active_username
from app.users import (
    bootstrap_user,
    effective_auction_strategy,
    effective_source_settings,
    save_source_setting,
)


def test_source_and_player_settings_are_isolated_by_username(seeded: Session) -> None:
    initialize_sources(seeded)
    alice = set_active_username("Alice")
    try:
        save_source_setting(seeded, "sleeper", enabled=False, weight=Decimal("0.25"))
        update_preference(
            "00999",
            "0001234",
            PreferenceUpdate(target=True, tags=["sleeper"]),
            seeded,
        )
        assert effective_source_settings(seeded)["sleeper"]["enabled"] is False
        alice_row = next(
            row for row in build_consensus(seeded, "00999") if row["player_id"] == "0001234"
        )
        assert alice_row["preference"]["target"] is True
        assert alice_row["preference"]["tags"] == ["sleeper"]
    finally:
        reset_active_username(alice)

    bob = set_active_username("Bob")
    try:
        assert effective_source_settings(seeded)["sleeper"]["enabled"] is True
        bob_row = next(
            row for row in build_consensus(seeded, "00999") if row["player_id"] == "0001234"
        )
        assert bob_row["preference"]["target"] is False
    finally:
        reset_active_username(bob)

    assert seeded.scalar(select(DataSource).where(DataSource.id == "sleeper")).enabled is True
    assert seeded.scalar(select(UserPlayerPreference)).username == "alice"


def test_wilsonmw_is_admin_and_balanced_strategy_is_default(seeded: Session) -> None:
    account = bootstrap_user(seeded, "wilsonmw", admin_usernames={"wilsonmw"})
    strategy = effective_auction_strategy(seeded, "00999")

    assert account.is_admin is True
    assert strategy["template"] == "balanced"
    assert strategy["priority_order"] != ["WR", "QB", "RB", "TE", "DEF"]
