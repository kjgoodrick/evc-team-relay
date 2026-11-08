"""Index retained picture references by message for bounded content cleanup."""

from alembic import op

revision = "202611070004"
down_revision = "202611070003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_discussion_picture_references_message",
        "discussion_picture_references",
        ["message_id", "revision"],
    )


def downgrade():
    op.drop_index("ix_discussion_picture_references_message", "discussion_picture_references")
