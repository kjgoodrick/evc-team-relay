"""Durable receipts for explicit offline restore-generation rotation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202611080001"
down_revision = "202611070005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "collaboration_restores",
        sa.Column("generation", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("plan_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("target_sha256", sa.String(64), nullable=False),
        sa.Column("previous_state_sha256", sa.String(64), nullable=False),
        sa.Column("share_count", sa.Integer(), nullable=False),
        sa.Column("schema_versions", sa.JSON(), nullable=False),
        sa.Column("operator", sa.String(200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("share_count >= 0", name="ck_collaboration_restore_count"),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM collaboration_restores)")):
        raise RuntimeError("Cannot erase completed collaboration restore receipts")
    op.drop_table("collaboration_restores")
