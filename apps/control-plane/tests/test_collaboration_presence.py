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
from sqlalchemy import create_engine
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
        models.BaseView(definition_id=definition.id, slot=slot, state="active", descriptor={})
        for slot in (0, 1)
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
