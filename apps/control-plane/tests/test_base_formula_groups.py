"""Formula groups use the same immutable witness and revocation rules as notes."""

import uuid

import pytest
import test_base_groups as checks
from test_base_groups import base, capture, counts, groups, request, targets, thread, world
from test_base_target_definitions import catalog, configure
from test_discussions import headers, send

from app.core.config import get_settings
from app.db import models as m

world, base, targets, groups = world, base, targets, groups


@pytest.fixture
def formula_groups(client, groups, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_bases_formula_groups_enabled", True)
    configure(
        client,
        groups,
        formulas={"group": "formula.input", "input": "status"},
        groupBy={"property": "formula.group"},
    )
    return groups


def test_capture_derives_transitive_calculation_and_discovery_without_source_output(
    client, db_session, formula_groups
):
    t = formula_groups
    configure(
        client,
        t,
        formulas={"group": "if(flag, formula.sum, 0)", "sum": "a * 2 + b"},
        groupBy={"property": "formula.group"},
    )
    t.source.source = "---\na: 3\nb: 1\nflag: true\n---\nPRIVATE source body"
    page = catalog(client, t)
    grouping = next(item for item in page["targets"] if item["kind"] == "group")
    assert page["formula_group_ids"] == [grouping["id"]]
    assert page["computed_values"] is False
    assert "a * 2" not in str(page)
    payload = request(client, t, value={"type": "number", "value": 7})
    captured = capture(client, t, payload)
    assert capture(client, t, payload) == captured and t.source.calls == 1
    target = captured["anchor"]["target"]
    witness = db_session.get(m.BaseGroupWitness, uuid.UUID(target["witness_id"]))
    assert target["property"] == {"source": "formula", "name": "group"}
    assert witness.evidence["adapter"] == "formula_note_v1"
    assert witness.evidence["references"] == [
        {"record_id": t.record.id, "record_generation": str(t.record.collaboration_generation)}
    ]
    assert "PRIVATE" not in str(captured) + str(witness.evidence)
    send(client, t.w, thread(t, captured))


@pytest.mark.parametrize(
    "expression",
    [
        'file("Private.md").secret',
        "file.folder",
        "now()",
        "formula.group",
        "if(true, status, this.secret)",
    ],
)
def test_unsupported_closure_not_advertised_or_observed(
    client, db_session, formula_groups, expression
):
    t = formula_groups
    configure(client, t, formulas={"group": expression}, groupBy={"property": "formula.group"})
    assert catalog(client, t)["formula_group_ids"] == []
    assert "base_group_adapter_unavailable" in str(
        capture(client, t, request(client, t), expected=409)
    )
    assert t.source.calls == 0 and counts(db_session) == (0, 0, 0)


def test_wrong_computed_value_or_type_cannot_mint_a_witness(client, db_session, formula_groups):
    t = formula_groups
    configure(
        client, t, formulas={"group": "number(status) + 1"}, groupBy={"property": "formula.group"}
    )
    t.source.source = '---\nstatus: "2"\n---\n'
    for value in [{"type": "text", "value": "3"}, {"type": "number", "value": 2}]:
        assert "base_group_value_changed" in str(
            capture(client, t, request(client, t, value=value), expected=409)
        )
    t.source.source = "---\nstatus: unknown\n---\n"
    assert "base_group_value_unavailable" in str(
        capture(client, t, request(client, t), expected=409)
    )
    assert counts(db_session) == (0, 0, 0)


def test_definition_is_rechecked_after_private_io(client, db_session, formula_groups):
    t = formula_groups
    payload = request(client, t)
    t.source.before = lambda: configure(
        client, t, formulas={"group": '"Changed"'}, groupBy={"property": "formula.group"}
    )
    assert "base_target_definition_changed" in str(capture(client, t, payload, expected=409))
    assert counts(db_session) == (0, 0, 0)


def test_formula_flag_blocks_new_work_preserves_exact_receipts_and_history(
    client, formula_groups, monkeypatch
):
    t = formula_groups
    payload = request(client, t)
    captured = capture(client, t, payload)
    original = thread(t, captured)
    accepted = send(client, t.w, original)
    monkeypatch.setattr(get_settings(), "collaboration_bases_formula_groups_enabled", False)
    assert "formula_group_ids" not in catalog(client, t)
    capabilities = client.get(
        "/v1/collaboration/capabilities",
        params={"share_id": str(t.w.share.id)},
        headers=headers(t.w.tokens["reader"]),
    ).json()
    assert capabilities["features"]["discussion_base_formula_groups"] is False
    assert capture(client, t, payload) == captured
    assert send(client, t.w, original) == accepted
    checks.get(client, t, f"/threads/{accepted['thread_id']}")
    payload["operation_id"] = str(uuid.uuid4())
    assert "base_formula_groups_disabled" in str(capture(client, t, payload, expected=409))
    assert "base_formula_groups_disabled" in str(
        send(client, t.w, thread(t, captured), expected=409)
    )


def test_formula_source_comment_permission_is_not_required(client, db_session, formula_groups):
    checks.test_comment_only_capture_is_idempotent_and_requires_read_not_comment_on_source(
        client, db_session, formula_groups
    )


def test_formula_same_value_never_unlocks_another_witness(client, db_session, formula_groups):
    checks.test_matching_visible_group_never_unlocks_a_protected_threads_witness(
        client, db_session, formula_groups
    )


def test_formula_link_inputs_control_mentions_and_access(client, db_session, formula_groups):
    checks.test_link_groups_require_every_reference_for_capture_mentions_and_later_access(
        client, db_session, formula_groups
    )


@pytest.mark.parametrize("change", ["read", "delete", "generation", "source_type", "share"])
def test_formula_source_is_rechecked_after_private_io(client, db_session, formula_groups, change):
    checks.test_capture_rechecks_source_after_private_io_and_never_persists_on_failure(
        client, db_session, formula_groups, change
    )
