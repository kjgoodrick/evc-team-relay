"""Location transport acceptance against the application and real authorization.

The WebSocket test driver's receive is bounded so a missing close/update fails
the test instead of leaving a hung test process. Slow-consumer and concurrent
authorization races are tested separately with controlled async transports.
"""

import asyncio
import json
import time
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import anyio
import jwt
import pytest
from fastapi import HTTPException, WebSocketDisconnect
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.api.routers import collaboration_events as events
from app.core import security
from app.core.config import get_settings
from app.db import models
from app.db import session as session_module
from app.services import collaboration_presence as presence
from app.services import session_service
from app.services.presence_protocol import PresenceIdentity

pytestmark = pytest.mark.db_commit
PATH = "/v1/collaboration/events"


@pytest.fixture
def engine(test_env, tmp_path, monkeypatch):
    # Concurrent socket authorization needs independent database connections;
    # the suite's normal in-memory StaticPool shares one sqlite connection
    # across threads and cannot model overlapping transactions safely.
    database = create_engine(
        "sqlite+pysqlite:///" + str(tmp_path / "locations.sqlite"),
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(session_module, "_engine", database)
    monkeypatch.setattr(
        session_module, "_SessionLocal", sessionmaker(bind=database, autoflush=False)
    )
    yield database
    database.dispose()


def guid():
    return str(uuid.uuid4())


def recv(socket):
    async def bounded_receive():
        with anyio.fail_after(3):
            return await socket._send_rx.receive()

    message = socket.portal.call(bounded_receive)
    socket._raise_on_close(message)
    return json.loads(message["text"])


def ticket(client, token):
    response = client.post(
        "/v1/collaboration/socket-ticket", headers={"Authorization": "Bearer " + token}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["socket_path"] == PATH and body["protocol_version"] == 1
    assert body["expires_in"] == 30
    return body["ticket"]


@contextmanager
def connected(client, token, instance=None, *, protocol_version=None):
    instance = instance or guid()
    value = ticket(client, token)
    with client.websocket_connect(PATH) as socket:
        message = {"type": "authenticate", "ticket": value, "instance_id": instance}
        if protocol_version is not None:
            message["protocol_version"] = protocol_version
        socket.send_json(message)
        ready = recv(socket)
        assert ready["type"] == "ready" and ready["instance_id"] == instance
        assert ready["protocol_version"] == (protocol_version or 1)
        assert len(ready["session_id"]) == 32
        yield socket, ready


def publish(socket, location):
    socket.send_json({"type": "presence", "location": location})
    socket.send_json({"type": "ping"})
    assert recv(socket) == {"type": "pong"}  # Publication has been processed.


def follow(socket, user_id, ready, location, subscription=None):
    subscription = subscription or guid()
    socket.send_json(
        {
            "type": "follow",
            "subscription_id": subscription,
            "target": {
                "user_id": str(user_id),
                "session_id": ready["session_id"],
                "instance_id": ready["instance_id"],
            },
            "from": location,
        }
    )
    return subscription


def available(subscription, location):
    return {
        "type": "location",
        "subscription_id": subscription,
        "status": "available",
        "location": location,
    }


def unavailable(subscription):
    return {"type": "location", "subscription_id": subscription, "status": "unavailable"}


@pytest.fixture
def team(db_session, test_user, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_presence_enabled", True)
    viewer = models.User(email="reader@example.com", password_hash="", is_active=True)
    db_session.add(viewer)
    db_session.flush()
    share = models.Share(
        kind=models.ShareKind.FOLDER,
        path="Docs",
        managed=True,
        visibility=models.ShareVisibility.PRIVATE,
        owner_user_id=test_user.id,
    )
    db_session.add(share)
    db_session.flush()
    member = models.ShareMember(
        share_id=share.id, user_id=viewer.id, role=models.ShareMemberRole.VIEWER
    )
    rule = models.ManagedRule(
        share_id=share.id, user_id=viewer.id, path="", recursive=True, permission="read"
    )
    db_session.add_all([member, rule])
    db_session.add(
        models.ManagedRule(
            share_id=share.id,
            user_id=viewer.id,
            path="Private.md",
            recursive=False,
            permission="none",
        )
    )
    notes = {}
    for name in ("First", "Second", "Private"):
        entry = models.ManagedEntry(
            id=guid(), share_id=share.id, path=name + ".md", record={"type": "markdown"}
        )
        db_session.add(entry)
        notes[name.lower()] = entry
    tokens, sessions = {}, {}
    for name, user in (("owner", test_user), ("viewer", viewer)):
        session, _ = session_service.create_session(db_session, user.id)
        sessions[name] = session
        tokens[name] = security.create_access_token(str(user.id), session_id=str(session.id))
    db_session.commit()

    def location(name="first", surface="editor"):
        return {
            "kind": "note",
            "share_id": str(share.id),
            "entry_id": notes[name].id,
            "surface": surface,
        }

    return SimpleNamespace(
        owner=test_user,
        viewer=viewer,
        share=share,
        rule=rule,
        member=member,
        notes=notes,
        tokens=tokens,
        sessions=sessions,
        location=location,
    )


def test_ticket_requires_live_session_and_is_single_use(client, team):
    for token in ("bad", security.create_access_token(str(team.owner.id))):
        response = client.post(
            "/v1/collaboration/socket-ticket", headers={"Authorization": "Bearer " + token}
        )
        assert response.status_code == 401
    value = ticket(client, team.tokens["owner"])
    message = {"type": "authenticate", "ticket": value, "instance_id": guid()}
    with client.websocket_connect(PATH) as socket:
        socket.send_json(message)
        ready = recv(socket)
        assert ready["type"] == "ready"
        assert str(team.sessions["owner"].id) not in json.dumps(ready)
    with client.websocket_connect(PATH) as socket:
        socket.send_json(message)
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == 4403
    assert client.app.state.collaboration_presence.handshakes == 0


def test_ticket_expiry_and_pending_limit(client, team):
    value = ticket(client, team.tokens["owner"])
    broker = client.app.state.collaboration_presence
    for pending in broker.tickets.values():
        pending.expires = time.monotonic() - 1
    with client.websocket_connect(PATH) as socket:
        socket.send_json({"type": "authenticate", "ticket": value, "instance_id": guid()})
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == 4403
    for _ in range(8):
        ticket(client, team.tokens["owner"])
    response = client.post(
        "/v1/collaboration/socket-ticket",
        headers={"Authorization": "Bearer " + team.tokens["owner"]},
    )
    assert response.status_code == 429


def test_channel_disabled_and_url_credentials_reject(client, team, monkeypatch):
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(PATH + "?token=forbidden"):
            pass
    assert error.value.code == 4403
    monkeypatch.setattr(get_settings(), "collaboration_presence_enabled", False)
    assert client.post("/v1/collaboration/socket-ticket").status_code == 404
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(PATH):
            pass
    assert error.value.code == 4403


def test_capability_is_explicit_and_scoped_credentials_cannot_get_tickets(client, team):
    response = client.get(
        f"/v1/collaboration/capabilities?share_id={team.share.id}",
        headers={"Authorization": "Bearer " + team.tokens["viewer"]},
    )
    assert response.status_code == 200
    assert response.json()["features"]["presence_locations_preview"] is True
    assert response.json()["features"]["presence_surface_locations_preview"] is True
    claims = security.decode_access_token(team.tokens["owner"])
    claims["scope"] = "managed"
    scoped = jwt.encode(claims, get_settings().jwt_secret, algorithm=get_settings().jwt_algorithm)
    response = client.post(
        "/v1/collaboration/socket-ticket", headers={"Authorization": "Bearer " + scoped}
    )
    assert response.status_code == 401


def test_read_only_viewer_follows_across_authorized_notes_and_private_note_stops(client, team):
    with connected(client, team.tokens["owner"]) as (publisher, ready):
        with connected(client, team.tokens["viewer"]) as (viewer, _):
            publish(publisher, team.location())
            subscription = follow(viewer, team.owner.id, ready, team.location())
            assert recv(viewer) == {"type": "following", "subscription_id": subscription}
            assert recv(viewer) == available(subscription, team.location())
            publish(publisher, team.location("second", "reading"))
            assert recv(viewer) == available(subscription, team.location("second", "reading"))
            publish(publisher, team.location("private"))
            assert recv(viewer) == unavailable(subscription)
            # Access/visibility loss removes the subscription; becoming visible
            # again must not unexpectedly pull the former follower to a note.
            publish(publisher, team.location())
            viewer.send_json({"type": "ping"})
            assert recv(viewer) == {"type": "pong"}
    assert not client.app.state.collaboration_presence.connections
    assert not client.app.state.collaboration_presence.followers


@pytest.mark.parametrize("mode", ["wrong-note", "wrong-person", "hidden", "self"])
def test_initial_follow_requires_known_visible_shared_context(client, team, mode):
    with connected(client, team.tokens["owner"]) as (publisher, ready):
        with connected(client, team.tokens["viewer"]) as (viewer, viewer_ready):
            publish(publisher, None if mode == "hidden" else team.location())
            subscription = follow(
                viewer,
                team.viewer.id
                if mode == "self"
                else guid()
                if mode == "wrong-person"
                else team.owner.id,
                viewer_ready if mode == "self" else ready,
                team.location("second" if mode == "wrong-note" else "first"),
            )
            assert recv(viewer) == unavailable(subscription)


@pytest.mark.parametrize("loss", ["hidden", "deleted", "permission", "membership", "session"])
def test_idle_follow_revalidates_visibility_access_and_sessions(
    client, db_session, team, monkeypatch, loss
):
    monkeypatch.setattr(presence, "CHECK_SECONDS", 0.05)
    with connected(client, team.tokens["owner"]) as (publisher, ready):
        with connected(client, team.tokens["viewer"]) as (viewer, _):
            publish(publisher, team.location())
            subscription = follow(viewer, team.owner.id, ready, team.location())
            assert recv(viewer)["type"] == "following"
            assert recv(viewer) == available(subscription, team.location())
            if loss == "hidden":
                publish(publisher, None)
            elif loss == "deleted":
                team.notes["first"].deleted = True
            elif loss == "permission":
                team.rule.permission = "none"
            elif loss == "membership":
                db_session.delete(team.member)
            else:
                db_session.delete(team.sessions["owner"])
            db_session.commit()
            assert recv(viewer) == unavailable(subscription)


def test_publisher_disconnect_stops_follow_and_unfollow_ignores_old_subscription(client, team):
    with connected(client, team.tokens["viewer"]) as (viewer, _):
        with connected(client, team.tokens["owner"]) as (publisher, ready):
            publish(publisher, team.location())
            subscription = follow(viewer, team.owner.id, ready, team.location())
            assert recv(viewer)["type"] == "following"
            assert recv(viewer) == available(subscription, team.location())
            viewer.send_json({"type": "unfollow", "subscription_id": guid()})
            viewer.send_json({"type": "ping"})
            assert recv(viewer) == {"type": "pong"}
        assert recv(viewer) == unavailable(subscription)


def test_matching_unfollow_removes_subscription(client, team):
    with connected(client, team.tokens["owner"]) as (publisher, ready):
        with connected(client, team.tokens["viewer"]) as (viewer, _):
            publish(publisher, team.location())
            subscription = follow(viewer, team.owner.id, ready, team.location())
            assert recv(viewer)["type"] == "following"
            assert recv(viewer) == available(subscription, team.location())
            viewer.send_json({"type": "unfollow", "subscription_id": subscription})
            viewer.send_json({"type": "ping"})
            assert recv(viewer) == {"type": "pong"}
            assert not client.app.state.collaboration_presence.followers


@pytest.mark.parametrize("destination", ["private", "missing", "asset"])
def test_publisher_cannot_publish_unreadable_missing_or_non_markdown_location(
    client, db_session, team, destination
):
    location = team.location("private")
    if destination == "missing":
        location["entry_id"] = guid()
    elif destination == "asset":
        team.notes["second"].record = {"type": "file"}
        db_session.commit()
        location = team.location("second")
    with connected(client, team.tokens["viewer"]) as (socket, _):
        socket.send_json({"type": "presence", "location": location})
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == 4403


def test_reauthentication_refreshes_only_same_user_session_and_instance(client, team):
    instance = guid()
    with connected(client, team.tokens["owner"], instance) as (socket, ready):
        refreshed = security.create_access_token(
            str(team.owner.id), session_id=str(team.sessions["owner"].id)
        )
        socket.send_json(
            {"type": "authenticate", "ticket": ticket(client, refreshed), "instance_id": instance}
        )
        assert recv(socket) == {"type": "authenticated", "expires_at": ready["expires_at"]}
        socket.send_json(
            {
                "type": "authenticate",
                "ticket": ticket(client, team.tokens["viewer"]),
                "instance_id": instance,
            }
        )
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == 4403


def test_reauthentication_cannot_switch_login_session_for_same_account(client, db_session, team):
    session, _ = session_service.create_session(db_session, team.owner.id)
    db_session.commit()
    other = security.create_access_token(str(team.owner.id), session_id=str(session.id))
    with connected(client, team.tokens["owner"]) as (socket, ready):
        socket.send_json(
            {
                "type": "authenticate",
                "ticket": ticket(client, other),
                "instance_id": ready["instance_id"],
            }
        )
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == 4403


def test_ticket_cannot_keep_revoked_session_alive(client, db_session, team):
    value = ticket(client, team.tokens["owner"])
    db_session.delete(team.sessions["owner"])
    db_session.commit()
    with client.websocket_connect(PATH) as socket:
        socket.send_json({"type": "authenticate", "ticket": value, "instance_id": guid()})
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == 4403


def test_viewer_session_revocation_closes_idle_channel(client, db_session, team, monkeypatch):
    monkeypatch.setattr(presence, "CHECK_SECONDS", 0.05)
    with connected(client, team.tokens["viewer"]) as (socket, _):
        db_session.delete(team.sessions["viewer"])
        db_session.commit()
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == 4403


def test_idle_channel_and_handshake_reservations_are_bounded(client, team, monkeypatch):
    owner = client.app.state.collaboration_presence
    owner.handshakes = presence.MAX_CONNECTIONS
    try:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(PATH):
                pass
        assert error.value.code == 4429
    finally:
        owner.handshakes = 0
    monkeypatch.setattr(presence, "CHECK_SECONDS", 0.05)
    monkeypatch.setattr(presence, "IDLE_SECONDS", 0.01)
    with connected(client, team.tokens["owner"]) as (socket, _):
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == 4403
    assert not owner.connections and owner.handshakes == 0


def test_duplicate_instance_rejected_without_displacing_owner(client, team):
    instance = guid()
    with connected(client, team.tokens["owner"], instance) as (socket, _):
        with pytest.raises(WebSocketDisconnect) as error:
            with connected(client, team.tokens["owner"], instance):
                pass
        assert error.value.code == 4409
        publish(socket, team.location())
        assert len(client.app.state.collaboration_presence.connections) == 1


@pytest.mark.parametrize(
    "message",
    [
        '{"type":"ping","type":"ping"}',
        '{"type":"ping","extra":1}',
        '{"type":"presence","location":NaN}',
        '{"type":"presence","location":{}}',
        "[]",
        '"' + "x" * 4096 + '"',
    ],
)
def test_malformed_frames_close_without_echoing_data(client, team, message):
    with connected(client, team.tokens["owner"]) as (socket, _):
        socket.send_text(message)
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == 4400 and not error.value.reason


@pytest.fixture
def surface_team(db_session, team, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_policy_enabled", True)
    monkeypatch.setattr(get_settings(), "collaboration_bases_registry_enabled", True)
    image = models.ManagedEntry(
        id=guid(),
        share_id=team.share.id,
        path="Diagram.png",
        record={"type": "image", "hash": "a" * 64},
    )
    base = models.ManagedEntry(
        id=guid(),
        share_id=team.share.id,
        path="Planning.base",
        record={"type": "file", "hash": "b" * 64},
    )
    db_session.add_all([image, base])
    db_session.flush()
    definition = models.BaseDefinition(
        entry_id=base.id,
        generation=base.collaboration_generation,
        revision=1,
        content_sha256="b" * 64,
        configuration={"views": []},
    )
    db_session.add(definition)
    db_session.flush()
    views = [
        models.BaseView(
            definition_id=definition.id,
            slot=slot,
            state="active",
            descriptor={"name": f"View {slot}", "type": kind},
        )
        for slot, kind in enumerate(("table", "sabidurian-timeline"))
    ]
    db_session.add_all(views)
    db_session.commit()
    locations = [
        {
            "kind": "image",
            "share_id": str(team.share.id),
            "entry_id": image.id,
            "generation": str(image.collaboration_generation),
            "content_sha256": "a" * 64,
        },
        *[
            {
                "kind": "base",
                "share_id": str(team.share.id),
                "entry_id": base.id,
                "generation": str(base.collaboration_generation),
                "definition_id": str(definition.id),
                "view_id": str(view.id),
                "registry_revision": 1,
            }
            for view in views
        ],
    ]
    return SimpleNamespace(
        image=image, base=base, definition=definition, views=views, locations=locations
    )


def test_v2_follows_note_image_and_saved_views_with_read_permission(client, team, surface_team):
    with connected(client, team.tokens["owner"], protocol_version=2) as (publisher, ready):
        with connected(client, team.tokens["viewer"], protocol_version=2) as (viewer, _):
            publish(publisher, team.location())
            subscription = follow(viewer, team.owner.id, ready, team.location())
            assert recv(viewer) == {"type": "following", "subscription_id": subscription}
            assert recv(viewer) == available(subscription, team.location())
            for location in [*surface_team.locations, team.location("second", "reading")]:
                publish(publisher, location)
                assert recv(viewer) == available(subscription, location)


def image_context(client, team, entry, token=None):
    return client.get(
        "/v1/collaboration/image-location",
        params={"share_id": str(team.share.id), "entry_id": entry.id},
        headers={"Authorization": "Bearer " + (token or team.tokens["viewer"])},
    )


def base_context(client, team, entry=None):
    return client.get(
        "/v1/collaboration/base-context",
        params={"share_id": str(team.share.id), "entry_id": entry.id},
        headers={"Authorization": "Bearer " + team.tokens["viewer"]},
    )


def test_base_context_reads_active_views_without_writes_config_or_comment_permission(
    client, db_session, engine, team, surface_team, monkeypatch
):
    monkeypatch.setattr(get_settings(), "collaboration_discussions_enabled", False)
    surface_team.views[0].descriptor = {
        "name": "Work",
        "type": "table",
        "filters": "private filter",
    }
    db_session.add(
        models.BaseView(
            definition_id=surface_team.definition.id,
            state="archived",
            descriptor={"name": "Old private view", "type": "table"},
        )
    )
    db_session.commit()
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = base_context(client, team, surface_team.base)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    expected = {
        key: value
        for key, value in surface_team.locations[1].items()
        if key not in {"kind", "view_id"}
    }
    assert response.json() == {
        "protocol_version": 1,
        **expected,
        "content_sha256": "b" * 64,
        "views": [
            {
                "id": str(view.id),
                "slot": slot,
                "name": "Work" if slot == 0 else "View 1",
                "type": "table" if slot == 0 else "sabidurian-timeline",
            }
            for slot, view in enumerate(surface_team.views)
        ],
    }
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert not any("collaboration_bases.configuration" in statement for statement in statements)
    assert (
        len([statement for statement in statements if "JOIN collaboration_base_views" in statement])
        == 1
    )
    for private in ("private filter", "Old private view", surface_team.base.path):
        assert private not in response.text


@pytest.mark.parametrize(
    "loss",
    [
        "permission",
        "membership",
        "session",
        "deleted",
        "type",
        "path",
        "hash",
        "generation",
        "definition",
    ],
)
def test_base_context_rechecks_live_access_and_exact_registry(
    client, db_session, team, surface_team, loss
):
    assert base_context(client, team, surface_team.base).status_code == 200
    if loss == "permission":
        team.rule.permission = "none"
    elif loss == "membership":
        db_session.delete(team.member)
    elif loss == "session":
        db_session.delete(team.sessions["viewer"])
    elif loss == "deleted":
        surface_team.base.deleted = True
    elif loss == "path":
        surface_team.base.path = "Other.txt"
    elif loss == "generation":
        surface_team.base.collaboration_generation = uuid.uuid4()
    elif loss == "definition":
        db_session.delete(surface_team.definition)
    else:
        surface_team.base.record = (
            {"type": "image"} if loss == "type" else {"type": "file", "hash": "c" * 64}
        )
    db_session.commit()
    response = base_context(client, team, surface_team.base)
    assert response.status_code in (401, 403, 404), response.text
    assert "views" not in response.json()


def test_base_context_omits_unmatched_views_and_reflects_new_revision(
    client, db_session, team, surface_team
):
    surface_team.views[0].state = "unmatched"
    surface_team.views[0].slot = None
    surface_team.definition.revision += 1
    db_session.commit()
    response = base_context(client, team, surface_team.base)
    assert response.status_code == 200
    assert response.json()["registry_revision"] == 2
    assert [view["slot"] for view in response.json()["views"]] == [1]
    surface_team.views[1].state = "unmatched"
    surface_team.views[1].slot = None
    db_session.commit()
    assert base_context(client, team, surface_team.base).json()["views"] == []


@pytest.mark.parametrize("damage", ["name", "duplicate", "slot", "type", "limit"])
def test_base_context_rejects_malformed_or_oversized_identity_metadata(
    client, db_session, team, surface_team, damage
):
    if damage == "name":
        surface_team.views[0].descriptor = {"name": "bad\nname", "type": "table"}
    elif damage == "duplicate":
        surface_team.views[1].descriptor = surface_team.views[0].descriptor
    elif damage == "slot":
        surface_team.views[0].slot = 64
    elif damage == "type":
        surface_team.views[0].descriptor = {"name": "Work", "type": 1}
    else:
        db_session.add_all(
            [
                models.BaseView(
                    definition_id=surface_team.definition.id,
                    slot=slot,
                    state="active",
                    descriptor={"name": str(slot), "type": "table"},
                )
                for slot in range(2, 65)
            ]
        )
    db_session.commit()
    assert base_context(client, team, surface_team.base).status_code == 403


@pytest.mark.parametrize(
    "setting",
    [
        "collaboration_presence_enabled",
        "collaboration_policy_enabled",
        "collaboration_bases_registry_enabled",
    ],
)
def test_base_context_is_explicitly_gated(client, team, surface_team, monkeypatch, setting):
    monkeypatch.setattr(get_settings(), setting, False)
    assert base_context(client, team, surface_team.base).status_code == 404
    response = client.get(
        f"/v1/collaboration/capabilities?share_id={team.share.id}",
        headers={"Authorization": "Bearer " + team.tokens["viewer"]},
    )
    assert response.json()["features"]["presence_base_context_preview"] is False


def move_image(socket, location, state):
    socket.send_json({"type": "image_state", "location": location, "state": state})
    socket.send_json({"type": "ping"})
    assert recv(socket) == {"type": "pong"}


def moving_watch(socket, location):
    subscription = guid()
    socket.send_json(
        {
            "type": "watch",
            "subscription_id": subscription,
            "location": location,
            "image_states": True,
        }
    )
    initial = [recv(socket), recv(socket)]
    assert {value["type"] for value in initial} == {"roster", "image_states"}
    assert all(
        value["status"] == "available" and value["subscription_id"] == subscription
        for value in initial
    )
    return subscription


def embedded_image(surface_team=None, **changes):
    image = surface_team.locations[0] if surface_team else None
    return {
        "version": 1,
        "occurrence": {
            "anchor": {"tname": "contents", "item": {"client": 7, "clock": 10}, "assoc": 0},
            "head": {"tname": "contents", "item": {"client": 7, "clock": 30}, "assoc": -1},
        },
        "source_sha256": "c" * 64,
        "asset": {
            "kind": "managed",
            "entry_id": image["entry_id"],
            "generation": image["generation"],
            "sha256": image["content_sha256"],
        }
        if image
        else {"kind": "external", "url_sha256": "d" * 64, "version_evidence": "unknown"},
        "basis": {"width": 640, "height": 480, "orientation": "display"},
        "state": {"pointer": {"x": 0.2, "y": 0.5}, "viewport": None},
        **changes,
    }


def move_embed(socket, location, embed):
    socket.send_json({"type": "image_embed", "location": location, "embed": embed})
    socket.send_json({"type": "ping"})
    assert recv(socket) == {"type": "pong"}


def watch_embeds(socket, location):
    subscription = guid()
    socket.send_json(
        {"type": "watch_embeds", "subscription_id": subscription, "location": location}
    )
    return subscription


def embed_frame(subscription, peers=None):
    return {
        "type": "image_embeds",
        "subscription_id": subscription,
        "status": "available",
        "peers": peers or [],
    }


def test_embeds_cross_editor_reading_with_authenticated_identity_and_no_comment_permission(
    client, team, surface_team, monkeypatch
):
    monkeypatch.setattr(get_settings(), "collaboration_discussions_enabled", False)
    embed = embedded_image(surface_team)
    with connected(client, team.tokens["viewer"], protocol_version=2) as (publisher, ready):
        publish(publisher, team.location())
        with connected(client, team.tokens["owner"], protocol_version=2) as (viewer, _):
            subscription = watch_embeds(viewer, team.location(surface="reading"))
            assert recv(viewer) == embed_frame(subscription)
            move_embed(publisher, team.location(), embed)
            target = {
                "user_id": str(team.viewer.id),
                "session_id": ready["session_id"],
                "instance_id": ready["instance_id"],
            }
            assert recv(viewer) == embed_frame(subscription, [{"target": target, "embed": embed}])
            # A second copy of the same image has a different relative interval.
            second = embedded_image(
                surface_team,
                occurrence={
                    "anchor": {"tname": "contents", "item": {"client": 7, "clock": 50}},
                    "head": {"tname": "contents", "item": {"client": 7, "clock": 70}},
                },
            )
            move_embed(publisher, team.location(), second)
            assert recv(viewer) == embed_frame(subscription, [{"target": target, "embed": second}])
            publish(publisher, None)
            assert recv(viewer) == embed_frame(subscription)


@pytest.mark.parametrize("denied_party", ["publisher", "viewer"])
def test_embeds_omit_all_asset_metadata_after_independent_image_permission_revocation(
    client, db_session, team, surface_team, monkeypatch, denied_party
):
    monkeypatch.setattr(presence, "CHECK_SECONDS", 0.03)
    publisher_name, viewer_name = (
        ("viewer", "owner") if denied_party == "publisher" else ("owner", "viewer")
    )
    with connected(client, team.tokens[publisher_name], protocol_version=2) as (publisher, _):
        publish(publisher, team.location())
        move_embed(publisher, team.location(), embedded_image(surface_team))
        with connected(client, team.tokens[viewer_name], protocol_version=2) as (viewer, _):
            subscription = watch_embeds(viewer, team.location())
            assert len(recv(viewer)["peers"]) == 1
            db_session.add(
                models.ManagedRule(
                    share_id=team.share.id,
                    user_id=team.viewer.id,
                    path=surface_team.image.path,
                    recursive=False,
                    permission="none",
                )
            )
            db_session.commit()
            assert recv(viewer) == embed_frame(subscription)
            # Denying an image must not hide the still-authorized note location.
            actors = list(client.app.state.collaboration_presence.connections.values())
            assert any(
                peer.location == presence.Location.model_validate(team.location())
                for peer in actors
            )
            assert any(peer.watch and peer.watch.subscription_id == subscription for peer in actors)


@pytest.mark.parametrize(
    "change", ["image_generation", "image_bytes", "image_deleted", "note_denied"]
)
def test_embed_delivery_rechecks_note_and_asset_version(
    client, db_session, team, surface_team, monkeypatch, change
):
    monkeypatch.setattr(presence, "CHECK_SECONDS", 0.03)
    with connected(client, team.tokens["owner"], protocol_version=2) as (publisher, _):
        publish(publisher, team.location())
        move_embed(publisher, team.location(), embedded_image(surface_team))
        with connected(client, team.tokens["viewer"], protocol_version=2) as (viewer, _):
            subscription = watch_embeds(viewer, team.location())
            assert len(recv(viewer)["peers"]) == 1
            if change == "image_generation":
                surface_team.image.collaboration_generation = uuid.uuid4()
            elif change == "image_bytes":
                surface_team.image.record = {"type": "image", "hash": "e" * 64}
            elif change == "image_deleted":
                db_session.delete(surface_team.image)
            else:
                team.rule.permission = "none"
            db_session.commit()
            expected = embed_frame(subscription)
            if change == "note_denied":
                expected = {
                    "type": "image_embeds",
                    "subscription_id": subscription,
                    "status": "unavailable",
                }
            assert recv(viewer) == expected


@pytest.mark.parametrize(
    "mode",
    ["hidden", "different_note", "legacy", "asset_denied", "cross_share", "generation", "bytes"],
)
def test_embed_publication_requires_visible_note_and_current_readable_asset(
    client, db_session, team, surface_team, mode
):
    embed = embedded_image(surface_team)
    with connected(
        client, team.tokens["viewer"], protocol_version=1 if mode == "legacy" else 2
    ) as (socket, _):
        if mode != "hidden":
            publish(socket, team.location("second" if mode == "different_note" else "first"))
        if mode == "asset_denied":
            db_session.add(
                models.ManagedRule(
                    share_id=team.share.id,
                    user_id=team.viewer.id,
                    path=surface_team.image.path,
                    recursive=False,
                    permission="none",
                )
            )
            db_session.commit()
        elif mode == "cross_share":
            embed["asset"]["entry_id"] = guid()
        elif mode == "generation":
            embed["asset"]["generation"] = guid()
        elif mode == "bytes":
            embed["asset"]["sha256"] = "f" * 64
        socket.send_json({"type": "image_embed", "location": team.location(), "embed": embed})
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == (
            4400 if mode in ("hidden", "different_note", "legacy") else 4403
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"version": True},
        {"source_sha256": "invalid"},
        {"source": "private source"},
        {"occurrence": {"anchor": {"tname": "contents"}, "head": {"tname": "other"}}},
        {
            "occurrence": {
                "anchor": {"tname": "contents", "item": {"client": True, "clock": 0}},
                "head": {"tname": "contents"},
            }
        },
        {"basis": {"width": True, "height": 10, "orientation": "display"}},
        {"basis": {"width": 1000001, "height": 10, "orientation": "display"}},
        {"state": {"pointer": {"x": 2, "y": 0}, "viewport": None}},
        {"asset": {"kind": "external", "url_sha256": "d" * 64, "version_evidence": "verified"}},
        {
            "asset": {
                "kind": "external",
                "url_sha256": "d" * 64,
                "version_evidence": "unknown",
                "url": "https://private.invalid",
            }
        },
    ],
)
def test_embedded_schema_rejects_unbounded_or_sensitive_fields(patch):
    with pytest.raises(ValueError):
        presence.EmbeddedImage.model_validate(embedded_image(**patch))


def test_external_embed_transport_and_stale_unwatch(client, team):
    embed = embedded_image()
    with connected(client, team.tokens["owner"], protocol_version=2) as (publisher, _):
        publish(publisher, team.location())
        move_embed(publisher, team.location(), embed)
        with connected(client, team.tokens["viewer"], protocol_version=2) as (viewer, _):
            old = watch_embeds(viewer, team.location())
            assert recv(viewer)["peers"][0]["embed"] == embed
            current = watch_embeds(viewer, team.location("second"))
            assert recv(viewer) == embed_frame(current)
            viewer.send_json({"type": "unwatch", "subscription_id": old})
            publish(publisher, team.location("second"))
            move_embed(publisher, team.location("second"), embed)
            assert recv(viewer)["subscription_id"] == current


@pytest.mark.parametrize("change", ["occurrence", "asset", "hidden", "token", "watch"])
def test_embed_semantic_change_while_waiting_for_socket_cannot_leak_checked_asset(
    monkeypatch, change
):
    async def run():
        owner = presence.PresenceBroker()
        viewer, publisher = (
            owner.add("viewer", actor(), guid(), 2),
            owner.add("publisher", actor(), guid(), 2),
        )
        location = presence.Location(
            kind="note", share_id=guid(), entry_id=guid(), surface="editor"
        )
        original = presence.EmbeddedImage.model_validate(embedded_image())
        owner.publish(publisher, location)
        owner.move_embed(publisher, location, original)
        owner.watch(viewer, presence.Watch(guid(), location, embedded_images=True))
        checked, sent = asyncio.Event(), []

        async def authorize(_fn, _token, _location, peers):
            checked.set()
            return [
                {
                    "target": dict(zip(("user_id", "session_id", "instance_id"), key)),
                    "embed": embed.model_dump(mode="json"),
                }
                for key, _, embed in peers
            ]

        async def emit(value):
            sent.append(value)

        monkeypatch.setattr(events, "run_in_threadpool", authorize)
        monkeypatch.setattr(presence, "IMAGE_STATE_INTERVAL", 0)
        await viewer.send_lock.acquire()
        task = asyncio.create_task(
            events.deliver_roster(SimpleNamespace(send_json=emit), owner, viewer, True)
        )
        try:
            await asyncio.wait_for(checked.wait(), 1)
            if change in ("asset", "occurrence"):
                data = original.model_dump(mode="json")
                if change == "asset":
                    data["asset"]["url_sha256"] = "e" * 64
                else:
                    data["occurrence"]["anchor"]["item"]["clock"] = 50
                owner.move_embed(publisher, location, presence.EmbeddedImage.model_validate(data))
            elif change == "hidden":
                owner.publish(publisher, None)
            elif change == "token":
                publisher.token = "replaced"
            else:
                owner.watch(viewer, presence.Watch(guid(), location, embedded_images=True))

            async def denied(*_):
                return []

            monkeypatch.setattr(events, "run_in_threadpool", denied)
            viewer.send_lock.release()
            async with asyncio.timeout(1):
                while not sent:
                    await asyncio.sleep(0.001)
            assert sent == [embed_frame(viewer.watch.subscription_id)]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            owner.remove(publisher)
            owner.remove(viewer)
        assert not owner.rooms and not owner.watchers and publisher.image_embed is None

    asyncio.run(run())


def test_embed_geometry_does_not_starve_authorization_or_trigger_own_observer(monkeypatch):
    async def run():
        owner = presence.PresenceBroker()
        viewer, publisher = (
            owner.add("viewer", actor(), guid(), 2),
            owner.add("publisher", actor(), guid(), 2),
        )
        location = presence.Location(
            kind="note", share_id=guid(), entry_id=guid(), surface="reading"
        )
        owner.publish(publisher, location)
        owner.move_embed(
            publisher, location, presence.EmbeddedImage.model_validate(embedded_image())
        )
        owner.watch(viewer, presence.Watch(guid(), location, embedded_images=True))
        owner.watch(publisher, presence.Watch(guid(), location, embedded_images=True))
        publisher.image_dirty.clear()
        revision, sent = publisher.embed_revision, []

        async def authorize(_fn, _token, _location, peers):
            for x in range(1001):
                owner.move_embed(
                    publisher,
                    location,
                    presence.EmbeddedImage.model_validate(
                        embedded_image(
                            state={"pointer": {"x": x / 1000, "y": 0.5}, "viewport": None}
                        )
                    ),
                )
            return [
                {
                    "target": dict(zip(("user_id", "session_id", "instance_id"), key)),
                    "embed": embed.model_dump(mode="json"),
                }
                for key, _, embed in peers
            ]

        async def emit(value):
            sent.append(value)

        monkeypatch.setattr(events, "run_in_threadpool", authorize)
        monkeypatch.setattr(presence, "IMAGE_STATE_INTERVAL", 0)
        task = asyncio.create_task(
            events.deliver_roster(SimpleNamespace(send_json=emit), owner, viewer, True)
        )
        try:
            async with asyncio.timeout(1):
                while not sent:
                    await asyncio.sleep(0.001)
            assert sent[0]["peers"][0]["embed"]["state"]["pointer"]["x"] == 1
            assert publisher.embed_revision == revision
            assert not publisher.image_dirty.is_set() and not viewer.roster_dirty.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            owner.remove(publisher)
            owner.remove(viewer)

    asyncio.run(run())


def test_image_movement_is_authorized_separate_from_roster_and_clears_on_leave(
    client, team, surface_team
):
    location = surface_team.locations[0]
    state = {
        "pointer": {"x": 0.25, "y": 0.75},
        "viewport": {"x": 0.1, "y": 0.2, "width": 0.5, "height": 0.4},
    }
    # A read-only reviewer can point; the owner observes while hidden.
    with connected(client, team.tokens["viewer"], protocol_version=2) as (publisher, ready):
        publish(publisher, location)
        with connected(client, team.tokens["owner"], protocol_version=2) as (viewer, _):
            subscription = moving_watch(viewer, location)
            move_image(publisher, location, state)
            assert recv(viewer) == {
                "type": "image_states",
                "subscription_id": subscription,
                "status": "available",
                "peers": [
                    {
                        "target": {
                            "user_id": str(team.viewer.id),
                            "session_id": ready["session_id"],
                            "instance_id": ready["instance_id"],
                        },
                        "state": state,
                    }
                ],
            }
            move_image(publisher, location, None)
            assert recv(viewer) == {
                "type": "image_states",
                "subscription_id": subscription,
                "status": "available",
                "peers": [],
            }
            move_image(publisher, location, state)
            assert recv(viewer)["type"] == "image_states"
            publish(publisher, None)
            cleared = [recv(viewer), recv(viewer)]
            assert {value["type"] for value in cleared} == {"roster", "image_states"}
            assert all(value["peers"] == [] for value in cleared)


@pytest.mark.parametrize(
    "state",
    [
        {"pointer": {"x": -0.1, "y": 0}, "viewport": None},
        {"pointer": {"x": 1.1, "y": 0}, "viewport": None},
        {"pointer": {"x": True, "y": 0}, "viewport": None},
        {"pointer": {"x": "0.5", "y": 0}, "viewport": None},
        {"pointer": {"x": float("nan"), "y": 0}, "viewport": None},
        {"pointer": {"x": 0, "y": 0, "user_id": "spoof"}, "viewport": None},
        {"pointer": None, "viewport": {"x": 0.8, "y": 0, "width": 0.3, "height": 1}},
        {"pointer": None, "viewport": {"x": 0, "y": 0, "width": 0, "height": 1}},
        {"pointer": None},
    ],
)
def test_image_movement_rejects_invalid_geometry_and_identity_fields(
    client, team, surface_team, state
):
    location = surface_team.locations[0]
    with connected(client, team.tokens["owner"], protocol_version=2) as (socket, _):
        publish(socket, location)
        socket.send_json({"type": "image_state", "location": location, "state": state})
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == 4400


@pytest.mark.parametrize("mode", ["hidden", "note", "revoked", "version", "legacy"])
def test_image_movement_requires_current_visible_image_and_read_access(
    client, db_session, team, surface_team, mode
):
    location = surface_team.locations[0]
    with connected(
        client, team.tokens["viewer"], protocol_version=1 if mode == "legacy" else 2
    ) as (socket, _):
        if mode not in ("hidden", "legacy"):
            publish(socket, team.location() if mode == "note" else location)
        if mode == "revoked":
            team.rule.permission = "none"
            db_session.commit()
        if mode == "version":
            location = {**location, "generation": guid()}
        socket.send_json(
            {
                "type": "image_state",
                "location": location,
                "state": {"pointer": {"x": 0.2, "y": 0.5}, "viewport": None},
            }
        )
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == (4403 if mode in ("revoked", "version") else 4400)


def test_old_surface_watch_never_receives_unsolicited_movement(client, team, surface_team):
    location = surface_team.locations[0]
    with connected(client, team.tokens["owner"], protocol_version=2) as (publisher, _):
        publish(publisher, location)
        with connected(client, team.tokens["viewer"], protocol_version=2) as (viewer, _):
            watch(viewer, location)
            assert recv(viewer)["type"] == "roster"
            move_image(publisher, location, {"pointer": {"x": 0.2, "y": 0.5}, "viewport": None})
            viewer.send_json({"type": "ping"})
            assert recv(viewer) == {"type": "pong"}


def test_image_context_requires_only_read_access_and_returns_current_version(
    client, db_session, team, surface_team, monkeypatch
):
    for setting in (
        "collaboration_policy_enabled",
        "collaboration_discussions_enabled",
        "collaboration_image_files_enabled",
    ):
        monkeypatch.setattr(get_settings(), setting, False)
    response = image_context(client, team, surface_team.image)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"protocol_version": 1, "location": surface_team.locations[0]}
    surface_team.image.record = {"type": "image", "hash": "d" * 64}
    surface_team.image.collaboration_generation = uuid.uuid4()
    db_session.commit()
    fresh = image_context(client, team, surface_team.image)
    assert fresh.status_code == 200
    assert fresh.json()["location"] == {
        **surface_team.locations[0],
        "content_sha256": "d" * 64,
        "generation": str(surface_team.image.collaboration_generation),
    }


@pytest.mark.parametrize("loss", ["permission", "membership", "session", "deleted", "type", "hash"])
def test_image_context_rechecks_access_and_live_image_metadata(
    client, db_session, team, surface_team, loss
):
    assert image_context(client, team, surface_team.image).status_code == 200
    if loss == "permission":
        team.rule.permission = "none"
    elif loss == "membership":
        db_session.delete(team.member)
    elif loss == "session":
        db_session.delete(team.sessions["viewer"])
    elif loss == "deleted":
        surface_team.image.deleted = True
    else:
        surface_team.image.record = {"type": "markdown"} if loss == "type" else {"type": "image"}
    db_session.commit()
    response = image_context(client, team, surface_team.image)
    assert response.status_code in (401, 403, 404), response.text
    assert "location" not in response.json()
    assert surface_team.image.path not in response.text


@pytest.mark.parametrize("credential", ["missing", "invalid", "sessionless", "scoped"])
@pytest.mark.parametrize("endpoint", ["image-location", "base-context", "base-record-context"])
def test_surface_context_requires_normal_session_credentials(
    client, team, surface_team, credential, endpoint
):
    claims = security.decode_access_token(team.tokens["viewer"])
    claims["scope"] = "managed"
    token = {
        "invalid": "invalid",
        "sessionless": security.create_access_token(str(team.viewer.id)),
        "scoped": jwt.encode(
            claims, get_settings().jwt_secret, algorithm=get_settings().jwt_algorithm
        ),
        "missing": None,
    }[credential]
    response = client.get(
        "/v1/collaboration/" + endpoint,
        params={
            "share_id": str(team.share.id),
            "entry_id": surface_team.image.id
            if endpoint == "image-location"
            else surface_team.base.id,
        },
        headers={"Authorization": "Bearer " + token} if token else {},
    )
    assert response.status_code in (401, 403), response.text
    assert "location" not in response.json()


def test_image_context_is_disabled_with_presence_and_rejects_non_image(
    client, team, surface_team, monkeypatch
):
    assert image_context(client, team, team.notes["first"]).status_code == 403
    assert image_context(client, team, surface_team.base).status_code == 403
    monkeypatch.setattr(get_settings(), "collaboration_presence_enabled", False)
    assert image_context(client, team, surface_team.image).status_code == 404


@pytest.mark.parametrize("index", [0, 1])
def test_legacy_follower_gets_no_new_surface_identifiers_and_does_not_resume(
    client, team, surface_team, index
):
    with connected(client, team.tokens["owner"], protocol_version=2) as (publisher, ready):
        with connected(client, team.tokens["viewer"]) as (viewer, _):
            publish(publisher, team.location())
            subscription = follow(viewer, team.owner.id, ready, team.location())
            assert recv(viewer)["type"] == "following"
            assert recv(viewer) == available(subscription, team.location())
            publish(publisher, surface_team.locations[index])
            assert recv(viewer) == unavailable(subscription)
            publish(publisher, team.location())
            viewer.send_json({"type": "ping"})
            assert recv(viewer) == {"type": "pong"}


@pytest.mark.parametrize("index", [0, 1])
def test_v1_publisher_cannot_send_new_surface_locations(client, team, surface_team, index):
    with connected(client, team.tokens["owner"]) as (socket, _):
        socket.send_json({"type": "presence", "location": surface_team.locations[index]})
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == 4400


@pytest.mark.parametrize("index", [0, 1])
def test_initial_surface_follow_requires_same_image_version_or_saved_view(
    client, team, surface_team, index
):
    location = surface_team.locations[index]
    wrong = dict(location)
    if index == 0:
        wrong["content_sha256"] = "c" * 64
    else:
        wrong = surface_team.locations[2]
    with connected(client, team.tokens["owner"], protocol_version=2) as (publisher, ready):
        with connected(client, team.tokens["viewer"], protocol_version=2) as (viewer, _):
            publish(publisher, location)
            subscription = follow(viewer, team.owner.id, ready, wrong)
            assert recv(viewer) == unavailable(subscription)
            subscription = follow(viewer, team.owner.id, ready, location)
            assert recv(viewer)["type"] == "following"
            assert recv(viewer) == available(subscription, location)


@pytest.mark.parametrize(
    "index,loss",
    [
        (0, "image-bytes"),
        (0, "generation"),
        (0, "deleted"),
        (0, "permission"),
        (1, "generation"),
        (1, "base-bytes"),
        (1, "revision"),
        (1, "view-state"),
        (1, "view-owner"),
        (1, "disabled"),
        (1, "permission"),
        (1, "deleted"),
    ],
)
def test_idle_surface_follow_rechecks_versions_identity_access_and_feature_state(
    client, db_session, team, surface_team, monkeypatch, index, loss
):
    monkeypatch.setattr(presence, "CHECK_SECONDS", 0.05)
    location = surface_team.locations[index]
    entry = surface_team.image if index == 0 else surface_team.base
    with connected(client, team.tokens["owner"], protocol_version=2) as (publisher, ready):
        with connected(client, team.tokens["viewer"], protocol_version=2) as (viewer, _):
            publish(publisher, location)
            subscription = follow(viewer, team.owner.id, ready, location)
            assert recv(viewer)["type"] == "following"
            assert recv(viewer) == available(subscription, location)
            if loss in {"image-bytes", "base-bytes"}:
                entry.record = {**entry.record, "hash": "c" * 64}
            elif loss == "generation":
                entry.collaboration_generation = uuid.uuid4()
            elif loss == "deleted":
                entry.deleted = True
            elif loss == "permission":
                team.rule.permission = "none"
            elif loss == "revision":
                surface_team.definition.revision += 1
            elif loss == "view-state":
                surface_team.views[0].state = "unmatched"
            elif loss == "view-owner":
                # A view from another definition is never interchangeable.
                other = models.BaseDefinition(
                    entry_id=surface_team.image.id,
                    generation=uuid.uuid4(),
                    revision=1,
                    content_sha256="c" * 64,
                    configuration={},
                )
                db_session.add(other)
                db_session.flush()
                surface_team.views[0].definition_id = other.id
            elif loss == "disabled":
                monkeypatch.setattr(get_settings(), "collaboration_bases_registry_enabled", False)
            db_session.commit()
            assert recv(viewer) == unavailable(subscription)


@pytest.mark.parametrize(
    "index,field,value",
    [
        (0, "content_sha256", "c" * 64),
        (0, "generation", "new-id"),
        (0, "entry_id", "new-id"),
        (1, "view_id", "new-id"),
        (1, "definition_id", "new-id"),
        (1, "generation", "new-id"),
        (1, "registry_revision", 2),
    ],
)
def test_surface_publication_rejects_unverified_identity_or_version(
    client, team, surface_team, index, field, value
):
    location = {**surface_team.locations[index], field: guid() if value == "new-id" else value}
    with connected(client, team.tokens["owner"], protocol_version=2) as (socket, _):
        socket.send_json({"type": "presence", "location": location})
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == 4403


@pytest.mark.parametrize(
    "patch",
    [
        {"registry_revision": True},
        {"registry_revision": "1"},
        {"registry_revision": 1.0},
        {"registry_revision": 9007199254740992},
        {"path": "Private.md"},
        {"value": "computed result"},
        {"pointer": {"x": 0.5}},
    ],
)
def test_surface_schema_rejects_unnegotiated_targets_and_values(client, team, surface_team, patch):
    with connected(client, team.tokens["owner"], protocol_version=2) as (socket, _):
        socket.send_json({"type": "presence", "location": {**surface_team.locations[1], **patch}})
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == 4400 and not error.value.reason


@pytest.mark.parametrize("version", [True, 2.0, "2", 0, 3, None])
def test_authenticate_rejects_invalid_protocol_versions(client, team, version):
    with client.websocket_connect(PATH) as socket:
        socket.send_json(
            {
                "type": "authenticate",
                "ticket": ticket(client, team.tokens["owner"]),
                "instance_id": guid(),
                "protocol_version": version,
            }
        )
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == 4400


def test_v2_refresh_preserves_protocol_and_cannot_downgrade(client, team, surface_team):
    with connected(client, team.tokens["owner"], protocol_version=2) as (socket, ready):
        message = {
            "type": "authenticate",
            "instance_id": ready["instance_id"],
            "ticket": ticket(client, team.tokens["owner"]),
        }
        socket.send_json(message)
        assert recv(socket)["type"] == "authenticated"
        publish(socket, surface_team.locations[0])
        socket.send_json(
            {
                **message,
                "ticket": ticket(client, team.tokens["owner"]),
                "protocol_version": 1,
            }
        )
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == 4400


def actor():
    return presence.Actor(PresenceIdentity(guid(), "Member", "a" * 32), int(time.time()) + 60)


def watch(socket, location, subscription=None):
    subscription = subscription or guid()
    socket.send_json({"type": "watch", "subscription_id": subscription, "location": location})
    return subscription


def roster(subscription, peers=None):
    return {
        "type": "roster",
        "subscription_id": subscription,
        "status": "available",
        "peers": peers or [],
    }


@pytest.mark.parametrize("index", [0, 1])
def test_surface_roster_observation_is_private_and_authenticates_names(
    client, team, surface_team, index
):
    location = surface_team.locations[index]
    with connected(client, team.tokens["owner"], protocol_version=2) as (publisher, ready):
        with connected(client, team.tokens["viewer"], protocol_version=2) as (viewer, viewer_ready):
            subscription = watch(viewer, location)
            assert recv(viewer) == roster(subscription)
            # Observing does not publish the viewer. The publisher sees nobody.
            publisher_subscription = watch(publisher, location)
            assert recv(publisher) == roster(publisher_subscription)
            publish(publisher, location)
            peers = [
                {
                    "target": {
                        "user_id": str(team.owner.id),
                        "session_id": ready["session_id"],
                        "instance_id": ready["instance_id"],
                    },
                    "name": team.owner.email.split("@")[0],
                }
            ]
            assert recv(viewer) == roster(subscription, peers)
            # Roster identity is immediately usable for the existing Follow flow.
            follow_id = follow(viewer, team.owner.id, ready, location)
            assert recv(viewer)["type"] == "following"
            assert recv(viewer) == available(follow_id, location)
            viewer.send_json({"type": "unfollow", "subscription_id": follow_id})
            viewer.send_json({"type": "ping"})
            assert recv(viewer) == {"type": "pong"}
            publish(publisher, None)
            assert recv(viewer) == roster(subscription)
    owner = client.app.state.collaboration_presence
    assert not owner.rooms and not owner.watchers and not owner.followers


def test_surface_roster_does_not_cross_saved_views_and_stale_unwatch_is_ignored(
    client, team, surface_team
):
    first, second = surface_team.locations[1:]
    with connected(client, team.tokens["owner"], protocol_version=2) as (publisher, _):
        with connected(client, team.tokens["viewer"], protocol_version=2) as (viewer, _):
            publish(publisher, second)
            old = watch(viewer, first)
            assert recv(viewer) == roster(old)
            current = watch(viewer, second)
            assert len(recv(viewer)["peers"]) == 1
            viewer.send_json({"type": "unwatch", "subscription_id": old})
            publish(publisher, first)
            assert recv(viewer) == roster(current)
            viewer.send_json({"type": "unwatch", "subscription_id": current})
            viewer.send_json({"type": "ping"})
            assert recv(viewer) == {"type": "pong"}
            assert not client.app.state.collaboration_presence.watchers


@pytest.mark.parametrize(
    "loss", ["viewer-access", "publisher-access", "image-version", "disconnect"]
)
def test_surface_roster_clears_idle_revocation_replacement_and_departure(
    client, db_session, team, surface_team, monkeypatch, loss
):
    monkeypatch.setattr(presence, "CHECK_SECONDS", 0.05)
    location = surface_team.locations[0]
    # The limited member publishes; the owner observes, except for viewer loss.
    publisher_token = team.tokens["owner" if loss == "viewer-access" else "viewer"]
    viewer_token = team.tokens["viewer" if loss == "viewer-access" else "owner"]
    with connected(client, viewer_token, protocol_version=2) as (viewer, _):
        with connected(client, publisher_token, protocol_version=2) as (publisher, _):
            publish(publisher, location)
            subscription = watch(viewer, location)
            assert len(recv(viewer)["peers"]) == 1
            if loss in {"viewer-access", "publisher-access"}:
                team.rule.permission = "none"
            elif loss == "image-version":
                surface_team.image.record = {"type": "image", "hash": "c" * 64}
            db_session.commit()
            if loss != "disconnect":
                result = recv(viewer)
                assert result == (
                    roster(subscription)
                    if loss == "publisher-access"
                    else {
                        "type": "roster",
                        "subscription_id": subscription,
                        "status": "unavailable",
                    }
                )
        if loss == "disconnect":
            assert recv(viewer) == roster(subscription)


@pytest.mark.parametrize("version,note", [(1, False), (2, True)])
def test_surface_watch_rejects_legacy_protocol_and_note_rooms(
    client, team, surface_team, version, note
):
    with connected(client, team.tokens["owner"], protocol_version=version) as (socket, _):
        watch(socket, team.location() if note else surface_team.locations[0])
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == 4400


def test_roster_snapshot_changed_while_waiting_for_socket_cannot_be_sent(monkeypatch):
    async def run():
        owner = presence.PresenceBroker()
        viewer, publisher = (
            owner.add("viewer", actor(), guid(), 2),
            owner.add("publisher", actor(), guid(), 2),
        )
        location = presence.ImageLocation(
            kind="image",
            share_id=guid(),
            entry_id=guid(),
            generation=guid(),
            content_sha256="a" * 64,
        )
        owner.publish(publisher, location)
        owner.watch(viewer, presence.Watch(guid(), location))
        captured = asyncio.Event()

        async def authorize(*_):
            captured.set()
            return [{"name": "Old participant"}]

        monkeypatch.setattr(events, "run_in_threadpool", authorize)
        sent = []

        async def emit(value):
            sent.append(value)

        await viewer.send_lock.acquire()
        task = asyncio.create_task(
            events.deliver_roster(SimpleNamespace(send_json=emit), owner, viewer)
        )
        try:
            await asyncio.wait_for(captured.wait(), 1)
            # Publication changed after database checks but before the socket
            # lock is available. Old participant data must not escape.
            owner.publish(publisher, None)

            async def empty(*_):
                return []

            monkeypatch.setattr(events, "run_in_threadpool", empty)
            viewer.send_lock.release()
            for _ in range(20):
                if sent:
                    break
                await asyncio.sleep(0.01)
            assert sent == [roster(viewer.watch.subscription_id)]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            owner.remove(publisher)
            owner.remove(viewer)

    asyncio.run(run())


@pytest.mark.parametrize("change", ["hidden", "watch", "token"])
def test_image_movement_checked_before_socket_wait_cannot_escape_changed_context(
    monkeypatch, change
):
    async def run():
        owner = presence.PresenceBroker()
        viewer, publisher = (
            owner.add("viewer", actor(), guid(), 2),
            owner.add("publisher", actor(), guid(), 2),
        )
        location = presence.ImageLocation(
            kind="image",
            share_id=guid(),
            entry_id=guid(),
            generation=guid(),
            content_sha256="a" * 64,
        )
        owner.publish(publisher, location)
        owner.move(
            publisher, location, presence.ImageState(pointer={"x": 0.2, "y": 0.3}, viewport=None)
        )
        watch = presence.Watch(guid(), location, True)
        owner.watch(viewer, watch)
        checked, sent = asyncio.Event(), []

        async def authorize(*_):
            checked.set()
            return [
                {
                    "target": {
                        "user_id": publisher.key[0],
                        "session_id": publisher.key[1],
                        "instance_id": publisher.key[2],
                    }
                }
            ]

        async def emit(value):
            sent.append(value)

        monkeypatch.setattr(events, "run_in_threadpool", authorize)
        monkeypatch.setattr(presence, "IMAGE_STATE_INTERVAL", 0)
        await viewer.send_lock.acquire()
        task = asyncio.create_task(
            events.deliver_roster(SimpleNamespace(send_json=emit), owner, viewer, True)
        )
        try:
            await asyncio.wait_for(checked.wait(), 1)
            if change == "hidden":
                owner.publish(publisher, None)
            elif change == "watch":
                owner.watch(viewer, presence.Watch(guid(), location, True))
            else:
                viewer.token = "replaced"

            async def empty(*_):
                return []

            monkeypatch.setattr(events, "run_in_threadpool", empty)
            viewer.send_lock.release()
            async with asyncio.timeout(1):
                while not sent:
                    await asyncio.sleep(0.001)
            assert all(value["type"] == "image_states" and value["peers"] == [] for value in sent)
            assert all(value["subscription_id"] == viewer.watch.subscription_id for value in sent)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            owner.remove(publisher)
            owner.remove(viewer)

    asyncio.run(run())


def test_image_movement_slow_consumer_keeps_only_latest_state_and_no_roster_work(monkeypatch):
    async def run():
        owner = presence.PresenceBroker()
        viewer, publisher = (
            owner.add("viewer", actor(), guid(), 2),
            owner.add("publisher", actor(), guid(), 2),
        )
        location = presence.ImageLocation(
            kind="image",
            share_id=guid(),
            entry_id=guid(),
            generation=guid(),
            content_sha256="a" * 64,
        )
        owner.publish(publisher, location)
        owner.watch(viewer, presence.Watch(guid(), location, True))
        owner.watch(publisher, presence.Watch(guid(), location, True))
        publisher.image_dirty.clear()
        viewer.roster_dirty.clear()
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        sent = []

        async def authorize(*_):
            return [
                {
                    "target": {
                        "user_id": publisher.key[0],
                        "session_id": publisher.key[1],
                        "instance_id": publisher.key[2],
                    }
                }
            ]

        async def emit(value):
            sent.append(value)
            if len(sent) == 1:
                started.set()
                await release.wait()
            else:
                finished.set()

        monkeypatch.setattr(events, "run_in_threadpool", authorize)
        monkeypatch.setattr(presence, "IMAGE_STATE_INTERVAL", 0)
        task = asyncio.create_task(
            events.deliver_roster(SimpleNamespace(send_json=emit), owner, viewer, True)
        )
        try:
            await asyncio.wait_for(started.wait(), 1)
            for x in range(1001):
                owner.move(
                    publisher,
                    location,
                    presence.ImageState(pointer={"x": x / 1000, "y": 0.5}, viewport=None),
                )
            assert (
                not viewer.roster_dirty.is_set()
                and not viewer.controls
                and viewer.image_dirty.is_set()
            )
            assert not publisher.image_dirty.is_set()  # Own pointer is omitted.
            release.set()
            await asyncio.wait_for(finished.wait(), 1)
            assert len(sent) == 2 and sent[-1]["peers"][0]["state"]["pointer"] == {"x": 1, "y": 0.5}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            owner.remove(publisher)
            owner.remove(viewer)
        assert publisher.image_state is None and not owner.watchers and not owner.connections

    asyncio.run(run())


def test_roster_slow_consumer_coalesces_changes_and_releases_room_ownership(monkeypatch):
    async def run():
        owner = presence.PresenceBroker()
        viewer, publisher = (
            owner.add("viewer", actor(), guid(), 2),
            owner.add("publisher", actor(), guid(), 2),
        )
        location = presence.ImageLocation(
            kind="image",
            share_id=guid(),
            entry_id=guid(),
            generation=guid(),
            content_sha256="a" * 64,
        )
        owner.publish(publisher, location)
        watch = presence.Watch(guid(), location)
        owner.watch(viewer, watch)

        async def authorize(_fn, _token, _location, peers):
            return [{"name": "Member", "target": key} for key, _ in peers]

        monkeypatch.setattr(events, "run_in_threadpool", authorize)
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        sent = []

        async def emit(value):
            sent.append(value)
            if len(sent) == 1:
                started.set()
                await release.wait()
            else:
                finished.set()

        task = asyncio.create_task(
            events.deliver_roster(SimpleNamespace(send_json=emit), owner, viewer)
        )
        try:
            await asyncio.wait_for(started.wait(), 1)
            for _ in range(1000):
                owner.publish(publisher, None)
                owner.publish(publisher, location)
            owner.publish(publisher, None)
            assert not viewer.controls and viewer.roster_dirty.is_set()
            assert len(owner.watchers) == 1 and not owner.rooms
            release.set()
            await asyncio.wait_for(finished.wait(), 1)
            assert len(sent) == 2 and sent[-1] == roster(watch.subscription_id)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            owner.remove(publisher)
            owner.remove(viewer)
        assert not owner.watchers and not owner.rooms and not owner.connections

    asyncio.run(run())


def test_sent_roster_denial_cannot_cancel_a_new_observation(monkeypatch):
    async def run():
        owner = presence.PresenceBroker()
        viewer = owner.add("viewer", actor(), guid(), 2)
        location = presence.ImageLocation(
            kind="image",
            share_id=guid(),
            entry_id=guid(),
            generation=guid(),
            content_sha256="a" * 64,
        )
        first, replacement = presence.Watch(guid(), location), presence.Watch(guid(), location)
        owner.watch(viewer, first)
        retried = asyncio.Event()
        count = 0

        async def authorize(*_):
            nonlocal count
            count += 1
            if count == 1:
                raise HTTPException(403)
            retried.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(events, "run_in_threadpool", authorize)

        async def emit(value):
            assert (
                value["subscription_id"] == first.subscription_id
                and value["status"] == "unavailable"
            )
            # The user switches the observation while the prior write awaits.
            owner.watch(viewer, replacement)

        task = asyncio.create_task(
            events.deliver_roster(SimpleNamespace(send_json=emit), owner, viewer)
        )
        try:
            await asyncio.wait_for(retried.wait(), 1)
            assert viewer.watch == replacement and viewer.watch.delivery.last_roster is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            owner.remove(viewer)

    asyncio.run(run())


def test_rate_control_queue_and_connection_limits_are_bounded(monkeypatch):
    owner = presence.PresenceBroker()
    connection = owner.add("token", actor(), guid())
    monkeypatch.setattr(presence.time, "monotonic", lambda: connection.allowance_time)
    assert all(connection.permit_message() for _ in range(20))
    assert not connection.permit_message()
    for i in range(16):
        connection.control({"n": i})
    with pytest.raises(OverflowError):
        connection.control({"n": 17})
    monkeypatch.setattr(presence, "MAX_CONNECTIONS", 1)
    with pytest.raises(OverflowError):
        owner.add("another", actor(), guid())
    owner.remove(connection)
    assert not owner.connections


def test_changed_location_is_rechecked_after_slow_authorization(monkeypatch):
    async def scenario():
        owner = presence.PresenceBroker()
        publisher = owner.add("publisher", actor(), guid())
        viewer = owner.add("viewer", actor(), guid())
        location = presence.Location(
            kind="note", share_id=guid(), entry_id=guid(), surface="editor"
        )
        replacement = location.model_copy(update={"entry_id": uuid.uuid4()})
        owner.publish(publisher, location)
        owner.subscribe(viewer, presence.Follow(guid(), publisher.key))
        checked, sent = [], []
        delivered = asyncio.Event()

        async def authorize(_fn, _viewer, _publisher, target):
            checked.append(target)
            if len(checked) == 1:
                # Models movement while a database check is in flight.
                owner.publish(publisher, replacement)

        async def send(message):
            sent.append(message)
            delivered.set()

        monkeypatch.setattr(events, "run_in_threadpool", authorize)
        task = asyncio.create_task(events.deliver(SimpleNamespace(send_json=send), owner, viewer))
        try:
            await asyncio.wait_for(delivered.wait(), 1)
            assert checked == [location, replacement]
            assert sent == [
                available(viewer.follow.subscription_id, replacement.model_dump(mode="json"))
            ]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_delayed_denial_of_old_location_cannot_withdraw_replacement(monkeypatch):
    async def scenario():
        owner = presence.PresenceBroker()
        connection = owner.add("token", actor(), guid())
        location = presence.Location(
            kind="note", share_id=guid(), entry_id=guid(), surface="editor"
        )
        replacement = location.model_copy(update={"entry_id": uuid.uuid4()})
        owner.publish(connection, location)
        done = asyncio.Event()

        async def authenticate(_fn, _token, target=None):
            if target:
                assert target == location
                owner.publish(connection, replacement)
                done.set()
                raise HTTPException(403, "Previous location revoked")
            return connection.actor

        monkeypatch.setattr(events, "run_in_threadpool", authenticate)
        monkeypatch.setattr(presence, "CHECK_SECONDS", 0.01)
        monkeypatch.setattr(events, "enabled", lambda: True)
        task = asyncio.create_task(events.check_live(owner, connection))
        try:
            await asyncio.wait_for(done.wait(), 1)
            assert connection.location == replacement
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_slow_viewer_receives_latest_location_without_accumulating_movements(monkeypatch):
    async def scenario():
        owner = presence.PresenceBroker()
        publisher = owner.add("publisher", actor(), guid())
        viewer = owner.add("viewer", actor(), guid())
        location = presence.Location(
            kind="note", share_id=guid(), entry_id=guid(), surface="editor"
        )
        owner.publish(publisher, location)
        owner.subscribe(viewer, presence.Follow(guid(), publisher.key))
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        sent = []

        async def authorize(*_args):
            pass

        async def send(message):
            sent.append(message)
            if len(sent) == 1:
                entered.set()
                await release.wait()
            else:
                finished.set()

        monkeypatch.setattr(events, "run_in_threadpool", authorize)
        task = asyncio.create_task(events.deliver(SimpleNamespace(send_json=send), owner, viewer))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            for _ in range(1000):
                latest = location.model_copy(update={"entry_id": uuid.uuid4()})
                owner.publish(publisher, latest)
            release.set()
            await asyncio.wait_for(finished.wait(), 1)
            assert len(sent) == 2
            assert sent[-1] == available(
                viewer.follow.subscription_id, latest.model_dump(mode="json")
            )
            assert not viewer.controls
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def base_position(record=None, *, kind="cell", source="note", name="status", x=0.5):
    target = {"type": kind}
    if kind != "property":
        target.update(
            record_id=record.id if record else guid(),
            record_generation=str(record.collaboration_generation) if record else guid(),
        )
    if kind != "item":
        target["property"] = {"source": source, "name": name}
    return {"target": target, "offset": {"x": x, "y": 0.5}}


def base_state(record=None, **changes):
    return {"version": 1, "pointer": base_position(record), "viewport": None, **changes}


def move_base(socket, location, state):
    socket.send_json({"type": "base_state", "location": location, "state": state})
    socket.send_json({"type": "ping"})
    assert recv(socket) == {"type": "pong"}


def watch_base(socket, location, version=1):
    subscription = guid()
    socket.send_json(
        {
            "type": "watch",
            "subscription_id": subscription,
            "location": location,
            "base_states": True,
            **({"base_target_version": version} if version != 1 else {}),
        }
    )
    initial = [recv(socket), recv(socket)]
    assert {value["type"] for value in initial} == {"roster", "base_states"}
    assert all(value["status"] == "available" for value in initial)
    return subscription


def test_base_positions_include_cells_records_headers_without_roster_churn(
    client, team, surface_team, monkeypatch
):
    monkeypatch.setattr(get_settings(), "collaboration_discussions_enabled", False)
    location = surface_team.locations[1]
    with connected(client, team.tokens["viewer"], protocol_version=2) as (publisher, ready):
        publish(publisher, location)
        with connected(client, team.tokens["owner"], protocol_version=2) as (viewer, _):
            subscription = watch_base(viewer, location)
            for kind in ("cell", "item", "property"):
                state = base_state(pointer=base_position(team.notes["first"], kind=kind))
                move_base(publisher, location, state)
                assert recv(viewer) == {
                    "type": "base_states",
                    "subscription_id": subscription,
                    "status": "available",
                    "peers": [
                        {
                            "target": {
                                "user_id": str(team.viewer.id),
                                "session_id": ready["session_id"],
                                "instance_id": ready["instance_id"],
                            },
                            "state": state,
                        }
                    ],
                }
            move_base(publisher, location, None)
            assert recv(viewer)["peers"] == []
            move_base(publisher, location, state)
            assert recv(viewer)["peers"]
            publish(publisher, surface_team.locations[2])
            cleared = [recv(viewer), recv(viewer)]
            assert {value["type"] for value in cleared} == {"roster", "base_states"}
            assert all(not value["peers"] for value in cleared)


@pytest.mark.parametrize("party", ["publisher", "viewer"])
def test_base_position_idle_record_revocation_preserves_view_roster(
    client, db_session, team, surface_team, monkeypatch, party
):
    monkeypatch.setattr(presence, "CHECK_SECONDS", 0.04)
    publisher_name, viewer_name = (
        ("viewer", "owner") if party == "publisher" else ("owner", "viewer")
    )
    location = surface_team.locations[1]
    with connected(client, team.tokens[publisher_name], protocol_version=2) as (publisher, _):
        publish(publisher, location)
        with connected(client, team.tokens[viewer_name], protocol_version=2) as (viewer, _):
            watch_base(viewer, location)
            move_base(publisher, location, base_state(team.notes["first"]))
            assert recv(viewer)["peers"]
            db_session.add(
                models.ManagedRule(
                    share_id=team.share.id,
                    user_id=team.viewer.id,
                    path="First.md",
                    recursive=False,
                    permission="none",
                )
            )
            db_session.commit()
            result = recv(viewer)
            assert (
                result["type"] == "base_states"
                and result["status"] == "available"
                and result["peers"] == []
            )
            # The participant is still authorized to read the saved Base view.
            watch(viewer, location)
            assert len(recv(viewer)["peers"]) == 1
            viewer.send_json({"type": "ping"})
            assert recv(viewer) == {"type": "pong"}


def test_base_snapshot_omits_restricted_viewport_and_pointer_metadata(client, team, surface_team):
    location = presence.BaseLocation.model_validate(surface_team.locations[1])
    publisher = presence.authenticate(team.tokens["owner"])
    for field in ("pointer", "viewport"):
        state = presence.BaseState.model_validate(
            base_state(team.notes["first"], **{field: base_position(team.notes["private"])})
        )
        assert (
            presence.base_state_snapshot(
                team.tokens["viewer"],
                location,
                [(publisher.key(guid()), team.tokens["owner"], state)],
            )
            == []
        )


@pytest.mark.parametrize("change", ["generation", "deleted", "type", "private", "view", "hidden"])
def test_base_publication_rejects_stale_or_unreadable_record(
    client, db_session, team, surface_team, change
):
    location = surface_team.locations[1]
    state = base_state(team.notes["first"])
    if change == "generation":
        state["pointer"]["target"]["record_generation"] = guid()
    elif change in ("deleted", "type"):
        entry = team.notes["first"]
        if change == "deleted":
            entry.deleted = True
        else:
            entry.record = {"type": "image"}  # A note-property cell cannot refer to an image.
        db_session.commit()
    elif change == "private":
        state = base_state(team.notes["private"])
    with connected(client, team.tokens["viewer"], protocol_version=2) as (publisher, _):
        if change != "hidden":
            publish(publisher, location)
        publisher.send_json(
            {
                "type": "base_state",
                "location": surface_team.locations[2] if change == "view" else location,
                "state": state,
            }
        )
        with pytest.raises(WebSocketDisconnect) as error:
            recv(publisher)
        assert error.value.code == (4400 if change in ("view", "hidden") else 4403)


def test_base_record_context_is_read_only_and_never_repairs_lineage(
    client, db_session, engine, team, monkeypatch
):
    monkeypatch.setattr(get_settings(), "collaboration_discussions_enabled", False)
    monkeypatch.setattr(get_settings(), "collaboration_policy_enabled", True)
    monkeypatch.setattr(get_settings(), "collaboration_bases_registry_enabled", True)
    entry = team.notes["first"]
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    def read():
        return client.get(
            "/v1/collaboration/base-record-context",
            params={"share_id": str(team.share.id), "entry_id": entry.id},
            headers={"Authorization": "Bearer " + team.tokens["viewer"]},
        )

    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = read()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "protocol_version": 1,
        "share_id": str(team.share.id),
        "entry_id": entry.id,
        "generation": str(entry.collaboration_generation),
        "type": "markdown",
    }
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    entry.collaboration_generation = None
    db_session.commit()
    assert read().status_code == 403
    db_session.refresh(entry)
    assert entry.collaboration_generation is None
    entry.collaboration_generation = uuid.uuid4()
    db_session.commit()
    assert read().json()["generation"] == str(entry.collaboration_generation)
    entry.deleted = True
    db_session.commit()
    assert read().status_code in (403, 404)


