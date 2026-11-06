"""Verified standalone Bases definitions and saved-view identity registry."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "202611060003"
down_revision = "202611060002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "collaboration_bases",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "entry_id",
            sa.String(36),
            sa.ForeignKey("managed_entries.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("generation", UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
    )
    op.create_table(
        "collaboration_base_views",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "definition_id",
            UUID(as_uuid=True),
            sa.ForeignKey("collaboration_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slot", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("descriptor", sa.JSON(), nullable=False),
        sa.UniqueConstraint("definition_id", "slot", name="uq_collaboration_base_view_slot"),
    )
    op.create_index(
        "ix_collaboration_base_view_state",
        "collaboration_base_views",
        ["definition_id", "state", "id"],
    )
    op.create_table(
        "collaboration_base_revisions",
        sa.Column(
            "definition_id",
            UUID(as_uuid=True),
            sa.ForeignKey("collaboration_bases.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column(
            "actor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("operation_id", UUID(as_uuid=True), nullable=True),
        sa.Column("request_hash", sa.String(64), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("actor_id", "operation_id", name="uq_collaboration_base_operation"),
    )


def downgrade():
    op.drop_table("collaboration_base_revisions")
    op.drop_table("collaboration_base_views")
    op.drop_table("collaboration_bases")
