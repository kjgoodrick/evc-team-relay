"""Persistent removal clocks and audited moderation reads; no content purge."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "202611070003"
down_revision = "202611070002"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("discussion_threads", "discussion_messages"):
        op.add_column(name, sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(
            name, sa.Column("content_purged_at", sa.DateTime(timezone=True), nullable=True)
        )
        op.create_index("ix_" + name + "_deleted_at", name, ["deleted_at"])
    connection = op.get_bind()
    messages = sa.table(
        "discussion_messages",
        sa.column("id", UUID(as_uuid=True)),
        sa.column("revision", sa.Integer()),
        sa.column("deleted", sa.Boolean()),
        sa.column("edited_at", sa.DateTime(timezone=True)),
        sa.column("deleted_at", sa.DateTime(timezone=True)),
    )
    revisions = sa.table(
        "discussion_message_revisions",
        sa.column("message_id", UUID(as_uuid=True)),
        sa.column("revision", sa.Integer()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    removal = (
        sa.select(revisions.c.created_at)
        .where(revisions.c.message_id == messages.c.id, revisions.c.revision == messages.c.revision)
        .scalar_subquery()
    )
    connection.execute(
        messages.update()
        .where(messages.c.deleted.is_(True))
        .values(deleted_at=sa.func.coalesce(messages.c.edited_at, removal, sa.func.now()))
    )
    threads = sa.table(
        "discussion_threads",
        sa.column("id", UUID(as_uuid=True)),
        sa.column("scope_id", UUID(as_uuid=True)),
        sa.column("activity_sequence", sa.BigInteger()),
        sa.column("deleted", sa.Boolean()),
        sa.column("deleted_at", sa.DateTime(timezone=True)),
    )
    events = sa.table(
        "discussion_events",
        sa.column("scope_id", UUID(as_uuid=True)),
        sa.column("sequence", sa.BigInteger()),
        sa.column("kind", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    removal = (
        sa.select(events.c.created_at)
        .where(
            events.c.scope_id == threads.c.scope_id,
            events.c.sequence == threads.c.activity_sequence,
            events.c.kind == "thread.remove",
        )
        .scalar_subquery()
    )
    # Older partial data with no reliable clock gets a conservative window from
    # migration time. Never use creation time or purge anything during upgrade.
    connection.execute(
        threads.update()
        .where(threads.c.deleted.is_(True))
        .values(deleted_at=sa.func.coalesce(removal, sa.func.now()))
    )
    if connection.dialect.name == "postgresql":
        op.execute("ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'discussion_recovery_read'")


def downgrade():
    # Audit records and the additive enum label survive rollback. Existing code
    # must tolerate unknown audit actions before a production rollback is allowed.
    for name in ("discussion_messages", "discussion_threads"):
        op.drop_index("ix_" + name + "_deleted_at", name)
        op.drop_column(name, "content_purged_at")
        op.drop_column(name, "deleted_at")