@pytest.mark.parametrize(
    "setting",
    [
        "collaboration_presence_enabled",
        "collaboration_policy_enabled",
        "collaboration_bases_registry_enabled",
    ],
)
def test_base_position_discovery_feature_gate(client, team, surface_team, monkeypatch, setting):
    monkeypatch.setattr(get_settings(), setting, False)
    response = client.get(
        "/v1/collaboration/base-record-context",
        params={"share_id": str(team.share.id), "entry_id": team.notes["first"].id},
        headers={"Authorization": "Bearer " + team.tokens["viewer"]},
    )
    assert response.status_code == 404
    features = client.get(
        "/v1/collaboration/capabilities",
        params={"share_id": str(team.share.id)},
        headers={"Authorization": "Bearer " + team.tokens["viewer"]},
    ).json()["features"]
    assert features["presence_base_states_preview"] is False
    assert features["presence_base_targets_v2_preview"] is False


@pytest.mark.parametrize(
    "patch",
    [
        {"version": True},
        {"version": 4},
        {"path": "private"},
        {"pointer": {}},
        {"viewport": {"x": 1}},
        *[
            {"pointer": {"target": base_position()["target"], "offset": offset}}
            for offset in (
                {"x": -1, "y": 0},
                {"x": 1.1, "y": 0},
                {"x": True, "y": 0},
                {"x": "0", "y": 0},
                {"x": float("nan"), "y": 0},
                {"x": 0, "y": 0, "extra": 1},
            )
        ],
        *[
            {"pointer": base_position(kind="property", name=name)}
            for name in ("", "x" * 129, "😀" * 65, "bad\nname", "\ud800")
        ],
        {
            "pointer": {
                "target": {**base_position()["target"], "value": "private"},
                "offset": {"x": 0, "y": 0},
            }
        },
    ],
)
def test_base_positions_reject_unbounded_geometry_values_and_identity_fields(patch):
    with pytest.raises(ValueError):
        presence.BaseState.model_validate(base_state(**patch))


