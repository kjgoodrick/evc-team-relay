"""Independent comment permission rules; no change to note-write grants."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "202611050001"
down_revision = "202611030002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "shares",
        sa.Column("comment_rules_revision", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_table(
        "managed_comment_rules",
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
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.UniqueConstraint(
            "share_id", "user_id", "path", "recursive", name="uq_managed_comment_rule"
        ),
    )
    op.create_index("ix_managed_comment_rules_share_id", "managed_comment_rules", ["share_id"])


def downgrade():
    op.drop_table("managed_comment_rules")
    op.drop_column("shares", "comment_rules_revision")
