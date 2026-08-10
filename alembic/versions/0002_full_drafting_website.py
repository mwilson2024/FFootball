"""Add the full drafting website data model.

Revision ID: 0002_full_drafting_website
Revises: 0001_initial
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.db import Base

revision = "0002_full_drafting_website"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("players") as batch:
        batch.add_column(sa.Column("fantasy_positions_json", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("injury_status", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("practice_participation", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("bye_week", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("metadata_json", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))

    with op.batch_alter_table("roster_assignments") as batch:
        batch.add_column(sa.Column("salary", sa.Numeric(12, 2), nullable=True))
        batch.add_column(sa.Column("contract_info", sa.String(length=255), nullable=True))

    # The ORM metadata is the single source of truth for the new tables and
    # indexes. create_all is intentionally used only for missing objects; the
    # existing tables and data remain untouched.
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    for table_name in (
        "draft_audit_events",
        "draft_picks",
        "draft_sessions",
        "personal_player_preferences",
        "consensus_snapshots",
        "source_player_values",
        "player_identities",
        "data_sources",
        "app_settings",
    ):
        op.drop_table(table_name)

    with op.batch_alter_table("roster_assignments") as batch:
        batch.drop_column("contract_info")
        batch.drop_column("salary")

    with op.batch_alter_table("players") as batch:
        batch.drop_column("updated_at")
        batch.drop_column("metadata_json")
        batch.drop_column("bye_week")
        batch.drop_column("practice_participation")
        batch.drop_column("injury_status")
        batch.drop_column("fantasy_positions_json")
