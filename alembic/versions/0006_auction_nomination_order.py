"""Add persistent auction nomination order.

Revision ID: 0006_auction_nomination_order
Revises: 0005_mfl_memberships
"""

from __future__ import annotations

from alembic import op
from app.models import AuctionNominationState

revision = "0006_auction_nomination_order"
down_revision = "0005_mfl_memberships"
branch_labels = None
depends_on = None


def upgrade() -> None:
    AuctionNominationState.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    op.drop_table("auction_nomination_states")
