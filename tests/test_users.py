from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import player_detail
from app.consensus import build_consensus, parse_ranking_csv
from app.main import update_preference
from app.models import DataSource, UserLeagueSetting, UserMFLMembership, UserPlayerPreference
from app.schemas import PreferenceUpdate
from app.settings_store import runtime_settings, save_commissioner_imports
from app.sources import initialize_sources
from app.user_context import reset_active_username, set_active_username
from app.users import (
    auction_rob_mode,
    bootstrap_user,
    effective_auction_strategy,
    effective_source_settings,
    reset_source_settings,
    save_auction_rob_mode,
    save_mfl_memberships,
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
            PreferenceUpdate(target=True, fade=True, tags=["sleeper"]),
            seeded,
        )
        assert effective_source_settings(seeded)["sleeper"]["enabled"] is False
        alice_row = next(
            row for row in build_consensus(seeded, "00999") if row["player_id"] == "0001234"
        )
        assert alice_row["preference"]["target"] is True
        assert alice_row["preference"]["fade"] is True
        assert alice_row["preference"]["tags"] == ["sleeper"]
        detail = player_detail(seeded, "00999", "0001234")
        assert detail is not None
        assert {"Target", "Fade", "Sleeper"}.issubset(detail["profile"]["flags"])
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


def test_uploaded_csv_is_private_while_bundled_sources_are_shared(
    seeded: Session, tmp_path
) -> None:
    initialize_sources(seeded)
    content = b"player_name,team,position,overall_rank\nLeading Zero,BUF,RB,1\n"

    alice_token = set_active_username("Alice")
    try:
        alice_import = parse_ranking_csv(
            seeded,
            "00999",
            content,
            "My draft board",
            confirm=True,
            import_directory=tmp_path,
        )
        alice_source_id = alice_import["source_id"]
        alice_settings = effective_source_settings(seeded)
        assert alice_source_id in alice_settings
        assert "fantasypros_redraft_csv" in alice_settings
    finally:
        reset_active_username(alice_token)

    bob_token = set_active_username("Bob")
    try:
        assert alice_source_id not in effective_source_settings(seeded)
        bob_import = parse_ranking_csv(
            seeded,
            "00999",
            content,
            "My draft board",
            confirm=True,
            import_directory=tmp_path,
        )
        bob_source_id = bob_import["source_id"]
        assert bob_source_id != alice_source_id
        assert bob_source_id in effective_source_settings(seeded)
        bob_row = next(
            row for row in build_consensus(seeded, "00999") if row["player_id"] == "0001234"
        )
        assert bob_source_id in bob_row["source_ranks"]
        assert alice_source_id not in bob_row["source_ranks"]
    finally:
        reset_active_username(bob_token)


def test_wilsonmw_is_admin_and_balanced_strategy_is_default(seeded: Session) -> None:
    account = bootstrap_user(seeded, "wilsonmw", admin_usernames={"wilsonmw"})
    strategy = effective_auction_strategy(seeded, "00999")

    assert account.is_admin is True
    assert strategy["template"] == "balanced"
    assert strategy["priority_order"] != ["WR", "QB", "RB", "TE", "DEF"]


def test_rob_mode_defaults_on_and_can_be_changed(seeded: Session) -> None:
    assert auction_rob_mode(seeded) is True

    assert save_auction_rob_mode(seeded, False) is False
    assert auction_rob_mode(seeded) is False

    assert save_auction_rob_mode(seeded, True) is True
    assert auction_rob_mode(seeded) is True


def test_commissioner_import_toggle_persists_true_and_false(seeded: Session) -> None:
    assert save_commissioner_imports(seeded, False).mfl_enable_imports is False
    assert runtime_settings(seeded).mfl_enable_imports is False

    assert save_commissioner_imports(seeded, True).mfl_enable_imports is True
    assert runtime_settings(seeded).mfl_enable_imports is True


def test_reset_source_settings_restores_defaults_for_only_current_user(
    seeded: Session,
) -> None:
    initialize_sources(seeded)
    alice = set_active_username("Alice")
    try:
        save_source_setting(seeded, "sleeper", enabled=False, weight=Decimal("8.5"))
        assert effective_source_settings(seeded)["sleeper"] == {
            "enabled": False,
            "weight": Decimal("8.5000"),
        }

        assert reset_source_settings(seeded) == 1
        assert effective_source_settings(seeded)["sleeper"] == {
            "enabled": True,
            "weight": Decimal("0.25"),
        }
    finally:
        reset_active_username(alice)


def test_initialize_sources_replaces_stale_global_weights_with_code_defaults(
    seeded: Session,
) -> None:
    initialize_sources(seeded)
    source = seeded.get(DataSource, "espn_dynasty_csv")
    assert source is not None
    source.weight = Decimal("9.5")
    seeded.commit()

    initialize_sources(seeded)

    assert Decimal(source.weight) == Decimal("1")


def test_mfl_membership_sets_the_users_franchise(seeded: Session) -> None:
    save_mfl_memberships(
        seeded,
        "Alice",
        2026,
        [
            {
                "league_id": "00999",
                "league_name": "Test League",
                "franchise_id": "0001",
                "source_url": None,
            }
        ],
    )

    membership = seeded.scalar(select(UserMFLMembership))
    setting = seeded.scalar(select(UserLeagueSetting))
    assert membership is not None
    assert membership.username == "alice"
    assert membership.franchise_id == "0001"
    assert setting is not None
    assert setting.username == "alice"
    assert setting.league_id == "00999"
    assert setting.franchise_id == "0001"
