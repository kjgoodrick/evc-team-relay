"""Separate service-restore lineage from ordinary discussion cursor resets."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202611070005"
down_revision = "202611070004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "shares",
        sa.Column("collaboration_restore_generation", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade():
    # Removing an activated guard would let older queued requests write again.
    # A populated restored deployment needs an explicit migration strategy.
    active = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM shares "
            "WHERE collaboration_restore_generation IS NOT NULL)"
        )
    )
    if active:
        raise RuntimeError("Cannot downgrade an activated collaboration restore guard")
    op.drop_column("shares", "collaboration_restore_generation")
