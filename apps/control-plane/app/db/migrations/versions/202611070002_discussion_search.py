"""Current-message search projection, mentions and PostgreSQL full-text index."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

from app.services.discussion_search_projection_v1 import project

revision = "202611070002"
down_revision = "202611070001"
branch_labels = None
depends_on = None


def upgrade():
    documents = op.create_table(
        "discussion_search_documents",
        sa.Column(
            "message_id",
            UUID(as_uuid=True),
            sa.ForeignKey("discussion_messages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("plain_text", sa.Text(), nullable=False),
        sa.Column("terms", sa.Text(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_discussion_search_revision"),
    )
    mentions = op.create_table(
        "discussion_search_mentions",
        sa.Column(
            "message_id",
            UUID(as_uuid=True),
            sa.ForeignKey("discussion_search_documents.message_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_index(
        "ix_discussion_search_mentions_user",
        "discussion_search_mentions",
        ["user_id", "message_id"],
    )
    # Only current nondeleted bodies enter the projection. Bounded keyset
    # batches keep upgrade memory independent of history size.
    connection = op.get_bind()
    messages = sa.table(
        "discussion_messages",
        sa.column("id", UUID(as_uuid=True)),
        sa.column("revision", sa.Integer()),
        sa.column("deleted", sa.Boolean()),
    )
    revisions = sa.table(
        "discussion_message_revisions",
        sa.column("message_id", UUID(as_uuid=True)),
        sa.column("revision", sa.Integer()),
        sa.column("body", sa.JSON()),
    )
    users = sa.table("users", sa.column("id", UUID(as_uuid=True)))
    after = None
    while True:
        query = (
            sa.select(messages.c.id, messages.c.revision, revisions.c.body)
            .join(
                revisions,
                sa.and_(
                    revisions.c.message_id == messages.c.id,
                    revisions.c.revision == messages.c.revision,
                ),
            )
            .where(messages.c.deleted.is_(False))
            .order_by(messages.c.id)
            .limit(250)
        )
        if after is not None:
            query = query.where(messages.c.id > after)
        rows = connection.execute(query).all()
        if not rows:
            break
        for row in rows:
            plain_text, terms, mentioned = project(row.body)
            connection.execute(
                documents.insert().values(
                    message_id=row.id, revision=row.revision, plain_text=plain_text, terms=terms
                )
            )
            # Accounts can have been deleted after an old mention was saved.
            present = (
                connection.scalars(sa.select(users.c.id).where(users.c.id.in_(mentioned))).all()
                if mentioned
                else []
            )
            if present:
                connection.execute(
                    mentions.insert(),
                    [{"message_id": row.id, "user_id": user_id} for user_id in present],
                )
        after = rows[-1].id
    if connection.dialect.name == "postgresql":
        op.create_index(
            "ix_discussion_search_terms",
            "discussion_search_documents",
            [sa.text("to_tsvector('simple'::regconfig, terms)")],
            postgresql_using="gin",
        )


def downgrade():
    # This is a derived index, not message/history data. Re-upgrade rebuilds it.
    op.drop_table("discussion_search_mentions")
    op.drop_table("discussion_search_documents")