@pytest.mark.parametrize(
    "change",
    [
        "record",
        "property",
        "generation",
        "hidden",
        "token",
        "watch",
        "group-value",
        "group-reference",
        "range-axis",
        "range-date",
        "range-lane",
    ],
)
def test_base_semantic_change_fences_pending_socket_send(monkeypatch, change):
    async def run():
        owner = presence.PresenceBroker()
        viewer, publisher = (
            owner.add("viewer", actor(), guid(), 2),
            owner.add("publisher", actor(), guid(), 2),
        )
        location = presence.BaseLocation(
            kind="base",
            share_id=guid(),
            entry_id=guid(),
            generation=guid(),
            definition_id=guid(),
            view_id=guid(),
            registry_revision=1,
        )
        original = presence.BaseState.model_validate(
            extended_state(extended_range(extended_group()))
            if change.startswith("range-")
            else extended_state(extended_group())
            if change.startswith("group-")
            else base_state()
        )
        owner.publish(publisher, location)
        owner.move_base(publisher, location, original)
        owner.watch(viewer, presence.Watch(guid(), location, base_states=True))
        checked, sent = asyncio.Event(), []

        async def authorize(_fn, _token, _location, peers):
            checked.set()
            return [
                {
                    "target": dict(zip(("user_id", "session_id", "instance_id"), key)),
                    "state": state.model_dump(mode="json"),
                }
                for key, _, state in peers
            ]

        async def emit(value):
            sent.append(value)

        monkeypatch.setattr(events, "run_in_threadpool", authorize)
        monkeypatch.setattr(presence, "IMAGE_STATE_INTERVAL", 0)
        await viewer.send_lock.acquire()
        task = asyncio.create_task(
            events.deliver_roster(SimpleNamespace(send_json=emit), owner, viewer, True)
        )
        try:
            await asyncio.wait_for(checked.wait(), 1)
            if change in ("record", "property", "generation"):
                data = original.model_dump(mode="json")
                target = data["pointer"]["target"]
                if change == "property":
                    target["property"]["name"] = "different"
                else:
                    target["record_id" if change == "record" else "record_generation"] = guid()
                owner.move_base(publisher, location, presence.BaseState.model_validate(data))
            elif change.startswith(("group-", "range-")):
                data = original.model_dump(mode="json")
                target = data["pointer"]["target"]
                if change == "group-value":
                    target["value_sha256"] = "b" * 64
                elif change == "group-reference":
                    target["references"] = [{"record_id": guid(), "record_generation": guid()}]
                elif change == "range-axis":
                    target["axis"]["start_property"]["name"] = "changed"
                elif change == "range-date":
                    target["range"]["start"] = "2026-10-01"
                else:
                    target["lane"]["record_generation"] = guid()
                owner.move_base(publisher, location, presence.BaseState.model_validate(data))
            elif change == "hidden":
                owner.publish(publisher, None)
            elif change == "token":
                publisher.token = "replaced"
            else:
                owner.watch(viewer, presence.Watch(guid(), location, base_states=True))

            async def denied(*_):
                return []

            monkeypatch.setattr(events, "run_in_threadpool", denied)
            viewer.send_lock.release()
            async with asyncio.timeout(1):
                while not sent:
                    await asyncio.sleep(0.001)
            assert sent == [
                {
                    "type": "base_states",
                    "subscription_id": viewer.watch.subscription_id,
                    "status": "available",
                    "peers": [],
                }
            ]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            owner.remove(publisher)
            owner.remove(viewer)
        assert not owner.rooms and not owner.watchers and publisher.base_state is None

    asyncio.run(run())


