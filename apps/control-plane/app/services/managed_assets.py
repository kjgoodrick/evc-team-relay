"""Verified content-addressed objects for managed attachment revisions.

The inventory already carries the digest. A delayed upload can write an older
digest without overwriting bytes selected by restored inventory. Callers own
authorization, restore checks, transaction release and HTTP error translation.
Unmanaged web assets keep their existing contract.
"""

import hashlib
import re
import uuid

from minio.error import S3Error


class AssetTooLarge(ValueError):
    pass


class AssetMismatch(ValueError):
    pass


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise AssetMismatch("A managed attachment requires a lowercase SHA-256 digest")
    return value


def object_name(share_id, entry_id, sha256):
    return (
        f"managed-assets/v1/{uuid.UUID(str(share_id))}/{uuid.UUID(str(entry_id))}/{digest(sha256)}"
    )


def open_object(client, bucket, share_id, entry_id, sha256):
    """Prefer immutable bytes; legacy fallback is only safe after hash checking.

    Return the response and whether it uses the old mutable key. The caller must
    bound and verify its read, and close/release the response on every path.
    """
    try:
        return client.get_object(bucket, object_name(share_id, entry_id, sha256)), False
    except S3Error as error:
        if error.code not in {"NoSuchKey", "NoSuchObject"}:
            raise
    return client.get_object(
        bucket, f"web-assets/{uuid.UUID(str(share_id))}/{uuid.UUID(str(entry_id))}"
    ), True


def verify(data, sha256, limit, expected_length=None):
    digest(sha256)
    if len(data) > limit:
        raise AssetTooLarge("Managed attachment exceeds the byte limit")
    if expected_length is not None and len(data) != expected_length:
        raise AssetMismatch("Managed attachment length does not match its scoped request")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise AssetMismatch("Managed attachment bytes do not match their digest")


def read(client, bucket, share_id, entry_id, sha256, limit):
    response, legacy = open_object(client, bucket, share_id, entry_id, sha256)
    try:
        data = response.read(limit + 1)
        content_type = response.headers.get("Content-Type", "application/octet-stream")
        verify(data, sha256, limit)
        return data, content_type, legacy
    finally:
        response.close()
        response.release_conn()


def present(client, bucket, share_id, entry_id, sha256, limit):
    """Hash-addressed objects need only a stat; legacy keys require verification."""
    try:
        metadata = client.stat_object(bucket, object_name(share_id, entry_id, sha256))
        if metadata.size > limit:
            raise AssetTooLarge("Managed attachment exceeds the byte limit")
        return
    except S3Error as error:
        if error.code not in {"NoSuchKey", "NoSuchObject"}:
            raise
    # Do not trust an old mutable object's existence as proof of this digest.
    read(client, bucket, share_id, entry_id, sha256, limit)
