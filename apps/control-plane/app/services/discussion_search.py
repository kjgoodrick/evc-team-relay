"""Bounded share search with current scope and target authorization.

Read locks match normal discussion reads. SQL filters file permissions first;
the exact Bases target validator then supplies the authorized thread set used
by the text query. No query text is evaluated against unauthorized targets.
"""

import hashlib
import json

from cryptography.fernet import InvalidToken
from sqlalchemy import and_, case, func, literal, or_, select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.api import deps
from app.core.config import get_settings
from app.db import models as m
from app.schemas.discussion_search import DiscussionSearch
from app.services import base_discussions, collaboration_auth, managed_service
from app.services import discussion_service as discussions
from app.services.discussion_search_projection_v1 import words

SCAN_LIMIT = 100


def enabled():
    settings = get_settings()
    return (
        settings.collaboration_policy_enabled
        and settings.collaboration_discussions_enabled
        and settings.collaboration_search_enabled
    )


def read_predicate(share, user, rules, path):
    """The managed path-rule precedence in SQL, including explicit denies.

    Prefix equality deliberately uses substr, not LIKE: SQLite LIKE otherwise
    folds ASCII case, and wildcard characters in paths are literal here.
    """
    if user.is_admin or share.owner_user_id == user.id:
        return literal(True)
    ordered = sorted(rules, key=lambda rule: (len(rule.path), not rule.recursive), reverse=True)
    if not ordered:
        return literal(False)
    clauses = []
    for rule in ordered:
        match = path == rule.path
        if rule.recursive:
            match = (
                literal(True)
                if rule.path == ""
                else or_(match, func.substr(path, 1, len(rule.path) + 1) == rule.path + "/")
            )
        clauses.append((match, rule.permission))
    return case(*clauses, else_="none").in_(["read", "write"])


def snippet(value, terms):
    # Return plain text only. The client must render textContent, never HTML.
    lower = value.casefold()
    offsets = [lower.find(term) for term in terms if term in lower]
    start = max(0, min(offsets) - 80) if offsets else 0
    excerpt = value[start : start + 476]
    return ("…" if start else "") + excerpt + ("…" if start + 476 < len(value) else "")


def search(db, token, share_id, request: DiscussionSearch):
    discussions.require_clean_session(db)
    try:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            db.execute(text("SET LOCAL statement_timeout = '10s'"))
        return _search(db, token, share_id, request)
    except OperationalError:
        db.rollback()
        discussions.error(503, "retryable_transaction")
    except SQLAlchemyError:
        # SQL exceptions can contain bound private query text. Return only a
        # stable diagnostic, as for comment commands.
        db.rollback()
        discussions.error(500, "discussion_storage_error")
    except Exception:
        db.rollback()
        raise


