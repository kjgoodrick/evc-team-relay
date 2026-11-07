"""Stable shared timeline markers, view placements and immutable capture revisions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "202611070001"
down_revision = "202611060007"
branch_labels = None
depends_on = None


def ref(name, table, *, primary=False):
    return sa.Column(
        name,
        UUID(as_uuid=True),
        sa.ForeignKey(table + ".id", ondelete="CASCADE"),
        primary_key=primary,
        nullable=False,
    )


def upgrade():
    op.create_table(
        "collaboration_timeline_marker_catalogs",
        ref("definition_id", "collaboration_bases", primary=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "collaboration_timeline_markers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        ref("definition_id", "collaboration_bases"),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_timeline_marker_definition",
        "collaboration_timeline_markers",
        ["definition_id", "active", "id"],
    )
    op.create_table(
        "collaboration_timeline_marker_placements",
        ref("marker_id", "collaboration_timeline_markers", primary=True),
        ref("view_id", "collaboration_base_views", primary=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        ref("axis_definition_id", "collaboration_base_target_definitions"),
        sa.Column("axis_definition_revision", sa.BigInteger(), nullable=False),
        sa.Column("axis", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_timeline_marker_view",
        "collaboration_timeline_marker_placements",
        ["view_id", "active", "marker_id"],
    )
    op.create_table(
        "collaboration_timeline_marker_revisions",
        ref("marker_id", "collaboration_timeline_markers", primary=True),
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column(
            "actor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "collaboration_timeline_marker_receipts",
        sa.Column("actor_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("operation_id", UUID(as_uuid=True), primary_key=True),
        ref("definition_id", "collaboration_bases"),
        sa.Column("generation", UUID(as_uuid=True), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
    )


def downgrade():
    tables = (
        "collaboration_timeline_marker_receipts",
        "collaboration_timeline_marker_revisions",
        "collaboration_timeline_marker_placements",
        "collaboration_timeline_markers",
        "collaboration_timeline_marker_catalogs",
    )
    if any(op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}")) for table in tables):
        raise RuntimeError("Export/migrate shared timeline markers before downgrading")
    for table in tables:
        op.drop_table(table)
