"""Durable notification projection and per-recipient discussion preferences."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "202611060007"
down_revision = "202611060006"
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
        "discussion_subscriptions",
        ref("user_id", "users", primary=True),
        ref("thread_id", "discussion_threads", primary=True),
        sa.Column("following", sa.Boolean(), nullable=False),
        sa.Column("muted", sa.Boolean(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_discussion_subscription_version"),
    )
    op.create_index("ix_discussion_subscriptions_thread", "discussion_subscriptions", ["thread_id"])
    op.create_table(
        "discussion_inbox_items",
        ref("user_id", "users", primary=True),
        ref("thread_id", "discussion_threads", primary=True),
        ref("share_id", "shares"),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("read_version", sa.BigInteger(), nullable=False),
        sa.Column("dismissed_version", sa.BigInteger(), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("activity_at", sa.DateTime(timezone=True), nullable=False),
        ref("message_id", "discussion_messages"),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.CheckConstraint(
            "version >= 1 AND read_version >= 0 AND read_version <= version "
            "AND dismissed_version >= 0 AND dismissed_version <= version",
            name="ck_discussion_inbox_versions",
        ),
        sa.CheckConstraint("reason IN ('mention', 'reply')", name="ck_discussion_inbox_reason"),
    )
    op.create_index(
        "ix_discussion_inbox_user_share_activity",
        "discussion_inbox_items",
        ["user_id", "share_id", "last_sequence"],
    )
    op.create_table(
        "discussion_notifications",
        ref("share_id", "shares", primary=True),
        sa.Column("sequence", sa.BigInteger(), primary_key=True),
        ref("user_id", "users", primary=True),
        ref("thread_id", "discussion_threads"),
        ref("message_id", "discussion_messages"),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("lease_token", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered", sa.Boolean(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "reason IN ('mention', 'reply')", name="ck_discussion_notification_reason"
        ),
    )
    op.create_index(
        "ix_discussion_notifications_pending",
        "discussion_notifications",
        ["completed_at", "lease_until"],
    )
    op.create_index(
        "ix_discussion_notifications_thread_user",
        "discussion_notifications",
        ["thread_id", "user_id", "sequence"],
    )


def downgrade():
    tables = ("discussion_notifications", "discussion_inbox_items", "discussion_subscriptions")
    if any(op.get_bind().scalar(sa.text("SELECT count(*) FROM " + table)) for table in tables):
        raise RuntimeError(
            "Export/migrate inbox, notifications and subscriptions before downgrading"
        )
    for table in tables:
        op.drop_table(table)
