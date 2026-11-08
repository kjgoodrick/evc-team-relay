# Private pictures in comments: server preview

Updated November 6, 2026. The server/storage protocol and a desktop plugin preview
are implemented; this is **not a finished release**. The plugin includes durable
offline blobs/upload dependencies, picker/paste controls, authorized image display
and cache, basic picture-preserving editing and Send and reopen. Native desktop
picker/reload/send/edit/history/offline viewing have evidence. Native clipboard,
mobile, expired-work repair, complete storage accounting and operational acceptance
remain. Keep `COLLABORATION_PICTURES_ENABLED=false` outside disposable development
fixtures until those parts and compatible clients are ready.

## Storage and permissions

Pictures use a separately provisioned private MinIO bucket, configured by
`COLLABORATION_PICTURES_BUCKET` (default `relay-discussion-pictures`). Give it no
anonymous/public policy. The service does not provision buckets in response to
comment requests. It reuses the bounded MinIO client, with objects under
`discussion-pictures/{share}/{scope}/{picture}/v1/{rendition-sha256}.{extension}`.
Vault files, Markdown and existing note attachment-write grants are untouched.
Do not change this bucket setting without a coordinated object migration.

The server owns picture metadata and immutable revision references in PostgreSQL.
An upload progresses from `staged` to `verified`; acceptance of a message makes
it `attached` in the same transaction as the message revision, receipt and event.
Upload alone does not publish a picture. Unattached metadata/content is visible
only to its uploader, including when another member is a moderator.

Staging, upload and publication require current read and comment permission on
the exact entry. A commenter with no note-edit permission can upload. A different
uploader or entry cannot reuse the reference. Downloads require current entry
read permission and a visible message/history reference. Removing a picture in
an edit preserves live-message history access. Deleting a message or removing a
thread hides its picture metadata/content/thumbnails from all ordinary readers,
including its author and moderators. A separate surviving readable reference
can still authorize the same picture. [Restricted moderation recovery](discussion-moderation-recovery-preview.md)
uses separately audited POSTs and no ordinary picture cache; native recovery
controls and timed purge remain unfinished. There are no bearer tokens, object keys or public image
URLs in message bodies.

Authorization is checked before upload work and again after object writes.
Downloads recheck after reading the object and before returning its bytes. No
SQL authorization lock spans decoding or MinIO I/O. This prevents a revoke during
a slow storage request from publishing or delivering content under stale access.

## Protocol

All paths below start with
`/v1/collaboration/shares/{share_id}/entries/{entry_id}/pictures` and require an
authenticated session. `discussion_pictures` is a separate negotiated capability;
the complete `comments` capability remains false.

| Request | Behavior |
| --- | --- |
| `POST` to the collection path | Stage `{picture_id, sha256, size}` using a client-generated UUID and the original upload's lowercase SHA-256. Exact retries return the same identity; a different digest/size for that ID conflicts. |
| `PUT /{picture_id}/content` | Upload raw bytes, verify the original size/digest, decode and sanitize, write full-size content plus thumbnail, then finalize verification under current authorization. This endpoint does not accept multipart or a remote URL. |
| `GET /{picture_id}` | Authorized lifecycle state, source identity, expiry and measured rendition metadata. Private storage keys are omitted. |
| `GET /{picture_id}/content` | Authorized normalized image bytes. |
| `GET /{picture_id}/thumbnail` | Authorized thumbnail bytes. |

Item paths are relative to the collection path above. Byte responses set
`private, no-store`, `nosniff`, and a restrictive CSP.
Clients should fetch with their authorization header, then use an owned blob URL
for rendering; they must revoke that URL when unloading or losing access.

The existing version-1 body supports an additional block:

```json
{
  "version": 1,
  "blocks": [
    {"type": "picture", "picture_id": "<uuid>", "alt": "Diagram of the proposal"}
  ]
}
```

A picture-only message is valid. There may be at most ten picture blocks and the
existing 32 KiB body limit still applies. Alt text is bounded at 1,024 characters.
An arbitrary URL, object key or unverified ID cannot substitute for a picture.
Upgrade readers before enabling the new block type: older preview clients do not
yet render it.

Lost upload acknowledgments retry the same ID and original bytes. Verified
retries do not decode or write objects again. Concurrent command retries produce
one receipt/message/revision-reference set. Turning the creation flag off keeps
existing reads and accepted command receipts available; it also permits edits
that retain the previous revision's pictures, but blocks new attachments.

## Verification and resource limits

- Original upload: 10 MiB; decoded canvas: 20 million pixels.
- Normalized full-size image: at most 10 MiB. Thumbnail: at most 512 pixels on
  either edge and 2 MiB. Oversized encoded output is rejected while writing.
