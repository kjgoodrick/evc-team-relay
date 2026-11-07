"""Persist group identity separately from each thread's source evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "202611060006"
down_revision = "202611060005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "collaboration_base_group_values",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "definition_id",
            UUID(as_uuid=True),
            sa.ForeignKey("collaboration_base_target_definitions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("definition_revision", sa.BigInteger(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "definition_id", "definition_revision", "fingerprint", name="uq_base_group_value"
        ),
    )
    op.create_table(
        "collaboration_base_group_witnesses",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id",
            UUID(as_uuid=True),
            sa.ForeignKey("collaboration_base_group_values.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("group_id", "fingerprint", name="uq_base_group_witness"),
    )
    op.create_table(
        "collaboration_base_group_capture_receipts",
        sa.Column(
            "actor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("operation_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "witness_id",
            UUID(as_uuid=True),
            sa.ForeignKey("collaboration_base_group_witnesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_collaboration_base_group_capture_receipts_witness_id",
        "collaboration_base_group_capture_receipts",
        ["witness_id"],
    )


def downgrade():
    for table in (
        "collaboration_base_group_values",
        "collaboration_base_group_witnesses",
        "collaboration_base_group_capture_receipts",
    ):
        if op.get_bind().scalar(sa.text("SELECT count(*) FROM " + table)):
            raise RuntimeError(
                "Export/migrate group targets and source evidence before downgrading"
            )
    op.drop_table("collaboration_base_group_capture_receipts")
    op.drop_table("collaboration_base_group_witnesses")
    op.drop_table("collaboration_base_group_values")
