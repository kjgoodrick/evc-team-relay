import base64
import hashlib
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError
from test_discussions import world as discussion_world

from app.services import inline_base_source as service

world = discussion_world

BLOCK = "```base\nviews:\n  - type: table\n    name: Table\n```"


def test_exact_fence_in_unicode_note_and_quote():
    for prefix in ("🌿\n\n", "> "):
        source = prefix + (BLOCK.replace("\n", "\n> ") if prefix == "> " else BLOCK)
        start = len(prefix.encode("utf-16-le")) // 2
        result = service.definition_at(source, start, start + 7)
        assert result.views == [{"type": "table", "name": "Table"}]


@pytest.mark.parametrize(
    "source",
    [
        "````md\n" + BLOCK + "\n````",
        "---\nexample: |\n  " + BLOCK.replace("\n", "\n  ") + "\n---",
        "```base\nviews: []",
    ],
)
def test_examples_frontmatter_and_unclosed_fences_are_not_definitions(source):
    start = source.index("```base")
    with pytest.raises(HTTPException) as exc:
        service.definition_at(source, start, start + 7)
    assert exc.value.detail["code"] == "inline_base_detached"


def test_identical_blocks_require_the_exact_occurrence():
    source = BLOCK + "\n\n" + BLOCK
    assert service.definition_at(source, len(BLOCK) + 2, len(BLOCK) + 9).views[0]["name"] == "Table"
    with pytest.raises(HTTPException):
        service.definition_at(source, 1, 8)


@pytest.fixture
def observation(monkeypatch):
    monkeypatch.setattr(service.anchor_service, "enabled", lambda: True)
    monkeypatch.setattr(service.anchor_service, "relay_origin", lambda: "http://private-relay")
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(collaboration_anchor_relay_token=SecretStr("test-only")),
    )
    payload = service.InlineSourceRequest(
        dependency="AA==", start="AQ==", end="Ag==", selected_length=7
    )
    replies = {
        "register": {},
        "validate": {
            "observed_snapshot_sha256": "a" * 64,
            "placements": [{"status": "attached", "from": 0, "to": 7}],
        },
        "source": {
            "observed_snapshot_sha256": "a" * 64,
            "source_prefix": BLOCK,
            "source_complete": True,
            "source_prefix_sha256": hashlib.sha256(BLOCK.encode()).hexdigest(),
        },
    }
    calls = []

    def post(client, origin, token, route, request, deadline, **kwargs):
        calls.append((route, request))
        return replies[route]

    monkeypatch.setattr(service.anchor_service, "_post", post)
    return payload, replies, calls


def test_private_source_observation_binds_stable_identity_and_never_accepts_client_yaml(
    observation,
):
    request, replies, calls = observation
    ids = [uuid.uuid4() for _ in range(3)]
    proof = service.observe(*ids, request)
    proof.check(*ids, request)
    assert proof.definition.views[0]["name"] == "Table"
    assert [route for route, _ in calls] == ["register", "validate", "source"]
    updated = request.model_copy(update={"dependency": base64.b64encode(b"new snapshot").decode()})
    assert updated.identity(*ids) == request.identity(*ids)
    with pytest.raises(ValidationError):
        service.InlineSourceRequest(**request.model_dump(), source=BLOCK)


@pytest.mark.parametrize(
    "change,code",
    [
        ("snapshot", "inline_base_source_changed"),
        ("truncated", "inline_base_source_too_large"),
        ("detached", "inline_base_detached"),
        ("digest", "anchor_validation_unavailable"),
    ],
)
def test_unverified_source_is_rejected(observation, change, code):
    request, replies, _ = observation
    if change == "snapshot":
        replies["source"]["observed_snapshot_sha256"] = "b" * 64
    if change == "truncated":
        replies["source"]["source_complete"] = False
    if change == "detached":
        replies["validate"]["placements"] = [{"status": "unanchored"}]
    if change == "digest":
        replies["source"]["source_prefix_sha256"] = "c" * 64
    with pytest.raises(HTTPException) as exc:
        service.observe(*(uuid.uuid4() for _ in range(3)), request)
    assert exc.value.detail["code"] == code


def test_reader_inspection_checks_permission_before_and_after_relay_io(
    client, db_session, monkeypatch, world
):
    import time

    from test_discussions import headers

    from app.core.config import get_settings
    from app.services import bases_definition

    monkeypatch.setattr(get_settings(), "collaboration_bases_registry_enabled", True)
    generation = uuid.uuid4()
    world.entry.collaboration_generation = generation
    db_session.commit()
    request = service.InlineSourceRequest(
        dependency="AA==", start="AQ==", end="Ag==", selected_length=7
    )
    calls = []

    def observe(share, entry, gen, source):
        calls.append(entry)
        return service.InlineSourceObservation(
            source.identity(share, entry, gen),
            "a" * 64,
            bases_definition.parse(b"views: [{name: Table, type: table}]"),
            service.anchor_service.now_ms() + 15000,
            time.monotonic() + 12,
        )

    monkeypatch.setattr(service, "observe", observe)
    url = f"/v1/collaboration/shares/{world.share.id}/entries/{world.entry.id}/bases/inline-source"
    body = {"generation": str(generation), "source": request.model_dump()}
    response = client.post(url, json=body, headers=headers(world.tokens["reader"]))
    assert response.status_code == 200, response.text
    assert response.json()["configuration"]["views"][0]["name"] == "Table"
    assert len(calls) == 1
    hidden = (
        f"/v1/collaboration/shares/{world.share.id}/entries/{world.other.id}/bases/inline-source"
    )
    assert (
        client.post(hidden, json=body, headers=headers(world.tokens["reader"])).status_code == 404
    )
    assert len(calls) == 1

    def changed_generation(*args):
        proof = observe(*args)
        world.entry.collaboration_generation = uuid.uuid4()
        db_session.commit()
        return proof

    monkeypatch.setattr(service, "observe", changed_generation)
    response = client.post(url, json=body, headers=headers(world.tokens["reader"]))
    assert response.status_code == 409
    assert response.json()["error"]["message"]["code"] == "anchor_generation_mismatch"


def test_invalid_inline_yaml_reports_a_validation_error():
    with pytest.raises(HTTPException) as exc:
        service.definition_at("```base\nviews: invalid\n```", 0, 7)
    assert exc.value.status_code == 422
