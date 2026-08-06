"""Store leagues and franchises discovered during MFL sign-in.

Revision ID: 0005_mfl_memberships
Revises: 0004_users_live_auction
"""

from __future__ import annotations

from alembic import op
from app.models import UserMFLMembership

revision = "0005_mfl_memberships"
down_revision = "0004_users_live_auction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    UserMFLMembership.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    op.drop_table("user_mfl_memberships")
