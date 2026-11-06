"""Persist view-local formula, summary and grouping definition revisions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "202611060005"
down_revision = "202611060004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "collaboration_base_target_definitions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "view_id",
            UUID(as_uuid=True),
            sa.ForeignKey("collaboration_base_views.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("observed_revision", sa.BigInteger(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("descriptor", sa.JSON(), nullable=False),
        sa.UniqueConstraint("view_id", "kind", "name", name="uq_base_target_definition_key"),
    )
    op.create_index(
        "ix_base_target_definition_active",
        "collaboration_base_target_definitions",
        ["view_id", "active", "id"],
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM collaboration_base_target_definitions")):
        raise RuntimeError("Export/migrate Bases target identities before downgrading")
    op.drop_table("collaboration_base_target_definitions")