- Current inputs: static PNG, JPEG, WebP and GIF, identified from bytes.
  SVG/HTML, unsupported containers and animated inputs are rejected explicitly.
  Animated pictures are not silently flattened. Broader format/color/animation
  behavior remains part of the unfinished image acceptance work.
- The server verifies structure, reopens, fully decodes, applies EXIF orientation,
  removes metadata, then re-encodes. JPEG output uses quality 90; other current
  inputs become PNG. Original upload bytes are not stored. ICC profiles are
  removed; color-managed fidelity and output-size behavior need the remaining
  format/performance acceptance work before release.
- One admitted upload/decode and two content reads per server process, with
  fail-fast `503 picture_capacity` and `Retry-After`. Upload admission occurs
  before receiving the body, which has a 30-second deadline and a size ceiling.
  Cancellation waits for an executing worker before releasing its slot/Session.
- Unattached staging has a 24-hour expiry, a 100-picture count ceiling and a
  100 MiB source-byte quota per uploader/share. These are staging limits, not
  complete vault storage accounting. Timestamp-expired staging reservations stop
  consuming this allowance immediately, even before maintenance runs. Expired
  IDs cannot be reused. Queued comments now have explicit upload refresh with
  server-confirmed withdrawal; draft/online-command and editable rejected-work
  recovery remain unfinished.

