"""Private comment pictures and references in immutable message revisions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "202611060001"
down_revision = "202611050003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "discussion_pictures",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scope_id",
            UUID(as_uuid=True),
            sa.ForeignKey("discussion_scopes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("source_size", sa.Integer(), nullable=False),
        sa.Column("source_mime", sa.String(32), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("renditions", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('staged', 'verified', 'attached', 'expired')",
            name="ck_discussion_picture_state",
        ),
        sa.CheckConstraint(
            "source_size > 0 AND source_size <= 10485760", name="ck_discussion_picture_size"
        ),
    )
    op.create_index(
        "ix_discussion_pictures_scope_owner",
        "discussion_pictures",
        ["scope_id", "owner_id", "state"],
    )
    op.create_index("ix_discussion_pictures_expiry", "discussion_pictures", ["state", "expires_at"])
    op.create_table(
        "discussion_picture_references",
        sa.Column(
            "picture_id",
            UUID(as_uuid=True),
            sa.ForeignKey("discussion_pictures.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("message_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.ForeignKeyConstraint(
            ["message_id", "revision"],
            ["discussion_message_revisions.message_id", "discussion_message_revisions.revision"],
            ondelete="CASCADE",
        ),
    )


def downgrade():
    op.drop_table("discussion_picture_references")
    op.drop_table("discussion_pictures")
