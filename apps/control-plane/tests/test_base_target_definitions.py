import uuid

from sqlalchemy import func, select
from test_base_discussions import command
from test_base_discussions import targets as target_fixture
from test_bases_registry import base as base_fixture
from test_bases_registry import inspect, repair, view
from test_discussions import headers, send
from test_discussions import world as discussion_world

from app.core.config import get_settings
from app.db import models as m
from app.services import base_target_definitions as service

world = discussion_world
base = base_fixture
targets = target_fixture


def catalog(client, t, *, actor="reader", expected=200, view_id=None, **payload):
    response = client.post(
        f"/v1/collaboration/shares/{t.w.share.id}/entries/{t.w.entry.id}/bases/target-definitions",
        headers=headers(t.w.tokens[actor]),
        json={"view_id": view_id or t.captured["view_id"], **payload},
    )
    assert response.status_code == expected, response.text
    return response.json()


def configure(client, t, *, title="Table", formulas=None, summaries=None, **options):
    t.base.save(
        [view(title, **options), view("Cards", "cards")],
        formulas=formulas
        if formulas is not None
        else {"double": "price * 2", "total": "formula.double + 1"},
        summaries=summaries if summaries is not None else {"Rounded": "values.mean().round(2)"},
    )
    return inspect(client, t.w)


def keyed(page):
    return {
        (item["kind"], item["property"]["source"], item["property"]["name"]): item
        for item in page["targets"]
    }


def test_catalog_reader_authority_view_isolation_and_no_evaluated_values(client, targets):
    t = targets
    snapshot = configure(
        client,
        t,
        groupBy={"property": "status", "direction": "ASC"},
        summaries={"Rounded": "values.mean().round(2)"},
    )
    first = catalog(client, t)
    assert first["registry_revision"] == snapshot["revision"]
    assert first["generation"] == t.captured["generation"]
    assert first["computed_values"] is False
    capabilities = client.get(
        "/v1/collaboration/capabilities",
        params={"share_id": str(t.w.share.id)},
        headers=headers(t.w.tokens["reader"]),
    ).json()
    assert capabilities["features"]["discussion_base_definition_catalog"] is True
    assert capabilities["discussion_base_targets"] == ["view", "item", "cell", "property"]
    assert {(item["kind"], item["property"]["name"]) for item in first["targets"]} == {
        ("formula", "double"),
        ("formula", "total"),
        ("group", "status"),
    }
    assert all(
        set(item) == {"id", "kind", "revision", "fingerprint", "property"}
        for item in first["targets"]
    )
    assert "price * 2" not in str(first)
    assert catalog(client, t, actor="reviewer") == first
    other = catalog(client, t, view_id=t.snapshot["views"][1]["id"])
    assert not {item["id"] for item in first["targets"]} & {item["id"] for item in other["targets"]}
    catalog(client, t, expected=422, dependency_ids=[t.record.id])
    catalog(client, t, expected=422, computed_value="hidden")
    catalog(client, t, view_id=str(uuid.uuid4()), expected=404)


def test_presentation_survives_but_definition_changes_and_reverts_advance_revision(client, targets):
    t = targets
    options = dict(
        groupBy={"property": "status", "direction": "ASC"},
        summaries={"status": "Unique", "formula.total": "Rounded"},
    )

    # Root summaries and view summaries use the same YAML key at different levels.
    def save(title, formulas, **extra):
        t.base.save(
            [view(title, **{**options, **extra}), view("Cards", "cards")],
            formulas=formulas,
            summaries={"Rounded": "values.mean().round(2)"},
        )
        inspect(client, t.w)

    original = {"double": "price * 2", "total": "formula.double + 1"}
    save("Table", original)
    first = keyed(catalog(client, t))
    save(
        "Table",
        original,
        order=["formula.total", "note.status"],
        groupOrder=["Done", "Todo"],
        groupBy={"property": "note.status", "direction": "DESC"},
        sort=[{"property": "file.name"}],
    )
    presentation = keyed(catalog(client, t))
    assert presentation == first
    save("Table", original)
    save("Renamed", original)
    assert keyed(catalog(client, t)) == first
    save("Renamed", {**original, "double": "price * 3"})
    edited = keyed(catalog(client, t))
    for key, before in first.items():
        assert edited[key]["id"] == before["id"]
        assert (edited[key]["revision"] > before["revision"]) == (key[0] != "group")
    save("Renamed", original)
    restored = keyed(catalog(client, t))
    for key, before in first.items():
        assert restored[key]["fingerprint"] == before["fingerprint"]
        assert (restored[key]["revision"] > edited[key]["revision"]) == (key[0] != "group")