def test_base_geometry_coalesces_without_roster_rebuild_or_authorization_starvation(monkeypatch):
    async def run():
        owner = presence.PresenceBroker()
        viewer, publisher = (
            owner.add("viewer", actor(), guid(), 2),
            owner.add("publisher", actor(), guid(), 2),
        )
        location = presence.BaseLocation(
            kind="base",
            share_id=guid(),
            entry_id=guid(),
            generation=guid(),
            definition_id=guid(),
            view_id=guid(),
            registry_revision=1,
        )
        original = base_state()
        owner.publish(publisher, location)
        owner.move_base(publisher, location, presence.BaseState.model_validate(original))
        for peer in (viewer, publisher):
            owner.watch(peer, presence.Watch(guid(), location, base_states=True))
            peer.roster_dirty.clear()
            peer.image_dirty.clear()
        viewer.image_dirty.set()
        revision, sent = publisher.base_revision, []

        async def authorize(_fn, _token, _location, peers):
            for x in range(1001):
                state = {
                    **original,
                    "pointer": {**original["pointer"], "offset": {"x": x / 1000, "y": 0.5}},
                }
                owner.move_base(publisher, location, presence.BaseState.model_validate(state))
            return [
                {
                    "target": dict(zip(("user_id", "session_id", "instance_id"), key)),
                    "state": state.model_dump(mode="json"),
                }
                for key, _, state in peers
            ]

        async def emit(value):
            sent.append(value)

        monkeypatch.setattr(events, "run_in_threadpool", authorize)
        monkeypatch.setattr(presence, "IMAGE_STATE_INTERVAL", 0)
        task = asyncio.create_task(
            events.deliver_roster(SimpleNamespace(send_json=emit), owner, viewer, True)
        )
        try:
            async with asyncio.timeout(1):
                while not sent:
                    await asyncio.sleep(0.001)
            assert sent[0]["peers"][0]["state"]["pointer"]["offset"]["x"] == 1
            assert publisher.base_revision == revision
            assert not publisher.image_dirty.is_set() and not viewer.roster_dirty.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            owner.remove(publisher)
            owner.remove(viewer)

    asyncio.run(run())


