"""Persist the boundary before which discussion replay is no longer complete."""

import sqlalchemy as sa
from alembic import op

revision = "202611080002"
down_revision = "202611080001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "shares",
        sa.Column(
            "discussion_event_floor", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_shares_discussion_event_floor",
        "shares",
        "discussion_event_floor >= 0 AND discussion_event_floor <= collaboration_sequence",
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM shares WHERE discussion_event_floor > 0)")
    ):
        raise RuntimeError("Cannot remove discussion replay boundaries after event retention")
    op.drop_constraint("ck_shares_discussion_event_floor", "shares", type_="check")
    op.drop_column("shares", "discussion_event_floor")