def test_disappearance_reappearance_and_formula_rename_cannot_reuse_old_revision(
    client, db_session, targets
):
    t = targets
    configure(client, t, groupBy={"property": "note.status"})
    first = keyed(catalog(client, t))
    configure(client, t, formulas={})
    assert catalog(client, t)["targets"] == []
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(m.BaseTargetDefinition)
            .where(
                m.BaseTargetDefinition.view_id == uuid.UUID(t.captured["view_id"]),
                m.BaseTargetDefinition.active.is_(True),
            )
        )
        == 0
    )
    configure(client, t, groupBy={"property": "status"})
    restored = keyed(catalog(client, t))
    for key, before in first.items():
        assert restored[key]["id"] == before["id"]
        assert restored[key]["revision"] > before["revision"]
    configure(client, t, formulas={"renamed": "price * 2"})
    renamed = catalog(client, t)["targets"]
    assert len(renamed) == 1 and renamed[0]["id"] not in {item["id"] for item in first.values()}


def test_summary_filter_limit_sort_and_group_definition_semantics(client, targets):
    t = targets

    def save(**options):
        t.base.save(
            [
                view(
                    "Table",
                    summaries={"note.score": "Average"},
                    groupBy={"property": "status", "direction": "ASC"},
                    **options,
                ),
                view("Cards", "cards"),
            ]
        )
        inspect(client, t.w)
        return keyed(catalog(client, t))

    first = save()
    filtered = save(filters='file.inFolder("Work")')
    summary, group = ("summary", "note", "score"), ("group", "note", "status")
    assert filtered[summary]["revision"] > first[summary]["revision"]
    assert filtered[group] == first[group]
    limited = save(limit=5, sort=[{"property": "file.name", "direction": "ASC"}])
    reordered = save(limit=5, sort=[{"property": "file.name", "direction": "DESC"}])
    assert reordered[summary]["revision"] > limited[summary]["revision"]
    assert reordered[group] == first[group]


def test_catalog_pages_fence_registry_changes_and_never_mix_views(client, targets):
    t = targets
    configure(client, t, formulas={str(i): str(i) for i in range(110)})
    first = catalog(client, t)
    assert len(first["targets"]) == 100 and first["has_more"]
    catalog(client, t, expected=422, after=first["next_after"])
    second = catalog(
        client, t, after=first["next_after"], expected_revision=first["registry_revision"]
    )
    assert len(second["targets"]) == 10 and not second["has_more"]
    assert len({item["id"] for item in first["targets"] + second["targets"]}) == 110
    configure(client, t, formulas={str(i): str(i + 1) for i in range(110)})
    catalog(
        client,
        t,
        after=first["next_after"],
        expected_revision=first["registry_revision"],
        expected=409,
    )


def test_catalog_checks_current_definition_and_access_before_returning_targets(
    client, db_session, targets, monkeypatch
):
    t = targets
    configure(client, t)
    catalog(client, t)
    monkeypatch.setattr(get_settings(), "collaboration_bases_discussions_enabled", False)
    catalog(client, t, expected=409)
    monkeypatch.setattr(get_settings(), "collaboration_bases_discussions_enabled", True)
    t.base.save([view("Table"), view("Cards", "cards")], formulas={"new": "1"})
    catalog(client, t, expected=409)
    inspect(client, t.w)
    db_session.add(
        m.ManagedRule(
            share_id=t.w.share.id,
            user_id=t.w.reader.id,
            path=t.w.entry.path,
            recursive=False,
            permission="none",
        )
    )
    db_session.commit()
    catalog(client, t, expected=404)
    catalog(client, t, actor="owner")


