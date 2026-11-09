"""Permit distinct inline Bases definitions in one managed Markdown entry."""

import sqlalchemy as sa
from alembic import op

revision = "202611090001"
down_revision = "202611080002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "collaboration_bases",
        sa.Column("source_key", sa.String(64), nullable=False, server_default="file"),
    )
    op.add_column("collaboration_bases", sa.Column("source_anchor", sa.JSON(), nullable=True))
    op.drop_constraint("collaboration_bases_entry_id_key", "collaboration_bases", type_="unique")
    op.create_unique_constraint(
        "uq_collaboration_base_source", "collaboration_bases", ["entry_id", "source_key"]
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM collaboration_bases WHERE source_key <> 'file')")
    ):
        raise RuntimeError("Cannot remove inline definition identities while they are in use")
    op.drop_constraint("uq_collaboration_base_source", "collaboration_bases", type_="unique")
    op.create_unique_constraint(
        "collaboration_bases_entry_id_key", "collaboration_bases", ["entry_id"]
    )
    op.drop_column("collaboration_bases", "source_anchor")
    op.drop_column("collaboration_bases", "source_key")
