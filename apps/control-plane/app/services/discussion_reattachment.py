"""Explicit target changes retain the thread's scope and read dependencies.

The discussion service holds actor/session/share/entry locks and authorizes the
old thread before calling this module. No network I/O occurs here.
"""

from pydantic import TypeAdapter, ValidationError

from app.core.config import get_settings
from app.db import models as m
from app.schemas import discussions as s
from app.services import base_discussions

_anchor = TypeAdapter(s.Anchor)


def enabled():
    settings = get_settings()
    return bool(
        settings.collaboration_policy_enabled
        and settings.collaboration_discussions_enabled
        and settings.collaboration_reattach_enabled
    )


def stored(payload):
    try:
        return _anchor.validate_python({k: v for k, v in payload.items() if k != "entry_id"})
    except (ValidationError, AttributeError, TypeError):
        base_discussions.error(500, "invalid_stored_anchor")


def read_dependencies(db, target):
    """Compare server-owned file dependencies, not current membership snapshots.

    The caller must validate both anchors first. Equal dependency sets preserve
    the read intersection even after later membership or file-rule changes.
    """
    if isinstance(target, s.BaseItemTarget):
        return frozenset([str(target.record_id)])
    group = (
        target
        if isinstance(target, s.BaseGroupTarget)
        else target.group
        if isinstance(target, s.BaseGroupSummaryTarget)
        else getattr(target, "lane", None)
    )
    if group is not None:
        witness = db.get(m.BaseGroupWitness, group.witness_id)
        if witness is None:
            base_discussions.error()
        return frozenset(row["record_id"] for row in witness.evidence["references"])
    return frozenset()


def validate(db, ctx, thread, command):
    if not enabled():
        base_discussions.error(409, "reattach_disabled")
    if thread.anchor_revision != command.expected_anchor_revision:
        base_discussions.error(409, "stale_anchor_revision")
    row = db.get(m.DiscussionAnchorRevision, (thread.id, thread.anchor_revision))
    if not row:
        base_discussions.error(500, "invalid_stored_anchor")
    old = stored(row.payload)
    new = command.anchor
    if isinstance(old, s.BaseAnchor):
        if not isinstance(new, s.BaseAnchor) or (
            old.definition_id,
            old.view_id,
            old.generation,
        ) != (new.definition_id, new.view_id, new.generation):
            base_discussions.error(409, "reattach_scope_changed")
        # Old anchor access was checked by thread_for; repeat explicitly so
        # this helper cannot accidentally become an authorization shortcut.
        base_discussions.validate(db, ctx, old, write=True)
        base_discussions.validate(db, ctx, new, creation=True, write=True)
        if read_dependencies(db, old.target) != read_dependencies(db, new.target):
            base_discussions.error(409, "reattach_readers_changed")
    elif isinstance(new, s.BaseAnchor):
        base_discussions.error(409, "reattach_scope_changed")
    # validate_anchor_target additionally enforces the note/image/base family
    # and exact image-file identity. No cross-entry move is performed here.
    return old


def append(db, ctx, thread, command):
    payload = {**command.anchor.model_dump(mode="json"), "entry_id": ctx.entry.id}
    current = db.get(m.DiscussionAnchorRevision, (thread.id, thread.anchor_revision))
    if current.payload == payload:
        return False
    thread.anchor_revision += 1
    db.add(
        m.DiscussionAnchorRevision(
            thread_id=thread.id,
            revision=thread.anchor_revision,
            actor_id=ctx.user.id,
            payload=payload,
        )
    )
    return True
