import uuid
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.db import models as m
from app.services import collaboration_restore_admin as admin

VERSIONS = ("202611080001",)


@pytest.fixture
def shares(db_session):
    owner = m.User(email="owner@offline-restore.test", password_hash="", is_active=True)
    db_session.add(owner)
    db_session.flush()
    values = [
        m.Share(
            kind=m.ShareKind.FOLDER if index == 0 else m.ShareKind.DOC,
            path=f"Fixture-{index}",
            owner_user_id=owner.id,
            visibility=m.ShareVisibility.PRIVATE,
            managed=index == 0,
            collaboration_epoch=uuid.uuid4(),
            collaboration_sequence=index + 10,
            collaboration_restore_generation=uuid.uuid4() if index else None,
        )
        for index in range(2)
    ]
    db_session.add_all(values)
    db_session.commit()
    return values


def plan(db):
    return admin.prepare(db, "a" * 64, "b" * 64, "Disposable restore drill", VERSIONS)


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_rotation_covers_original_and_previously_restored_shares_without_resetting_content(
    db_session, shares
):
    selected = plan(db_session)
    before = [
        (share.id, share.path, share.collaboration_sequence, share.owner_user_id)
        for share in shares
    ]
    result = admin.apply(db_session, selected, VERSIONS)
    db_session.commit()
    db_session.expire_all()
    assert result["state"] == "applied" and result["share_count"] == 2
    assert before == [
        (share.id, share.path, share.collaboration_sequence, share.owner_user_id)
        for share in shares
    ]
    for share in shares:
        assert share.collaboration_restore_generation == selected.generation
        assert share.collaboration_epoch == admin.new_epoch(selected, share.id)
    receipt = db_session.get(m.CollaborationRestore, selected.generation)
    assert receipt.plan_sha256 == admin.checksum(selected.model_dump(mode="json"))
    assert receipt.snapshot_sha256 == "a" * 64 and receipt.target_sha256 == "b" * 64
    assert receipt.schema_versions == list(VERSIONS)
    assert receipt.operator == "Disposable restore drill"
    assert count(db_session, m.AuditLog) == 2
    assert all(
        log.details["operation"] == "collaboration_restore"
        for log in db_session.scalars(select(m.AuditLog))
    )


def test_lost_response_retry_does_not_rotate_again_or_duplicate_audit(db_session, shares):
    selected = plan(db_session)
    admin.apply(db_session, selected, VERSIONS)
    db_session.commit()
    current = admin.inventory(db_session)
    assert admin.apply(db_session, selected, VERSIONS)["state"] == "already-applied"
    db_session.commit()
    assert admin.inventory(db_session) == current
    assert count(db_session, m.CollaborationRestore) == 1
    assert count(db_session, m.AuditLog) == 2


def test_late_retry_after_another_restore_never_revives_earlier_generation(db_session, shares):
    first = plan(db_session)
    admin.apply(db_session, first, VERSIONS)
    db_session.commit()
    second = plan(db_session)
    admin.apply(db_session, second, VERSIONS)
    db_session.commit()
    current = admin.inventory(db_session)
    assert admin.apply(db_session, first, VERSIONS)["state"] == "superseded"
    db_session.commit()
    assert admin.inventory(db_session) == current
    assert count(db_session, m.CollaborationRestore) == 2
    assert count(db_session, m.AuditLog) == 4


@pytest.mark.parametrize(
    "change", ["generation", "epoch", "sequence", "new-share", "deleted-share", "schema"]
)
def test_stale_plan_cannot_partially_rotate_any_share(db_session, shares, change):
    selected = plan(db_session)
    if change == "generation":
        shares[0].collaboration_restore_generation = uuid.uuid4()
    elif change == "epoch":
        shares[0].collaboration_epoch = uuid.uuid4()
    elif change == "sequence":
        shares[0].collaboration_sequence += 1
    elif change == "new-share":
        db_session.add(
            m.Share(
                kind=m.ShareKind.DOC,
                path="Added",
                owner_user_id=shares[0].owner_user_id,
                visibility=m.ShareVisibility.PRIVATE,
            )
        )
    elif change == "deleted-share":
        db_session.delete(shares[0])
    db_session.commit()
    current = admin.inventory(db_session)
    with pytest.raises(admin.RestoreError):
        admin.apply(db_session, selected, ("different",) if change == "schema" else VERSIONS)
    assert admin.inventory(db_session) == current
    assert count(db_session, m.CollaborationRestore) == 0
    assert count(db_session, m.AuditLog) == 0


