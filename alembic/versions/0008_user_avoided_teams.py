"""Add per-user avoided NFL teams.

Revision ID: 0008_user_avoided_teams
Revises: 0007_interactive_auction
"""

from __future__ import annotations

from alembic import op
from app.models import UserAvoidedTeam

revision = "0008_user_avoided_teams"
down_revision = "0007_interactive_auction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    UserAvoidedTeam.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    op.drop_table("user_avoided_teams")
