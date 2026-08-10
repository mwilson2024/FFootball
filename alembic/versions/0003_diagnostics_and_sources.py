"""Add persistent synchronization diagnostics.

Revision ID: 0003_diagnostics_and_sources
Revises: 0002_full_drafting_website
"""

from __future__ import annotations

from alembic import op
from app.models import SyncWarning

revision = "0003_diagnostics_and_sources"
down_revision = "0002_full_drafting_website"
branch_labels = None
depends_on = None


def upgrade() -> None:
    SyncWarning.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    op.drop_table("sync_warnings")
