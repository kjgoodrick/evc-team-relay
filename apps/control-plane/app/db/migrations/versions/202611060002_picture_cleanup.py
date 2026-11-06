"""Resumable private-picture inventory and durable retired identities."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "202611060002"
down_revision = "202611060001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "discussion_picture_sweeps",
        sa.Column("bucket", sa.String(63), primary_key=True),
        sa.Column("cursor", sa.String(1024), nullable=True),
        sa.Column("cycle_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", UUID(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(64), nullable=True),
    )
    op.create_table(
        "discussion_picture_retirements",
        sa.Column("bucket", sa.String(63), primary_key=True),
        sa.Column("prefix", sa.String(192), primary_key=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_discussion_picture_retirements_seen",
        "discussion_picture_retirements",
        ["bucket", "retired_at", "last_seen_at"],
    )


def downgrade():
    op.drop_table("discussion_picture_retirements")
    op.drop_table("discussion_picture_sweeps")
