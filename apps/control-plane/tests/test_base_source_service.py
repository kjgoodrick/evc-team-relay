"""Private source transport and bounded parsing; not group authorization tests."""

import base64
import hashlib
import json
import threading
import uuid
from dataclasses import replace

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from app.core.config import get_settings
from app.services import anchor_service
from app.services import base_source_service as service


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.setattr(service.base_discussions, "definitions_enabled", lambda: True)
    monkeypatch.setattr(
        get_settings(), "collaboration_anchor_relay_token", SecretStr("source-test-private")
    )
    monkeypatch.setattr(get_settings(), "managed_relay_url", "ws://source-relay.test:8080")
    monkeypatch.setattr(service, "_slots", threading.BoundedSemaphore(2))
    return uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "AAA="


def relay(monkeypatch, *, prefix="---\nStatus: Planned\n---\nBody", complete=True, change=None):
    original_client = httpx.Client
    requests = []

    def handle(request):
        assert request.headers["authorization"] == "Bearer source-test-private"
        assert str(request.url).startswith("http://source-relay.test:8080/internal/collaboration/")
        value = json.loads(request.content)
        requests.append((request.url.path.rsplit("/", 1)[-1], value))
        result = {
            key: value[key]
            for key in (
                "protocol_version",
                "request_id",
                "document_id",
                "share_id",
                "generation",
                "expires_at_ms",
            )
        }
        result.update(
            request_sha256=hashlib.sha256(request.content).hexdigest(),
            checkpointed_at_ms=anchor_service.now_ms(),
        )
        if "source_dependency" in value:
            result.update(
                source_prefix=prefix,
                source_prefix_sha256=hashlib.sha256(prefix.encode()).hexdigest(),
                source_complete=complete,
                observed_snapshot_sha256="a" * 64,
            )
        status = 200
        if change:
            status, result = change(value, result)
        encoded = result if isinstance(result, bytes) else json.dumps(result).encode()
        return httpx.Response(status, stream=httpx.ByteStream(encoded))

    def factory(**kwargs):
        assert kwargs == {"follow_redirects": False, "trust_env": False}
        return original_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(service.httpx, "Client", factory)
    return requests


def failure(call, status, code):
    with pytest.raises(HTTPException) as caught:
        call()
    assert caught.value.status_code == status
    assert caught.value.detail == {"code": code}
    return caught.value


def test_observation_is_bound_to_scope_generation_dependency_and_expiry(identity, monkeypatch):
    requests = relay(monkeypatch)
    observed = service.observe(*identity)
    assert [route for route, _ in requests] == ["register", "source"]
    assert requests[0][1]["request_id"] != requests[1][1]["request_id"]
    assert "source_dependency" not in requests[0][1]
    assert requests[1][1]["source_dependency"] == identity[-1]
    assert service.property_value(observed, "status") == "Planned"
    assert "Planned" not in repr(observed)
    observed.check(*identity)
    for index in range(4):
        other = list(identity)
        other[index] = uuid.uuid4() if index < 3 else "AAE="
        failure(lambda: observed.check(*other), 409, "anchor_generation_mismatch")
    failure(
        lambda: replace(observed, expires_at_ms=0).check(*identity),
        503,
        "anchor_validation_expired",
    )
    failure(
        lambda: replace(observed, deadline=0).check(*identity),
        503,
        "anchor_validation_expired",
    )


@pytest.mark.parametrize("dependency", [None, 1, "", "AA", "AAA=\n", "AA?=", "AAB=", "A" * 87385])
def test_noncanonical_or_oversized_dependency_never_reaches_relay(
    identity, monkeypatch, dependency
):
    requests = relay(monkeypatch)
    failure(lambda: service.observe(*identity[:3], dependency), 422, "invalid_base_value_source")
    assert requests == []


