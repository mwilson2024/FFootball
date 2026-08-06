"""Add user-owned settings and live auction state.

Revision ID: 0004_users_live_auction
Revises: 0003_diagnostics_and_sources
"""

from __future__ import annotations

from alembic import op
from app.models import (
    AuctionLiveState,
    UserAccount,
    UserLeagueSetting,
    UserPlayerPreference,
    UserSourceSetting,
)

revision = "0004_users_live_auction"
down_revision = "0003_diagnostics_and_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for model in (
        UserAccount,
        UserSourceSetting,
        UserLeagueSetting,
        UserPlayerPreference,
        AuctionLiveState,
    ):
        model.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    for table in (
        "auction_live_states",
        "user_player_preferences",
        "user_league_settings",
        "user_source_settings",
        "user_accounts",
    ):
        op.drop_table(table)