def test_failure_rolls_back_generations_and_audit_with_receipt(db_session, shares):
    selected = plan(db_session)
    before = admin.inventory(db_session)
    with pytest.raises(RuntimeError, match="Synthetic commit failure"):
        with db_session.begin_nested():
            with patch.object(
                db_session, "flush", side_effect=RuntimeError("Synthetic commit failure")
            ):
                admin.apply(db_session, selected, VERSIONS)
    db_session.expire_all()
    assert admin.inventory(db_session) == before
    assert count(db_session, m.CollaborationRestore) == 0
    assert count(db_session, m.AuditLog) == 0


def test_receipt_identity_rejects_altered_operator_or_backup(db_session, shares):
    selected = plan(db_session)
    admin.apply(db_session, selected, VERSIONS)
    db_session.commit()
    for key, value in (
        ("operator", "Different operator"),
        ("snapshot_sha256", "c" * 64),
        ("target_sha256", "d" * 64),
    ):
        with pytest.raises(admin.RestoreError, match="different plan"):
            admin.apply(db_session, selected.model_copy(update={key: value}), VERSIONS)
    assert count(db_session, m.CollaborationRestore) == 1


def test_empty_inventory_still_has_a_durable_idempotent_receipt(db_session):
    selected = plan(db_session)
    assert selected.shares == ()
    assert admin.apply(db_session, selected, VERSIONS)["share_count"] == 0
    db_session.commit()
    assert admin.apply(db_session, selected, VERSIONS)["state"] == "already-applied"
    assert count(db_session, m.CollaborationRestore) == 1


def test_inconsistent_receipt_is_not_reported_as_success(db_session, shares):
    selected = plan(db_session)
    admin.apply(db_session, selected, VERSIONS)
    db_session.commit()
    receipt = db_session.get(m.CollaborationRestore, selected.generation)
    receipt.share_count += 1
    db_session.commit()
    current = admin.inventory(db_session)
    with pytest.raises(admin.RestoreError, match="inconsistent"):
        admin.apply(db_session, selected, VERSIONS)
    assert admin.inventory(db_session) == current


def test_pending_changes_are_not_committed_as_part_of_rotation(db_session, shares):
    selected = plan(db_session)
    shares[0].path = "Unreviewed change"
    with pytest.raises(admin.RestoreError, match="pending"):
        admin.apply(db_session, selected, VERSIONS)
    assert count(db_session, m.CollaborationRestore) == 0


@pytest.mark.parametrize(
    "change",
    ["duplicate", "reordered", "extra", "invalid-generation", "operator", "digest", "sequence"],
)
def test_plan_parser_rejects_ambiguous_or_invalid_input(db_session, shares, change):
    raw = plan(db_session).model_dump(mode="json")
    if change == "duplicate":
        raw["shares"].append(raw["shares"][0])
    elif change == "reordered":
        raw["shares"].reverse()
    elif change == "extra":
        raw["force"] = True
    elif change == "invalid-generation":
        raw["generation"] = str(uuid.uuid5(uuid.NAMESPACE_DNS, "reuse"))
    elif change == "operator":
        raw["operator"] = "line\nbreak"
    elif change == "digest":
        raw["snapshot_sha256"] = "../not-a-digest"
    else:
        raw["shares"][0]["sequence"] = True
    with pytest.raises(ValidationError):
        admin.RestorePlan.model_validate(raw)