def test_unmatched_and_archived_views_hide_catalog_and_repair_advances_proof(client, targets):
    t = targets
    configure(client, t)
    before = catalog(client, t)
    t.base.save([view("Unrelated", "list")], formulas={"double": "price * 2"})
    ambiguous = inspect(client, t.w)
    catalog(client, t, expected=409)
    repair(
        client,
        t.w,
        ambiguous,
        [{"slot": 0, "view_id": t.captured["view_id"]}],
        archive=[t.snapshot["views"][1]["id"]],
    )
    repaired = catalog(client, t)
    old = next(item for item in before["targets"] if item["property"]["name"] == "double")
    assert repaired["targets"][0]["id"] == old["id"]
    assert repaired["targets"][0]["revision"] > old["revision"]
    catalog(client, t, view_id=t.snapshot["views"][1]["id"], expected=409)


def test_limit_does_not_break_file_sync_or_existing_base_discussions(client, targets):
    t = targets
    configure(client, t)
    original = catalog(client, t)
    configure(client, t, formulas={str(i): "1" for i in range(service.MAX_PER_VIEW + 1)})
    catalog(client, t, expected=409)
    send(client, t.w, command(t, "view"))
    configure(client, t)
    restored = catalog(client, t)
    assert min(item["revision"] for item in restored["targets"]) > max(
        item["revision"] for item in original["targets"]
    )


def test_old_writer_gaps_and_lazy_upgrade_never_restore_stale_proof(client, db_session, targets):
    t = targets
    configure(client, t)
    first = catalog(client, t)
    # Simulate two registry transitions made by an older writer without this
    # table, returning to the same bytes. A read must invalidate the old proof.
    definition = db_session.get(m.BaseDefinition, uuid.UUID(first["definition_id"]))
    definition.revision += 2
    db_session.commit()
    after_gap = catalog(client, t)
    assert {item["id"] for item in after_gap["targets"]} == {
        item["id"] for item in first["targets"]
    }
    assert all(item["revision"] == definition.revision for item in after_gap["targets"])
    assert catalog(client, t) == after_gap
    for item in db_session.scalars(select(m.BaseTargetDefinition)):
        db_session.delete(item)
    db_session.commit()
    upgrade = catalog(client, t)
    assert len(upgrade["targets"]) == 2
    assert catalog(client, t) == upgrade


def test_ambiguous_properties_long_names_and_dynamic_dependencies_are_not_guessed():
    long_name = "n" * 256
    config = {"formulas": {"first": 'formula["next"]', "next": "10"}}
    target_view = view(
        "Table",
        groupBy={"property": "formula.first"},
        summaries={"status": "Unique", "note.status": "Filled", "note." + long_name: "Empty"},
    )
    first = service.candidates(config, target_view)
    assert len(first) == 4
    assert (
        next(item for item in first if item.kind == "summary").descriptor["property"]["name"]
        == long_name
    )
    config["formulas"]["next"] = "20"
    second = service.candidates(config, target_view)
    assert all(a.fingerprint != b.fingerprint for a, b in zip(first, second))


def test_total_catalog_budget_retains_other_discussions(client, targets):
    t = targets
    t.base.save(
        [view("Table"), view("Cards", "cards")] + [view(f"Extra {i}") for i in range(3)],
        formulas={str(i): "1" for i in range(210)},
    )
    inspected = inspect(client, t.w)
    assert all(item["id"] for item in inspected["views"])
    catalog(client, t, expected=409)
    send(client, t.w, command(t, "view"))


def test_malformed_definition_dependencies_cannot_receive_active_identities():
    assert (
        service.candidates(
            {"formulas": {"bad": 123}, "summaries": {"bad": None}},
            view(
                "Table",
                groupBy={"property": "formula.bad"},
                summaries={"formula.bad": "Sum", "score": "bad"},
            ),
        )
        == []
    )