The decoder follows Pillow's distinction between verification and pixel loading,
and its orientation API; it adds a stricter explicit pixel limit before decoding.
See [Pillow Image verification/loading](https://pillow.readthedocs.io/en/stable/reference/Image.html#PIL.Image.Image.verify)
and [EXIF transpose](https://pillow.readthedocs.io/en/stable/reference/ImageOps.html#PIL.ImageOps.exif_transpose).

## Expiry and private-object cleanup

`COLLABORATION_PICTURE_CLEANUP_ENABLED` is a separate, default-off switch. It can
continue reclaiming staging after picture creation is disabled. The worker starts
only when this flag is enabled at service startup; configuration changes still
require the normal authorized service lifecycle. No production flag was enabled
as part of development.

The worker marks expired, unreferenced staging identities before deleting bytes.
It inventories only canonical version-1 picture keys in the separate private
bucket. A 120-second database lease and persisted cursor coordinate processes
and resume interrupted batches. Defaults are 100 objects and 100 expiry candidates
per batch, a 30-second work budget between bounded storage calls, a 60-second
poll interval, and an hour between completed inventories. The MinIO SDK may fetch
a page of up to 1,000 entries; the worker processes only its configured batch.
Unknown keys/versions are counted and left untouched.

Every immutable history reference protects the picture, including deleted
messages and moderated threads. Old unused encoder renditions can be removed
after 24 hours when valid attached metadata establishes the canonical keys.
Missing picture rows and attached pictures with no remaining references receive
a **30-day hold from first observation**, rather than from object creation.
Those hold settings are maintenance safeguards, not a message-history purge
policy. This worker does not delete discussion history.

Before deleting an orphan, cleanup commits a permanent namespace-retirement
record under the same Share lock used by uploads. Staging checks that record,
so a deleted picture row cannot be recreated while its old objects are being
removed. The record survives scope/share cascades and is never pruned. Later full
inventories remove writes that finish after an earlier sweep. Partial deletion
failures preserve the cursor for retry; stale lease owners cannot overwrite a
successor's progress. Busy shares and lease rows are skipped instead of blocking
foreground transactions.

No database transaction spans object-store I/O. Shutdown signals the running
thread and waits for it to exit; repeated cancellation does not abandon a live
deleter. Logs contain counts and error classes, without private keys or exception
messages. The worker refuses the general attachment bucket and any bucket whose
versioning is enabled **or suspended**: delete markers would not reclaim old
versions. Provision a separate private bucket that has never enabled versioning.

Maintenance tuning uses `COLLABORATION_PICTURE_CLEANUP_BATCH_SIZE` (1–1,000),
`COLLABORATION_PICTURE_CLEANUP_INTERVAL_SECONDS` (10–3,600),
`COLLABORATION_PICTURE_CLEANUP_CYCLE_SECONDS` (60–86,400), and
`COLLABORATION_PICTURE_ORPHAN_RETENTION_DAYS` (1–3,650). Backups/restores must disable
and fully drain this worker alongside other mutations, include the retirement
table and private bucket, and reset incomplete inventory progress before resuming.
The coherent backup/restore implementation and rehearsal remain separate work;
do not restore rows into a live cleanup run.

## Safe queued-comment recovery

The negotiated `discussion_recovery` capability adds authenticated
`POST /v1/collaboration/commands/withdraw`, taking the exact original
`CommandEnvelope`. This is a recovery primitive, not deletion of a posted comment.
Current read/comment permission is required. It returns either
`{status: "accepted", result: <original command result>}` or
`{status: "withdrawn", operation_id, entry_id}`. A mismatched hash/scope conflicts.

An accepted receipt always wins. Otherwise withdrawal occupies the same
actor/operation primary key as successful command receipts, with a private
`withdrawn` marker in the existing JSON result. No new SQL migration is needed.
The Share lock serializes normal sends and withdrawal; the receipt's global key
also excludes conflicting cross-scope reuse. Every later original send receives
`410 operation_withdrawn`, including a send already waiting on a private anchor
proof. Withdrawal itself never calls the relay or picture store, does not
revalidate the saved anchor or referenced message, and creates no message/event.
It preserves actor/scoped identity
through the existing receipt lifecycle; backups and future retention must keep
these fences with accepted receipts.

The client only replaces queued picture work after a validated withdrawal
response. A lost withdrawal response retries the original recovery request;
an already accepted comment is reconciled without generating another upload.
The local source/reference/request transfer is atomic and preserves rich bodies,
descriptions and anchors. This increment does not yet provide arbitrary editing
of rejected attachments, draft expiry repair or Send-and-reopen recovery.

## Evidence and remaining acceptance

Latest checks: **149 focused server tests passed**, including **28 picture tests**,
**20 cleanup tests** and **9 command-recovery tests**. The previous cleanup
checkpoint additionally ran the existing retention-worker tests.
Changed-file Ruff lint/format and whitespace checks passed. The PostgreSQL and
PostgreSQL/MinIO proofs described below passed separately. Existing dependency
deprecation/test-key warnings remain; they are not picture-test failures.

`tests/test_discussion_pictures.py` covers real decoding, metadata/orientation,
alpha, actual-type checks, byte/pixel/output bounds, cancellation admission,
private staging, reviewer uploads, retries, cross-user/scope rejection, atomic
publication, history/deletion visibility, revocation during storage, expiry,
quota, corrupt content and creation-switch rollback behavior. The migration is
also exercised through upgrade/downgrade and its composite history reference.

`tests/integration/discussions_postgres.py` now applies the full migration chain
through `202611060002`, checks model/schema parity, downgrades/upgrades, and retains
the actual PostgreSQL transaction/concurrency/authorization proofs.

`tests/integration/discussion_pictures_stack.py` exercises the HTTP routes through
FastAPI's ASGI test client with actual PostgreSQL and MinIO. It checks sanitized
content, thumbnails, anonymous object-store rejection, private publication,
idempotent upload retries, concurrent message/reference acceptance, absence of
share locks during storage, and a committed revoke before download delivery.
The cleanup extension pauses an actual PUT after its first rendition, expires and
reclaims it, then verifies the late second rendition is removed on the next pass.
It also proves retained references, nonblocking foreground locks, retired-ID
rejection during physical deletion, and exactly one lease among competing workers.
It requires the disposable `127.0.0.1` database named `collaboration_test` after
the PostgreSQL fixture above, and a disposable MinIO endpoint with the synthetic
credentials named in the script. It creates and removes its own random bucket.

The server checks alone do not establish native Obsidian/mobile behavior,
complete storage accounting, restoration, real-device memory/battery performance or production
readiness. The plugin now has separate desktop UI evidence; mobile and complete
operational acceptance remain. No production deployment or live vault
installation is part of this increment.

The subsequent client proof uses `tests/integration/discussions_client.py` with
`--pictures-endpoint 127.0.0.1:<disposable-minio-port>` and the plugin checkout.
It creates a private temporary bucket, an actual loopback HTTP server and SQLite
database, then runs the plugin's `realPictureStack.test.ts`. That test verifies
offline blob/outbox restart, a comment-only uploader, a lost upload response
recovered without retransmitting verified bytes, private staging, actual binary
content/thumbnails, edit history and preserved originals after revocation. The
extended test also exercises the account-owned picture loader/cache, the
online Send and reopen path used by the native composer, recovery of a genuinely
expired picture reservation, and reconciliation of a lost successful comment
response without re-upload or duplicate publication. PostgreSQL proofs also
cover concurrent send/withdraw and withdrawal during private anchor validation. The
fixture removes its bucket and stops its HTTP process; it refuses nonloopback
MinIO endpoints. It complements the PostgreSQL proofs above. The text-only
driver mode was also rerun successfully after adding this option.
