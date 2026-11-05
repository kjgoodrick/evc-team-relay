"""Whole-note discussion preview, immutable history, receipts and committed events."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "202611050002"
down_revision = "202611050001"
branch_labels = None
depends_on = None


def uid(name, target=None, *, primary=False, nullable=False, ondelete="CASCADE"):
    args = [UUID(as_uuid=True)]
    if target:
        args.append(sa.ForeignKey(target, ondelete=ondelete))
    return sa.Column(name, *args, primary_key=primary, nullable=nullable)


def actor(name="actor_id"):
    return uid(name, "users.id", nullable=True, ondelete="SET NULL")


def created():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)


def upgrade():
    op.add_column(
        "shares",
        sa.Column("collaboration_sequence", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column("shares", uid("collaboration_epoch", nullable=True))
    op.create_table(
        "discussion_scopes",
        uid("id", primary=True),
        uid("share_id", "shares.id"),
        sa.Column(
            "entry_id",
            sa.String(36),
            sa.ForeignKey("managed_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.UniqueConstraint("share_id", "entry_id", name="uq_discussion_scope_entry"),
    )
    op.create_table(
        "discussion_threads",
        uid("id", primary=True),
        uid("scope_id", "discussion_scopes.id"),
        actor("author_id"),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("state_revision", sa.Integer(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("removal_reason", sa.String(512), nullable=True),
        sa.Column("anchor_revision", sa.Integer(), nullable=False),
        sa.Column("created_sequence", sa.BigInteger(), nullable=False),
        sa.Column("activity_sequence", sa.BigInteger(), nullable=False),
        created(),
        sa.CheckConstraint("state IN ('open', 'resolved')", name="ck_discussion_thread_state"),
        sa.CheckConstraint("state_revision >= 1", name="ck_discussion_state_revision"),
    )
    op.create_index(
        "ix_discussion_threads_scope_created",
        "discussion_threads",
        ["scope_id", "created_sequence"],
    )
    op.create_table(
        "discussion_anchor_revisions",
        uid("thread_id", "discussion_threads.id", primary=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        actor(),
        created(),
    )
    op.create_table(
        "discussion_messages",
        uid("id", primary=True),
        uid("thread_id", "discussion_threads.id"),
        actor("author_id"),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("created_sequence", sa.BigInteger(), nullable=False),
        created(),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("revision >= 1", name="ck_discussion_message_revision"),
    )
    op.create_index(
        "ix_discussion_messages_thread_created",
        "discussion_messages",
        ["thread_id", "created_sequence"],
    )
    op.create_table(
        "discussion_message_revisions",
        uid("message_id", "discussion_messages.id", primary=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("body", sa.JSON(), nullable=True),
        actor(),
        sa.Column("reason", sa.String(512), nullable=True),
        created(),
    )
    op.create_table(
        "discussion_reactions",
        uid("message_id", "discussion_messages.id", primary=True),
        uid("user_id", "users.id", primary=True),
        sa.Column("emoji", sa.String(32), primary_key=True),
    )
    op.create_table(
        "discussion_receipts",
        uid("actor_id", primary=True),
        uid("operation_id", primary=True),
        uid("scope_id", "discussion_scopes.id"),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
    )
    op.create_table(
        "discussion_events",
        uid("share_id", "shares.id", primary=True),
        sa.Column("sequence", sa.BigInteger(), primary_key=True),
        uid("scope_id", "discussion_scopes.id"),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        created(),
    )
    op.create_index(
        "ix_discussion_events_scope_sequence", "discussion_events", ["scope_id", "sequence"]
    )


def downgrade():
    for table in (
        "discussion_events",
        "discussion_receipts",
        "discussion_reactions",
        "discussion_message_revisions",
        "discussion_messages",
        "discussion_anchor_revisions",
        "discussion_threads",
        "discussion_scopes",
    ):
        op.drop_table(table)
    op.drop_column("shares", "collaboration_epoch")
    op.drop_column("shares", "collaboration_sequence")