def extended_group(record=None, references=()):
    return {
        **base_position(record, kind="group")["target"],
        "value_sha256": "a" * 64,
        "references": [
            {"record_id": item.id, "record_generation": str(item.collaboration_generation)}
            for item in references
        ],
    }


def extended_range(lane=None):
    return {
        "type": "timeline_range",
        "axis": {
            "version": 1,
            "renderer": "sabidurian-timeline",
            "start_property": {"source": "note", "name": "start"},
            "end_property": None,
            "time_zone": "America/Denver",
        },
        "range": {
            "version": 1,
            "kind": "calendar",
            "start": "2026-11-01",
            "end": "2026-11-07",
            "start_inclusive": True,
            "end_inclusive": True,
            "time_zone": "America/Denver",
        },
        "lane": lane,
    }


def extended_state(target, viewport=None):
    return {
        "version": 2,
        "pointer": {"target": target, "offset": {"x": 0.25, "y": 0.5}},
        "viewport": viewport,
    }


def configure_presence_groups(db, surfaces):
    for view in surfaces.views:
        view.descriptor = {
            **view.descriptor,
            "groupBy": {"property": "note.status", "direction": "ASC"},
            "startDateProp": "note.start",
            "timeZone": "America/Denver",
        }
    db.commit()


@pytest.mark.parametrize("timeline", [False, True])
def test_extended_base_targets_negotiate_and_preserve_legacy_positions(
    client, db_session, team, surface_team, monkeypatch, timeline
):
    configure_presence_groups(db_session, surface_team)
    monkeypatch.setattr(get_settings(), "collaboration_discussions_enabled", False)
    location = surface_team.locations[2 if timeline else 1]
    group = extended_group(team.notes["first"])
    state = extended_state(
        extended_range(group) if timeline else group, base_position(team.notes["first"])
    )
    with connected(client, team.tokens["viewer"], protocol_version=2) as (publisher, _):
        publish(publisher, location)
        with connected(client, team.tokens["owner"], protocol_version=2) as (legacy, _):
            watch_base(legacy, location)
            with connected(client, team.tokens["owner"], protocol_version=2) as (modern, _):
                watch_base(modern, location, 2)
                move_base(publisher, location, state)
                assert recv(modern)["peers"][0]["state"] == state
                assert recv(legacy)["peers"][0]["state"] == {
                    "version": 1,
                    "pointer": None,
                    "viewport": state["viewport"],
                }
                move_base(publisher, location, None)
                assert recv(modern)["peers"] == []
                assert recv(legacy)["peers"] == []


