"""Add online presence and interactive auction bidding.

Revision ID: 0007_interactive_auction
Revises: 0006_auction_nomination_order
"""

from __future__ import annotations

from alembic import op
from app.models import InteractiveAuctionBid, InteractiveAuctionState, UserPresence

revision = "0007_interactive_auction"
down_revision = "0006_auction_nomination_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for model in (UserPresence, InteractiveAuctionState, InteractiveAuctionBid):
        model.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    for table in ("interactive_auction_bids", "interactive_auction_states", "user_presence"):
        op.drop_table(table)
