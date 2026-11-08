"""Restore-lineage checks; callers must hold the current share's row lock.

No automatic rotation: explicit offline maintenance rotates this value before
admitting clients. Ordinary permission/cursor resets do not change it. See
collaboration_restore_admin and the restore maintenance command.
"""

import uuid

from fastapi import HTTPException

from app.db.models import Share


def require_generation(share: Share, expected: uuid.UUID | None) -> None:
    if share.collaboration_restore_generation != expected:
        raise HTTPException(409, {"code": "restore_generation_changed"})