@pytest.mark.parametrize("party", ["publisher", "viewer"])
def test_idle_extended_group_link_revocation_preserves_base_roster(
    client, db_session, team, surface_team, monkeypatch, party
):
    configure_presence_groups(db_session, surface_team)
    monkeypatch.setattr(presence, "CHECK_SECONDS", 0.04)
    publisher_name, viewer_name = (
        ("viewer", "owner") if party == "publisher" else ("owner", "viewer")
    )
    location = surface_team.locations[2]
    group = extended_group(surface_team.image, (team.notes["first"],))
    group["property"] = {"source": "file", "name": "file"}
    surface_team.views[1].descriptor = {
        **surface_team.views[1].descriptor,
        "groupBy": {"property": "file.file"},
    }
    db_session.commit()
    with connected(client, team.tokens[publisher_name], protocol_version=2) as (publisher, _):
        publish(publisher, location)
        with connected(client, team.tokens[viewer_name], protocol_version=2) as (viewer, _):
            watch_base(viewer, location, 2)
            move_base(publisher, location, extended_state(extended_range(group)))
            assert recv(viewer)["peers"]
            db_session.add(
                models.ManagedRule(
                    share_id=team.share.id,
                    user_id=team.viewer.id,
                    path="First.md",
                    recursive=False,
                    permission="none",
                )
            )
            db_session.commit()
            assert recv(viewer)["peers"] == []
            watch(viewer, location)
            assert len(recv(viewer)["peers"]) == 1