def _search(db, token, share_id, request: DiscussionSearch):
    if not enabled():
        discussions.error(404, "discussion_search_disabled")
    discussions.require_clean_session(db)
    db.expire_all()
    user = collaboration_auth.lock_actor(db, token, require_session=True)
    share = db.scalar(select(m.Share).where(m.Share.id == share_id).with_for_update(read=True))
    if not share or not share.managed:
        discussions.error(404, "target_unavailable")
    moderate = user.is_admin or share.owner_user_id == user.id
    if (
        not moderate
        and db.scalar(
            select(m.ShareMember.id)
            .where(m.ShareMember.share_id == share.id, m.ShareMember.user_id == user.id)
            .with_for_update(read=True)
        )
        is None
    ):
        discussions.error(404, "target_unavailable")
    deps._get_user_from_token(db, token)
    rules = managed_service.rules_for(db, share, user)
    terms = words(request.query)
    if len(terms) > 32 or request.query and not terms:
        discussions.error(422, "invalid_search_query")
    filters = {**request.model_dump(mode="json", exclude={"cursor", "query"}), "words": terms}
    fingerprint = hashlib.sha256(
        json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    identity = [
        "discussion-search-v1",
        str(user.id),
        str(share.id),
        str(share.collaboration_epoch),
        fingerprint,
    ]
    before, ceiling = None, share.collaboration_sequence
    if request.cursor:
        try:
            saved = json.loads(
                discussions.cursor_codec().decrypt(request.cursor.encode(), ttl=3600)
            )
            if (
                not isinstance(saved, list)
                or len(saved) != 7
                or saved[:5] != identity
                or any(type(value) is not int or value < 1 for value in saved[5:])
                or saved[5] > saved[6]
            ):
                raise ValueError
            before, ceiling = saved[5:]
        except (InvalidToken, ValueError, TypeError, UnicodeError):
            discussions.error(409, "discussion_search_cursor_reset")

    # Selecting a specific scope has the same rejection semantics as opening it.
    if request.entry_id is not None:
        entry = db.scalar(
            select(m.ManagedEntry).where(
                m.ManagedEntry.id == str(request.entry_id),
                m.ManagedEntry.share_id == share.id,
                m.ManagedEntry.deleted.is_(False),
            )
        )
        if (
            not entry
            or entry.record.get("type") not in {"markdown", "image", "file"}
            or managed_service.permission(share, user, entry.path, rules) not in {"read", "write"}
        ):
            discussions.error(404, "target_unavailable")
        discussions.requested_view(
            db, discussions.Context(user, share, entry, moderate), request.view_id
        )

    query = (
        select(m.DiscussionThread, m.DiscussionScope, m.ManagedEntry, m.DiscussionAnchorRevision)
        .join(m.DiscussionScope, m.DiscussionScope.id == m.DiscussionThread.scope_id)
        .join(
            m.ManagedEntry,
            and_(
                m.ManagedEntry.id == m.DiscussionScope.entry_id, m.ManagedEntry.share_id == share.id
            ),
        )
        .join(
            m.DiscussionAnchorRevision,
            and_(
                m.DiscussionAnchorRevision.thread_id == m.DiscussionThread.id,
                m.DiscussionAnchorRevision.revision == m.DiscussionThread.anchor_revision,
            ),
        )
        .where(
            m.DiscussionScope.share_id == share.id,
            m.ManagedEntry.deleted.is_(False),
            m.DiscussionThread.deleted.is_(False),
            m.DiscussionThread.created_sequence <= ceiling,
            read_predicate(share, user, rules, m.ManagedEntry.path),
        )
    )
    if request.entry_id is not None:
        query = query.where(m.ManagedEntry.id == str(request.entry_id))
    if request.view_id is not None:
        query = query.where(
            m.DiscussionAnchorRevision.payload["view_id"].as_string() == str(request.view_id)
        )
    if before is not None:
        query = query.where(m.DiscussionThread.created_sequence < before)
    candidates = list(
        db.execute(query.order_by(m.DiscussionThread.created_sequence.desc()).limit(SCAN_LIMIT + 1))
    )
    allowed = {}
    for thread, scope, entry, anchor in candidates[:SCAN_LIMIT]:
        if entry.record.get("type") not in {
            "markdown",
            "image",
            "file",
        } or scope.kind != discussions.scope_kind(
            discussions.Context(user, share, entry, moderate)
        ):
            continue
        ctx = discussions.Context(user, share, entry, moderate)
        if base_discussions.can_read_thread(db, ctx, thread):
            allowed[thread.id] = (thread, scope, entry, anchor)

    items = []
    if allowed:
        matches = (
            select(
                m.DiscussionMessage.id.label("message_id"),
                func.row_number()
                .over(
                    partition_by=m.DiscussionMessage.thread_id,
                    order_by=m.DiscussionMessage.created_sequence.desc(),
                )
                .label("rank"),
            )
            .join(
                m.DiscussionSearchDocument,
                m.DiscussionSearchDocument.message_id == m.DiscussionMessage.id,
            )
            .where(
                m.DiscussionMessage.thread_id.in_(allowed),
                m.DiscussionMessage.deleted.is_(False),
                m.DiscussionSearchDocument.revision == m.DiscussionMessage.revision,
            )
        )
        if request.mine:
            matches = matches.where(m.DiscussionMessage.author_id == user.id)
        if request.mentions:
            matches = matches.where(
                m.DiscussionMessage.id.in_(
                    select(m.DiscussionSearchMention.message_id).where(
                        m.DiscussionSearchMention.user_id == user.id
                    )
                )
            )
        if terms:
            if db.get_bind().dialect.name == "postgresql":
                matches = matches.where(
                    func.to_tsvector(
                        text("'simple'::regconfig"), m.DiscussionSearchDocument.terms
                    ).op("@@")(func.plainto_tsquery(text("'simple'::regconfig"), " ".join(terms)))
                )
            else:
                for term in terms:
                    matches = matches.where(
                        func.instr(
                            literal(" ") + m.DiscussionSearchDocument.terms + literal(" "),
                            " " + term + " ",
                        )
                        > 0
                    )
        selected = matches.subquery()
        result = (
            select(m.DiscussionMessage, m.DiscussionSearchDocument.plain_text)
            .join(
                selected,
                and_(selected.c.message_id == m.DiscussionMessage.id, selected.c.rank == 1),
            )
            .join(
                m.DiscussionSearchDocument,
                m.DiscussionSearchDocument.message_id == m.DiscussionMessage.id,
            )
            .join(m.DiscussionThread, m.DiscussionThread.id == m.DiscussionMessage.thread_id)
        )
        if request.state != "all":
            result = result.where(m.DiscussionThread.state == request.state)
        for message, plain_text in db.execute(
            result.order_by(m.DiscussionThread.created_sequence.desc()).limit(request.limit + 1)
        ):
            thread, scope, entry, anchor = allowed[message.thread_id]
            items.append(
                {
                    "thread": discussions.thread_data(thread),
                    "entry_id": entry.id,
                    "kind": scope.kind,
                    "path": entry.path,
                    "view_id": anchor.payload["view_id"]
                    if anchor.payload.get("type") == "base"
                    else None,
                    "message_id": str(message.id),
                    "message_revision": message.revision,
                    "message_sequence": message.created_sequence,
                    "excerpt": snippet(plain_text, terms),
                }
            )

    more_matches = len(items) > request.limit
    page = items[: request.limit]
    through = (
        page[-1]["thread"]["created_sequence"]
        if more_matches
        else candidates[min(len(candidates), SCAN_LIMIT) - 1][0].created_sequence
        if candidates
        else None
    )
    cursor = (
        discussions.cursor_codec()
        .encrypt(json.dumps([*identity, through, ceiling]).encode())
        .decode()
        if through is not None and (more_matches or len(candidates) > SCAN_LIMIT)
        else None
    )
    # The cursor is an opaque bounded-scan continuation, not a count or promise
    # of another hit. Every page rechecks current permissions and target lineage.
    response = {"protocol_version": 1, "items": page, "next_cursor": cursor}
    db.commit()
    return response
