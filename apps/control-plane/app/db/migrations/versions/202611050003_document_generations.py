"""Server-owned CRDT lineage for managed text-anchor validation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "202611050003"
down_revision = "202611050002"
branch_labels = None
depends_on = None


def upgrade():
    # Legacy rows are assigned lazily under a row lock. No extension or large
    # data rewrite is required, and inventory updates cannot supply this field.
    op.add_column("managed_entries", sa.Column("collaboration_generation", UUID(as_uuid=True)))


def downgrade():
    op.drop_column("managed_entries", "collaboration_generation")