def test_decoded_dependency_limit_and_uuid_types(identity, monkeypatch):
    requests = relay(monkeypatch)
    encoded = base64.b64encode(b"a" * 65537).decode()
    assert len(encoded) == 87384  # Encoded length alone does not enforce the decoded limit.
    failure(lambda: service.observe(*identity[:3], encoded), 422, "invalid_base_value_source")
    failure(
        lambda: service.observe(str(identity[0]), *identity[1:]), 422, "invalid_base_value_source"
    )
    assert requests == []


def test_disabled_and_busy_observations_do_not_make_private_requests(identity, monkeypatch):
    requests = relay(monkeypatch)
    monkeypatch.setattr(service.base_discussions, "definitions_enabled", lambda: False)
    failure(lambda: service.observe(*identity), 409, "base_value_sources_disabled")
    monkeypatch.setattr(service.base_discussions, "definitions_enabled", lambda: True)
    monkeypatch.setattr(get_settings(), "collaboration_anchor_relay_token", SecretStr(""))
    failure(lambda: service.observe(*identity), 409, "base_value_sources_disabled")
    monkeypatch.setattr(
        get_settings(), "collaboration_anchor_relay_token", SecretStr("source-test-private")
    )
    assert service._slots.acquire(blocking=False) and service._slots.acquire(blocking=False)
    failure(lambda: service.observe(*identity), 503, "base_value_source_busy")
    assert requests == []
    service._slots.release()
    service._slots.release()
    service.observe(*identity)


@pytest.mark.parametrize(
    "key,value",
    [
        ("request_id", str(uuid.uuid4())),
        ("document_id", str(uuid.uuid4())),
        ("share_id", str(uuid.uuid4())),
        ("generation", str(uuid.uuid4())),
        ("request_sha256", "b" * 64),
        ("protocol_version", True),
        ("expires_at_ms", 0),
        ("checkpointed_at_ms", 0),
        ("source_complete", 1),
        ("source_prefix", []),
        ("source_prefix", "\ud800"),
        ("source_prefix", "changed text"),
        ("source_prefix_sha256", "b" * 64),
        ("observed_snapshot_sha256", "A" * 64),
        ("observed_snapshot_sha256", None),
    ],
)
def test_corrupt_source_receipts_fail_closed_and_release_admission(
    identity, monkeypatch, key, value
):
    def change(request, result):
        if "source_dependency" in request:
            result[key] = value
        return 200, result

    relay(monkeypatch, change=change)
    for _ in range(3):
        failure(lambda: service.observe(*identity), 503, "anchor_validation_unavailable")
    assert service._slots.acquire(blocking=False) and service._slots.acquire(blocking=False)
    assert not service._slots.acquire(blocking=False)


@pytest.mark.parametrize(
    "status,code,expected,public_code",
    [
        (409, "generation_mismatch", 409, "anchor_generation_mismatch"),
        (409, "share_mismatch", 409, "anchor_generation_mismatch"),
        (409, "dependency_not_ready", 409, "anchor_dependency_not_ready"),
        (404, "document_unavailable", 409, "anchor_dependency_not_ready"),
        (413, "source_document_too_large", 413, "base_value_source_too_large"),
        (413, "request_too_large", 503, "anchor_validation_unavailable"),
        (422, "invalid_dependency", 422, "invalid_anchor"),
        (503, "checkpoint_failed", 503, "anchor_validation_unavailable"),
        (302, "redirect", 503, "anchor_validation_unavailable"),
    ],
)
def test_private_failure_mapping(identity, monkeypatch, status, code, expected, public_code):
    def change(request, result):
        return (status, {"code": code}) if "source_dependency" in request else (200, result)

    relay(monkeypatch, change=change)
    failure(lambda: service.observe(*identity), expected, public_code)


