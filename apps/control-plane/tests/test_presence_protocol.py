import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.presence_protocol import (
    MAX_AWARENESS,
    InvalidPresence,
    PresenceConnection,
    PresenceIdentity,
    Reader,
    buffer,
    identity_for,
    varuint,
)

IDENTITY = PresenceIdentity("real-user", "Real Member", "public-session")
POSITION = {"tname": "contents", "item": {"client": 5, "clock": 0}, "assoc": 0}


def frame(state, client=7, clock=1):
    return b"\x01" + buffer(
        b"\x01" + varuint(client) + varuint(clock) + buffer(json.dumps(state).encode())
    )


def decoded(frame):
    outer = Reader(frame)
    assert outer.integer() == 1
    reader = Reader(outer.blob(MAX_AWARENESS))
    assert reader.integer() == 1
    client, clock, state = (
        reader.integer(),
        reader.integer(),
        json.loads(reader.blob(MAX_AWARENESS)),
    )
    outer.finish()
    reader.finish()
    return client, clock, state


def test_stamp_identity_and_preserve_only_validated_cursor():
    connection = PresenceConnection()
    cursor = {"anchor": POSITION, "head": {"tname": "contents", "item": None, "assoc": -1}}
    result = decoded(
        connection.transform(
            frame(
                {
                    "user": {"id": "victim", "name": "Admin", "avatarUrl": "https://attacker.test"},
                    "sessionId": "stolen",
                    "cursor": cursor,
                    "privatePath": "Secret/Note.md",
                }
            ),
            IDENTITY,
        )
    )
    assert result == (
        7,
        1,
        {"user": IDENTITY.user(), "sessionId": "public-session", "cursor": cursor},
    )
    assert decoded(connection.transform(frame(None, clock=2), IDENTITY))[2] is None
    assert decoded(connection.transform(frame({}, clock=3), IDENTITY))[2]["user"] == IDENTITY.user()


def test_binding_cannot_be_switched_or_claimed_with_a_tombstone():
    connection = PresenceConnection()
    with pytest.raises(InvalidPresence):
        connection.transform(frame(None), IDENTITY)
    connection.transform(frame({}), IDENTITY)
    for state in ({}, None):
        with pytest.raises(InvalidPresence):
            connection.transform(frame(state, client=8), IDENTITY)


@pytest.mark.parametrize("surface", ["editor", "reading"])
def test_surface_is_bounded_and_cannot_carry_a_private_path(surface):
    state = decoded(PresenceConnection().transform(frame({"surface": surface}), IDENTITY))[2]
    assert state["surface"] == surface
    assert state["cursor"] is None


@pytest.mark.parametrize("surface", ["Secret/Note.md", {}, [], True, 0, "canvas"])
def test_unknown_surface_rejects(surface):
    with pytest.raises(InvalidPresence):
        PresenceConnection().transform(frame({"surface": surface}), IDENTITY)


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        b"\x81\x00\x01\x00",
        b"\x01\xff",
        b"\x01\x01\x00",
        b"\x01" + buffer(b"\x02"),
        frame({}) + b"\x03",
        frame({"cursor": []}),
        frame({"surface": "reading", "cursor": {"anchor": POSITION, "head": POSITION}}),
        frame({"cursor": {"anchor": POSITION}}),
        frame({"cursor": {"anchor": {**POSITION, "tname": "secret"}, "head": POSITION}}),
        frame(
            {
                "cursor": {
                    "anchor": {**POSITION, "item": {"client": True, "clock": 0}},
                    "head": POSITION,
                }
            }
        ),
        frame({"cursor": {"anchor": {**POSITION, "assoc": float("inf")}, "head": POSITION}}),
        frame(
            {
                "cursor": {
                    "anchor": {**POSITION, "item": {"client": -1, "clock": 0}},
                    "head": POSITION,
                }
            }
        ),
        frame({"huge": "a" * MAX_AWARENESS}),
        frame([], clock=1),
        frame({}, clock=2**32),
        frame({}, client=2**53),
        b"\x03\x01",
        b"\x00\x00\x02\x00",
        b"\x08\x00",
        b"\x01" + buffer(b"\x01\x01\x01" + buffer(b'{"cursor":null,"cursor":null}')),
        b"\x01" + buffer(b"\x01\x01\x01" + buffer("{}".encode("utf-16"))),
    ],
)
def test_malformed_oversized_multi_client_and_unknown_payloads_reject(bad):
    with pytest.raises(InvalidPresence):
        PresenceConnection().transform(bad, IDENTITY)


@pytest.mark.parametrize(
    "valid", [b"\x03", b"\x00\x00\x01\x00", b"\x00\x02\x00", b"\x66\x01\x01", b"\x05\x01\x01x"]
)
def test_other_supported_frames_pass_unchanged(valid):
    assert PresenceConnection().transform(valid, None) == valid


def test_only_authenticated_profile_is_used_and_session_ids_are_opaque(test_user):
    db = Mock()
    db.scalar.return_value = SimpleNamespace(
        name="Member\n Name", picture_url="https://id.example/photo.png"
    )
    claims = {"session_id": "real-login-session"}
    first = identity_for(db, test_user, claims)
    assert first.name == "Member Name"
    assert first.avatar_url == "https://id.example/photo.png"
    assert len(first.session_id) == 32
    assert first.session_id != claims["session_id"]
    assert first.session_id == identity_for(db, test_user, claims).session_id
    assert (
        first.session_id != identity_for(db, test_user, {"session_id": "second-device"}).session_id
    )
    db.scalar.return_value = SimpleNamespace(
        name="New name", picture_url="file:///private/photo.png"
    )
    updated = identity_for(db, test_user, claims)
    assert updated.name == "New name" and updated.avatar_url is None
    assert updated.session_id == first.session_id
    db.scalar.return_value = None
    assert identity_for(db, test_user, {}).name == test_user.email.split("@")[0]