def test_extended_positions_authorize_grouping_axis_and_all_inputs_without_writes(
    client, db_session, team, surface_team
):
    configure_presence_groups(db_session, surface_team)
    location = presence.BaseLocation.model_validate(surface_team.locations[2])
    target = extended_range(extended_group(team.notes["first"]))
    statements = []

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", capture)
    try:
        presence.authenticate(
            team.tokens["viewer"],
            location,
            base_state=presence.BaseState.model_validate(extended_state(target)),
        )
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture)
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements
    )
    assert not any("collaboration_bases.configuration" in sql for sql in statements)
    for changed in (
        {
            **target,
            "axis": {**target["axis"], "start_property": {"source": "note", "name": "other"}},
        },
        {**target, "lane": {**target["lane"], "property": {"source": "note", "name": "other"}}},
        {**target, "lane": extended_group(team.notes["first"], (team.notes["private"],))},
        {**target, "lane": {**target["lane"], "record_generation": guid()}},
    ):
        with pytest.raises(HTTPException) as error:
            presence.authenticate(
                team.tokens["viewer"],
                location,
                base_state=presence.BaseState.model_validate(extended_state(changed)),
            )
        assert error.value.status_code == 403


@pytest.mark.parametrize(
    "patch",
    [
        {"value": "secret"},
        {"value_sha256": "bad"},
        {"property": {"source": "formula", "name": "secret"}},
        {"property": {"source": "file", "name": "size"}},
        {"references": [{"record_id": guid(), "record_generation": guid()}] * 9},
    ],
)
def test_extended_groups_reject_values_unsupported_derivations_and_unbounded_references(patch):
    with pytest.raises(ValueError):
        presence.BaseState.model_validate(extended_state({**extended_group(), **patch}))


def test_extended_target_versions_coordinates_and_duplicates_are_strict():
    group = extended_group()
    values = [
        {**extended_state(group), "version": 1},
        extended_state(
            {
                **group,
                "references": [{key: group[key] for key in ("record_id", "record_generation")}],
            }
        ),
        extended_state(
            {
                **extended_range(),
                "axis": {**extended_range()["axis"], "renderer": "sabidurian-sequence"},
            }
        ),
        extended_state(
            {**extended_range(), "axis": {**extended_range()["axis"], "time_zone": "UTC"}}
        ),
    ]
    for value in values:
        with pytest.raises(ValueError):
            presence.BaseState.model_validate(value)


@pytest.mark.parametrize(
    "version,states", [(True, True), (2.0, True), ("2", True), (4, True), (2, False)]
)
def test_extended_watch_requires_explicit_valid_base_negotiation(
    client, team, surface_team, version, states
):
    with connected(client, team.tokens["owner"], protocol_version=2) as (socket, _):
        socket.send_json(
            {
                "type": "watch",
                "subscription_id": guid(),
                "location": surface_team.locations[1],
                "base_states": states,
                "base_target_version": version,
            }
        )
        with pytest.raises(WebSocketDisconnect) as error:
            recv(socket)
        assert error.value.code == 4400


