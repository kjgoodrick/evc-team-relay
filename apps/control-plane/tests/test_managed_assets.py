import hashlib
import io
import uuid
from types import SimpleNamespace

import pytest
from minio.error import S3Error

from app.services import managed_assets as service


def failure(code):
    return S3Error(None, code, "synthetic", "/fixture", "fixture", "fixture")


def fixture(objects):
    reads, responses = [], []

    class Response(io.BytesIO):
        headers = {"Content-Type": "image/png"}
        released = False

        def release_conn(self):
            self.released = True

    def get_object(bucket, key):
        assert bucket == "fixture"
        reads.append(key)
        if key not in objects:
            raise failure("NoSuchKey")
        if isinstance(objects[key], Exception):
            raise objects[key]
        response = Response(objects[key])
        responses.append(response)
        return response

    return SimpleNamespace(get_object=get_object, reads=reads, responses=responses)


def test_verified_hash_keys_keep_file_revisions_distinct_and_prefer_immutable_bytes():
    share, entry = uuid.uuid4(), uuid.uuid4()
    current, previous = b"new image", b"old image"
    sha, old_sha = [hashlib.sha256(value).hexdigest() for value in (current, previous)]
    key = service.object_name(share, entry, sha)
    assert key != service.object_name(share, entry, old_sha)
    client = fixture({key: current, f"web-assets/{share}/{entry}": previous})
    assert service.read(client, "fixture", share, entry, sha, 100) == (current, "image/png", False)
    assert client.reads == [key]
    assert all(response.closed and response.released for response in client.responses)


def test_legacy_fallback_is_bounded_and_must_match_the_requested_revision():
    share, entry = uuid.uuid4(), uuid.uuid4()
    value = b"legacy bytes"
    sha = hashlib.sha256(value).hexdigest()
    client = fixture({f"web-assets/{share}/{entry}": value})
    assert service.read(client, "fixture", share, entry, sha, 100) == (value, "image/png", True)
    assert len(client.reads) == 2
    with pytest.raises(service.AssetMismatch):
        service.read(client, "fixture", share, entry, "0" * 64, 100)
    with pytest.raises(service.AssetTooLarge):
        service.read(client, "fixture", share, entry, sha, 3)
    assert all(response.closed and response.released for response in client.responses)


def test_storage_errors_and_corrupt_immutable_bytes_never_fall_back_to_older_content():
    share, entry = uuid.uuid4(), uuid.uuid4()
    sha = hashlib.sha256(b"expected").hexdigest()
    key = service.object_name(share, entry, sha)
    client = fixture({key: failure("AccessDenied"), f"web-assets/{share}/{entry}": b"expected"})
    with pytest.raises(S3Error):
        service.read(client, "fixture", share, entry, sha, 100)
    assert client.reads == [key]
    client = fixture({key: b"corrupt", f"web-assets/{share}/{entry}": b"expected"})
    with pytest.raises(service.AssetMismatch):
        service.read(client, "fixture", share, entry, sha, 100)
    assert client.reads == [key]
    assert all(response.closed and response.released for response in client.responses)


def test_upload_verification_rejects_wrong_lengths_hashes_and_oversized_bytes():
    data = b"captured bytes"
    sha = hashlib.sha256(data).hexdigest()
    service.verify(data, sha, 100, len(data))
    for invalid in ("", "../key", "A" * 64, "a" * 63):
        with pytest.raises(service.AssetMismatch):
            service.object_name(uuid.uuid4(), uuid.uuid4(), invalid)
    with pytest.raises(service.AssetMismatch):
        service.verify(data, sha, 100, len(data) + 1)
    with pytest.raises(service.AssetMismatch):
        service.verify(data + b"changed", sha, 100)
    with pytest.raises(service.AssetTooLarge):
        service.verify(data, sha, 1)
