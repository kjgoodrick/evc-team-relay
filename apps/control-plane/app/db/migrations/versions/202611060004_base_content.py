"""Atomic, conditional publication of bounded Bases configuration bytes."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "202611060004"
down_revision = "202611060003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "managed_base_content",
        sa.Column(
            "entry_id",
            sa.String(36),
            sa.ForeignKey("managed_entries.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("version", UUID(as_uuid=True), nullable=False),
        sa.Column("generation", UUID(as_uuid=True), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
    )
    op.create_table(
        "managed_base_receipts",
        sa.Column(
            "actor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("operation_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "entry_id",
            sa.String(36),
            sa.ForeignKey("managed_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_managed_base_receipts_entry_id", "managed_base_receipts", ["entry_id"])


def downgrade():
    # Nonempty configuration cannot be downgraded into the mutable attachment
    # path without an explicit export/migration. Refuse data loss.
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT count(*) FROM managed_base_content")):
        raise RuntimeError("Export/migrate guarded Bases content before downgrading")
    op.drop_table("managed_base_receipts")
    op.drop_table("managed_base_content")
