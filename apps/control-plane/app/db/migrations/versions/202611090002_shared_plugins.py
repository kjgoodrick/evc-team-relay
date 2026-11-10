"""Owner-managed shared plugin setup; no vault configuration bytes are mirrored."""

import sqlalchemy as sa
from alembic import op

revision = "202611090002"
down_revision = "202611090001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "shares",
        sa.Column("plugin_setup_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "shares", sa.Column("plugin_setup", sa.JSON(), nullable=False, server_default="[]")
    )


def downgrade():
    op.drop_column("shares", "plugin_setup")
    op.drop_column("shares", "plugin_setup_revision")