def test_source_json_escape_expansion_is_bounded_without_relaxing_other_routes(
    identity, monkeypatch
):
    prefix = "\x01" * service.MAX_PREFIX_BYTES
    with monkeypatch.context() as scoped:
        relay(scoped, prefix=prefix, complete=False)
        assert service.observe(*identity).source_prefix == prefix
    relay(monkeypatch, prefix="🌿" * (service.MAX_PREFIX_BYTES // 4 + 1))
    failure(lambda: service.observe(*identity), 503, "anchor_validation_unavailable")


@pytest.mark.parametrize("route", ["register", "source"])
def test_transport_stream_limit_applies_before_parsing(identity, monkeypatch, route):
    def change(request, result):
        current = "source" if "source_dependency" in request else "register"
        if current == route:
            return 200, b" " * ((512 * 1024 if route == "source" else 65536) + 1)
        return 200, result

    relay(monkeypatch, change=change)
    failure(lambda: service.observe(*identity), 503, "anchor_validation_unavailable")


def test_network_failures_do_not_expose_transport_details(identity, monkeypatch):
    def change(*_args):
        raise httpx.ConnectError("private-token source-relay.test")

    relay(monkeypatch, change=change)
    error = failure(lambda: service.observe(*identity), 503, "anchor_validation_unavailable")
    assert "private-token" not in str(error) and "source-relay.test" not in str(error)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("---\n---\n", {}),
        ("no metadata\n---\nx: hidden\n---\n", {}),
        ("---\nx: unfinished", {}),
        ("---\nx: 1\n---", {"x": 1}),
        ("---\r\nStatus: On\r\n---\r\n", {"Status": "On"}),
        (
            "---\ndate: 2026-11-06\nyes: true\nnumber: 1.5\n---\n",
            {
                "date": "2026-11-06",
                "yes": True,
                "number": 1.5,
            },
        ),
        ("---\nlist: [a, b]\nmap: {x: null}\n---\n", {"list": ["a", "b"], "map": {"x": None}}),
        ("---\ntext: |\n  ---\n  nested\n---\nbody", {"text": "---\nnested\n"}),
    ],
)
def test_frontmatter_fences_and_bounded_scalar_subset(raw, expected):
    assert service.metadata(raw, complete=True) == expected


def test_incomplete_prefix_never_becomes_eof_or_an_absent_frontmatter_block():
    assert service.metadata("---\nx: 1\n---\nmore", complete=False) == {"x": 1}
    assert service.metadata("plain body", complete=False) == {}
    for source in ("---\nx: 1", "---\nx: 1\n---"):
        with pytest.raises(service.InvalidMetadata):
            service.metadata(source, complete=False)


@pytest.mark.parametrize(
    "raw",
    [
        "\ufeff---\nx: hidden\n---\n",
        "a" * 65537,
        "---\n[x, y]\n---\n",
        "---\nx: 1\nx: 2\n---\n",
        "---\nx: &node [a]\ny: *node\n---\n",
        "---\nx: !!binary YQ==\n---\n",
        "---\nx: !application data\n---\n",
        "---\nx: 1e999\n---\n",
        "---\nx: 9007199254740992\n---\n",
        "---\nx: " + "9" * 5000 + "\n---\n",
        '---\nx: "\\uD800"\n---\n',
        '---\n"\\uD800": x\n---\n',
        "---\nx: " + "[" * 17 + "0" + "]" * 17 + "\n---\n",
        "---\nx: [" + ",".join("0" for _ in range(4096)) + "]\n---\n",
    ],
)
def test_unsupported_metadata_cannot_silently_supply_a_value(raw):
    with pytest.raises(service.InvalidMetadata) as caught:
        service.metadata(raw, complete=True)
    assert raw not in str(caught.value)


def test_property_lookup_is_unambiguous_and_validates_names(identity, monkeypatch):
    relay(monkeypatch, prefix="---\nStatus: Planned\n---\n")
    observed = service.observe(*identity)
    assert service.property_value(observed, "STATUS") == "Planned"
    assert service.property_value(observed, "absent") is None
    for name in (None, "", "x" * 257, "x\n", "\ud800"):
        with pytest.raises(service.InvalidMetadata):
            service.property_value(observed, name)
    ambiguous = replace(observed, source_prefix="---\nStatus: Planned\nstatus: Done\n---\n")
    with pytest.raises(service.InvalidMetadata, match="Ambiguous"):
        service.property_value(ambiguous, "status")