@pytest.mark.parametrize("version", [1, 2])
def test_extended_snapshot_budget_supports_full_bounded_room(monkeypatch, version):
    async def scenario():
        owner = presence.PresenceBroker()
        viewer = owner.add("viewer", actor(), guid(), 2)
        location = presence.BaseLocation(
            kind="base",
            share_id=guid(),
            entry_id=guid(),
            generation=guid(),
            definition_id=guid(),
            view_id=guid(),
            registry_revision=1,
        )
        group = extended_group()
        group["property"]["name"] = "😀" * 64
        group["references"] = [{"record_id": guid(), "record_generation": guid()} for _ in range(8)]
        state = presence.BaseState.model_validate(
            extended_state(group, {"target": group, "offset": {"x": 0.5, "y": 0.5}})
        )
        for index in range(127):
            peer = owner.add(str(index), actor(), guid(), 2)
            owner.publish(peer, location)
            owner.move_base(peer, location, state)
        owner.watch(
            viewer, presence.Watch(guid(), location, base_states=True, base_target_version=version)
        )
        sent = asyncio.Event()

        async def authorize(_fn, _token, _location, peers):
            return [
                {
                    "target": dict(zip(("user_id", "session_id", "instance_id"), key)),
                    "state": value.model_dump(mode="json"),
                }
                for key, _, value in peers
            ]

        async def emit(value):
            assert len(value["peers"]) == 127
            size = len(json.dumps(value, ensure_ascii=False).encode())
            if version == 2:
                assert presence.MAX_ROSTER_BYTES < size < presence.MAX_BASE_TARGET_BYTES
            else:
                assert size < presence.MAX_ROSTER_BYTES
                assert all(
                    item["state"] == {"version": 1, "pointer": None, "viewport": None}
                    for item in value["peers"]
                )
            sent.set()

        monkeypatch.setattr(events, "run_in_threadpool", authorize)
        monkeypatch.setattr(presence, "IMAGE_STATE_INTERVAL", 0)
        task = asyncio.create_task(
            events.deliver_roster(SimpleNamespace(send_json=emit), owner, viewer, True)
        )
        try:
            await asyncio.wait_for(sent.wait(), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for peer in list(owner.connections.values()):
                owner.remove(peer)

    asyncio.run(scenario())


def observe(socket, location, **options):
    subscription = guid()
    socket.send_json(
        {"type": "observe", "subscription_id": subscription, "location": location, **options}
    )
    return subscription


def test_passive_note_image_and_base_observations_keep_foreground_and_access_isolated(
    client, db_session, team, surface_team, monkeypatch
):
    monkeypatch.setattr(presence, "CHECK_SECONDS", 0.05)
    image, base, other_base = surface_team.locations
    with connected(client, team.tokens["owner"], protocol_version=2) as (publisher, ready):
        publish(publisher, image)
        with connected(client, team.tokens["viewer"], protocol_version=2) as (viewer, _):
            foreground = watch(viewer, base)
            assert recv(viewer) == roster(foreground)
            image_id = observe(viewer, image, image_states=True)
            frames = [recv(viewer), recv(viewer)]
            assert {frame["type"] for frame in frames} == {"roster", "image_states"}
            assert all(frame["subscription_id"] == image_id for frame in frames)
            assert (
                next(frame for frame in frames if frame["type"] == "roster")["peers"][0]["target"][
                    "instance_id"
                ]
                == ready["instance_id"]
            )
            note_id = observe(viewer, team.location())
            assert recv(viewer) == embed_frame(note_id)
            base_id = observe(viewer, other_base)
            assert recv(viewer) == roster(base_id)
            denied = observe(viewer, team.location("private"))
            assert recv(viewer) == {
                "type": "image_embeds",
                "subscription_id": denied,
                "status": "unavailable",
            }
            owner = client.app.state.collaboration_presence
            connection = next(peer for peer in owner.connections.values() if peer.watch)
            assert connection.location is None  # Passive observations never publish.
            assert set(connection.observations) == {image_id, note_id, base_id}
            surface_team.image.record = {"type": "image", "hash": "c" * 64}
            db_session.commit()
            revoked = recv(viewer)
            while revoked["status"] == "available":
                assert revoked["subscription_id"] == image_id and revoked["peers"] == []
                revoked = recv(viewer)
            assert revoked["subscription_id"] == image_id and revoked["status"] == "unavailable"
            # An invalidated image leaves the other observations and publisher intact.
            assert connection.watch.subscription_id == foreground
            for _ in range(50):
                if image_id not in connection.observations:
                    break
                time.sleep(0.01)
            assert set(connection.observations) == {note_id, base_id}
            viewer.send_json({"type": "ping"})
            while (frame := recv(viewer))["type"] != "pong":
                assert frame["subscription_id"] == image_id
    assert not owner.watchers and not owner.rooms and not owner.connections


def test_passive_and_foreground_same_room_ownership_survives_independent_unwatch(
    client, team, surface_team
):
    location = surface_team.locations[0]
    with connected(client, team.tokens["owner"], protocol_version=2) as (publisher, _):
        with connected(client, team.tokens["viewer"], protocol_version=2) as (viewer, _):
            foreground = watch(viewer, location)
            assert recv(viewer) == roster(foreground)
            passive = observe(viewer, location)
            assert recv(viewer) == roster(passive)
            viewer.send_json({"type": "unwatch", "subscription_id": foreground})
            publish(publisher, location)
            assert recv(viewer)["subscription_id"] == passive
            viewer.send_json({"type": "unobserve", "subscription_id": guid()})
            publish(publisher, None)
            assert recv(viewer) == roster(passive)
            viewer.send_json({"type": "unobserve", "subscription_id": passive})
            viewer.send_json({"type": "ping"})
            assert recv(viewer) == {"type": "pong"}
            assert not client.app.state.collaboration_presence.watchers


def test_passive_subscription_capacity_is_bounded_and_disconnect_releases_every_room(client, team):
    with connected(client, team.tokens["viewer"], protocol_version=2) as (viewer, _):
        for _ in range(presence.MAX_OBSERVATIONS):
            subscription = observe(viewer, team.location())
            assert recv(viewer) == embed_frame(subscription)
        observe(viewer, team.location("second"))
        with pytest.raises(WebSocketDisconnect) as error:
            recv(viewer)
        assert error.value.code == 4429
    assert not client.app.state.collaboration_presence.watchers


def test_passive_watch_replacement_fences_late_authorization_and_keeps_other_observers(monkeypatch):
    async def run():
        owner = presence.PresenceBroker()
        viewer = owner.add("viewer", actor(), guid(), 2)
        location = presence.ImageLocation(
            kind="image",
            share_id=guid(),
            entry_id=guid(),
            generation=guid(),
            content_sha256="a" * 64,
        )
        first = presence.Watch(guid(), location)
        replacement = presence.Watch(first.subscription_id, location)
        other = presence.Watch(guid(), location.model_copy(update={"entry_id": guid()}))
        owner.observe(viewer, first)
        owner.observe(viewer, other)
        captured, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls, sent = [], []

        async def authorize(_fn, _token, checked_location, _peers):
            calls.append(checked_location)
            if len(calls) == 1:
                captured.set()
                await release.wait()
                return [{"name": "Must not escape"}]
            return []

        async def emit(value):
            sent.append(value)
            if len(sent) == 2:
                delivered.set()

        monkeypatch.setattr(events, "run_in_threadpool", authorize)
        task = asyncio.create_task(
            events.deliver_roster(SimpleNamespace(send_json=emit), owner, viewer)
        )
        try:
            await asyncio.wait_for(captured.wait(), 1)
            owner.observe(viewer, replacement)
            release.set()
            await asyncio.wait_for(delivered.wait(), 1)
            assert {frame["subscription_id"] for frame in sent} == {
                first.subscription_id,
                other.subscription_id,
            }
            assert all(frame["peers"] == [] for frame in sent)
            assert viewer.observations[first.subscription_id] is replacement
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            owner.remove(viewer)
        assert not owner.watchers and not owner.connections

    asyncio.run(run())


def test_passive_movement_only_rechecks_its_own_scope_and_coalesces_bursts(monkeypatch):
    async def run():
        owner = presence.PresenceBroker()
        viewer, publisher = (
            owner.add("viewer", actor(), guid(), 2),
            owner.add("publisher", actor(), guid(), 2),
        )
        location = presence.ImageLocation(
            kind="image",
            share_id=guid(),
            entry_id=guid(),
            generation=guid(),
            content_sha256="a" * 64,
        )
        foreground = presence.Watch(guid(), location.model_copy(update={"entry_id": guid()}), True)
        passive = presence.Watch(guid(), location, True)
        owner.watch(viewer, foreground)
        owner.observe(viewer, passive)
        owner.publish(publisher, location)
        calls, sent = [], []
        initial, moved = asyncio.Event(), asyncio.Event()

        async def authorize(_fn, _token, checked_location, peers):
            calls.append(checked_location)
            return [
                {
                    "target": dict(
                        zip(("user_id", "session_id", "instance_id"), peer[0], strict=True)
                    )
                }
                for peer in peers
            ]

        async def emit(value):
            sent.append(value)
            if len(sent) == 2:
                initial.set()
            if len(sent) == 3:
                moved.set()

        monkeypatch.setattr(events, "run_in_threadpool", authorize)
        monkeypatch.setattr(presence, "IMAGE_STATE_INTERVAL", 0)
        task = asyncio.create_task(
            events.deliver_roster(SimpleNamespace(send_json=emit), owner, viewer, True)
        )
        try:
            await asyncio.wait_for(initial.wait(), 1)
            for x in range(1001):
                owner.move(
                    publisher,
                    location,
                    presence.ImageState(pointer={"x": x / 1000, "y": 0.5}, viewport=None),
                )
            await asyncio.wait_for(moved.wait(), 1)
            assert calls == [foreground.location, location, location]
            assert sent[-1]["subscription_id"] == passive.subscription_id
            assert sent[-1]["peers"][0]["state"]["pointer"] == {"x": 1, "y": 0.5}
            assert (
                len(passive.delivery.last_image_states) == 32
            )  # Fingerprint, not a retained payload.
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            owner.remove(viewer)
            owner.remove(publisher)

    asyncio.run(run())


def test_connection_output_budget_combines_all_subscription_and_control_frames(monkeypatch):
    async def run():
        owner = presence.PresenceBroker()
        viewer = owner.add("viewer", actor(), guid(), 2)
        monkeypatch.setattr(presence, "MAX_OUTPUT_BURST", 100)
        monkeypatch.setattr(presence.time, "monotonic", lambda: viewer.output_time)
        sent = []

        async def emit(value):
            sent.append(value)

        socket = SimpleNamespace(send_json=emit)
        assert await events.send(socket, viewer, {"type": "pong"})
        assert await events.send(socket, viewer, {"type": "roster", "peers": []})
        with pytest.raises(OverflowError):
            await events.send(socket, viewer, {"type": "image_states", "peers": ["x" * 100]})
        assert len(sent) == 2
        owner.remove(viewer)

    asyncio.run(run())


def configure_formula_presence(db, surfaces, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_presence_formula_groups_enabled", True)
    monkeypatch.setattr(get_settings(), "collaboration_discussions_enabled", False)
    monkeypatch.setattr(get_settings(), "collaboration_bases_formula_groups_enabled", False)
    configure_presence_groups(db, surfaces)
    surfaces.definition.configuration = {
        "formulas": {
            "double": "order * 2",
            "bucket": 'if(status == "Ready", formula.double, linked)',
        }
    }
    for view in surfaces.views:
        view.descriptor = {**view.descriptor, "groupBy": {"property": "formula.bucket"}}
    db.commit()


def test_formula_presence_context_is_negotiated_read_only_and_independent_of_comments(
    client, db_session, engine, team, surface_team, monkeypatch
):
    configure_formula_presence(db_session, surface_team, monkeypatch)
    legacy = base_context(client, team, surface_team.base)
    assert legacy.status_code == 200 and "formula_groups" not in legacy.json()
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = client.get(
            "/v1/collaboration/base-context",
            params={
                "share_id": str(team.share.id),
                "entry_id": surface_team.base.id,
                "base_target_version": 3,
            },
            headers={"Authorization": "Bearer " + team.tokens["viewer"]},
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["formula_groups"] == [
        {
            "view_id": str(view.id),
            "property": {"source": "formula", "name": "bucket"},
            "inputs": [{"source": "note", "name": key} for key in ("linked", "order", "status")],
        }
        for view in surface_team.views
    ]
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert len([s for s in statements if "collaboration_bases.configuration" in s]) == 1
    assert "order * 2" not in response.text


@pytest.mark.parametrize("timeline", [False, True])
@pytest.mark.parametrize("older", [1, 2])
def test_formula_groups_negotiate_without_breaking_older_receivers(
    client, db_session, team, surface_team, monkeypatch, timeline, older
):
    configure_formula_presence(db_session, surface_team, monkeypatch)
    location = surface_team.locations[2 if timeline else 1]
    group = extended_group(team.notes["first"], (team.notes["second"],))
    group["property"] = {"source": "formula", "name": "bucket"}
    state = {
        **extended_state(
            extended_range(group) if timeline else group, base_position(team.notes["first"])
        ),
        "version": 3,
    }
    with connected(client, team.tokens["viewer"], protocol_version=2) as (publisher, _):
        publish(publisher, location)
        with connected(client, team.tokens["owner"], protocol_version=2) as (legacy, _):
            watch_base(legacy, location, older)
            with connected(client, team.tokens["owner"], protocol_version=2) as (modern, _):
                watch_base(modern, location, 3)
                move_base(publisher, location, state)
                assert recv(modern)["peers"][0]["state"] == state
                assert recv(legacy)["peers"][0]["state"] == {
                    "version": older,
                    "pointer": None,
                    "viewport": state["viewport"],
                }


@pytest.mark.parametrize("loss", ["flag", "syntax", "linked_access", "member_type"])
def test_formula_group_presence_checks_gate_syntax_and_every_declared_input(
    db_session, team, surface_team, monkeypatch, loss
):
    configure_formula_presence(db_session, surface_team, monkeypatch)
    group = extended_group(team.notes["first"], (team.notes["second"],))
    group["property"] = {"source": "formula", "name": "bucket"}
    state = presence.BaseState.model_validate({**extended_state(group), "version": 3})
    location = presence.BaseLocation.model_validate(surface_team.locations[1])
    presence.authorize_base_state(db_session, team.viewer, location, state)
    if loss == "flag":
        monkeypatch.setattr(get_settings(), "collaboration_presence_formula_groups_enabled", False)
    elif loss == "syntax":
        surface_team.definition.configuration = {"formulas": {"bucket": 'file("Private").score'}}
    elif loss == "linked_access":
        db_session.add(
            models.ManagedRule(
                share_id=team.share.id,
                user_id=team.viewer.id,
                path="Second.md",
                recursive=False,
                permission="none",
            )
        )
    else:
        team.notes["first"].record = {"type": "image", "hash": "c" * 64}
    db_session.commit()
    with pytest.raises(HTTPException):
        presence.authorize_base_state(db_session, team.viewer, location, state)
