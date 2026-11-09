import base64
import json
import time
import uuid

import pytest
from sqlalchemy import select
from test_discussions import headers
from test_discussions import world as discussion_world

from app.core.config import get_settings
from app.db import models as m
from app.services import bases_definition, inline_base_source

world = discussion_world


@pytest.fixture
def inline(world, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_bases_registry_enabled", True)
    generation = uuid.uuid4()
    world.entry.collaboration_generation = generation
    db_session.commit()
    state = {"views": [{"name": "Table", "type": "table"}, {"name": "Cards", "type": "cards"}]}

    def observe(share, entry, gen, source):
        return inline_base_source.InlineSourceObservation(
            source.identity(share, entry, gen),
            "a" * 64,
            bases_definition.parse(json.dumps(state).encode()),
            inline_base_source.anchor_service.now_ms() + 15000,
            time.monotonic() + 12,
        )

    monkeypatch.setattr(inline_base_source, "observe", observe)
    return state, generation


def request(client, world, generation, start="AQ==", member="reader"):
    return client.post(
        f"/v1/collaboration/shares/{world.share.id}/entries/{world.entry.id}/bases/inline-inspect",
        headers=headers(world.tokens[member]),
        json={
            "generation": str(generation),
            "source": {
                "dependency": "AA==",
                "start": start,
                "end": "Ag==",
                "selected_length": 7,
            },
        },
    )


def test_inline_definitions_are_distinct_and_retries_reuse_view_ids(
    client, world, inline, db_session
):
    state, generation = inline
    first = request(client, world, generation)
    assert first.status_code == 200, first.text
    first = first.json()
    assert first["revision"] == 1
    assert all(view["id"] for view in first["views"])
    assert request(client, world, generation).json() == first
    second = request(client, world, generation, base64.b64encode(b"another fence").decode()).json()
    assert second["definition_id"] != first["definition_id"]
    assert set(view["id"] for view in first["views"]).isdisjoint(
        view["id"] for view in second["views"]
    )
    definitions = list(db_session.scalars(select(m.BaseDefinition)))
    assert len(definitions) == 2
    assert all(
        definition.source_key != "file" and definition.source_anchor for definition in definitions
    )
    assert world.entry.record == {"type": "markdown"}


def test_inline_view_identity_survives_reorder_rename_and_body_edits(client, world, inline):
    state, generation = inline
    first = request(client, world, generation).json()
    original = {view["name"]: view["id"] for view in first["views"]}
    state["views"].reverse()
    state["views"][1]["name"] = "Renamed table"
    next_result = request(client, world, generation).json()
    assert next_result["definition_id"] == first["definition_id"]
    assert [view["id"] for view in next_result["views"]] == [original["Cards"], original["Table"]]
    state["views"][1]["order"] = ["file.name", "note.status"]
    edited = request(client, world, generation).json()
    assert edited["views"][1]["id"] == original["Table"]
    assert edited["revision"] == 3


def test_deleted_view_is_archived_without_reusing_its_identity(client, world, inline, db_session):
    state, generation = inline
    first = request(client, world, generation).json()
    removed = first["views"][1]["id"]
    state["views"].pop()
    request(client, world, generation)
    old = db_session.get(m.BaseView, uuid.UUID(removed))
    assert old.state == "archived" and old.slot is None
    state["views"].append({"name": "Cards", "type": "cards"})
    result = request(client, world, generation).json()
    assert result["views"][1]["id"] != removed


def test_late_different_configuration_cannot_roll_back_newer_registry(
    client, world, inline, db_session
):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.services.inline_bases_registry import synchronize_verified

    state, generation = inline
    initial = request(client, world, generation).json()
    source = inline_base_source.InlineSourceRequest(
        dependency="AA==", start="AQ==", end="Ag==", selected_length=7
    )
    state["views"][0]["name"] = "Intermediate"
    stale = inline_base_source.observe(
        world.share.id, uuid.UUID(world.entry.id), generation, source
    )
    state["views"][0]["name"] = "Latest"
    latest = request(client, world, generation).json()
    assert latest["revision"] == initial["revision"] + 1
    with pytest.raises(HTTPException) as exc:
        synchronize_verified(
            db_session,
            SimpleNamespace(entry=world.entry, share=world.share),
            stale,
            source,
            initial["revision"],
        )
    assert exc.value.detail["code"] == "stale_base_revision"
    db_session.commit()
    assert request(client, world, generation).json() == latest


def test_inline_history_is_readable_by_definition_and_stays_isolated(client, world, inline):
    state, generation = inline
    first = request(client, world, generation).json()
    state["views"] = [{"name": "Table", "type": "table"}]
    updated = request(client, world, generation).json()
    other = request(client, world, generation, start="Aw==").json()
    path = f"/v1/collaboration/shares/{world.share.id}/entries/{world.entry.id}/bases/views"
    result = client.get(
        path,
        headers=headers(world.tokens["reader"]),
        params={"state": "archived", "definition_id": first["definition_id"]},
    )
    assert result.status_code == 200, result.text
    assert [view["id"] for view in result.json()["views"]] == [first["views"][1]["id"]]
    assert updated["definition_id"] == first["definition_id"]
    result = client.get(
        path,
        headers=headers(world.tokens["reader"]),
        params={"state": "archived", "definition_id": other["definition_id"]},
    )
    assert result.status_code == 200
    assert result.json()["views"] == []
    result = client.get(
        path,
        headers=headers(world.tokens["reader"]),
        params={"state": "archived", "definition_id": str(uuid.uuid4())},
    )
    assert result.status_code == 404
