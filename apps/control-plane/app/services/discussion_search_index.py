"""Maintain search in the same transaction as the authoritative message."""

from sqlalchemy import delete, select

from app.db import models as m
from app.services.discussion_search_projection_v1 import project


def update(db, message, body):
    db.execute(
        delete(m.DiscussionSearchMention).where(m.DiscussionSearchMention.message_id == message.id)
    )
    document = db.get(m.DiscussionSearchDocument, message.id)
    if message.deleted:
        if document is not None:
            db.delete(document)
        return
    plain_text, terms, mentioned = project(body)
    if document is None:
        document = m.DiscussionSearchDocument(message_id=message.id)
        db.add(document)
    document.revision, document.plain_text, document.terms = message.revision, plain_text, terms
    db.flush()
    existing = (
        db.scalars(select(m.User.id).where(m.User.id.in_(mentioned))).all() if mentioned else []
    )
    db.add_all(
        m.DiscussionSearchMention(message_id=message.id, user_id=user_id) for user_id in existing
    )
