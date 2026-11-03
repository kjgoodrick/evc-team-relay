"""Server-owned inventory and per-user path policy for new private shares."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "202611030002"
down_revision = "202611030001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "shares", sa.Column("managed", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.create_table(
        "managed_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "share_id",
            UUID(as_uuid=True),
            sa.ForeignKey("shares.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("last_path", sa.String(512), nullable=True),
        sa.Column("record", sa.JSON(), nullable=False),
        sa.Column("activity", sa.String(128), nullable=True),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("share_id", "path", name="uq_managed_path"),
    )
    op.create_index("ix_managed_entries_share_id", "managed_entries", ["share_id"])
    op.create_table(
        "managed_rules",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "share_id",
            UUID(as_uuid=True),
            sa.ForeignKey("shares.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("recursive", sa.Boolean(), nullable=False),
        sa.Column("permission", sa.String(8), nullable=False),
        sa.UniqueConstraint("share_id", "user_id", "path", "recursive", name="uq_managed_rule"),
    )
    op.create_index("ix_managed_rules_share_id", "managed_rules", ["share_id"])


def downgrade():
    op.drop_table("managed_rules")
    op.drop_table("managed_entries")
    op.drop_column("shares", "managed")
