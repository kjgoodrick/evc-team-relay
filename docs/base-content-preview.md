# Conditional Bases file publication

This server increment protects standalone managed `.base` configuration saves.
It does not yet connect the Obsidian file-transfer client, provide conflict UI,
or enable Bases comments. `MANAGED_BASES_SYNC_ENABLED` defaults false. Upgrade
the client and complete native/offline acceptance before enabling it.

## Storage decision

The generic attachment pipeline publishes inventory metadata before overwriting
a mutable MinIO object. Inventory compare-and-swap alone cannot stop a late
upload from replacing newer bytes. A staged immutable-object design would need
publication tracking, orphan cleanup and coordinated backup across two stores.

Bases definitions are small configuration files, already limited to 256 KiB by
registry inspection. For this bounded type, `managed_base_content` stores the
current bytes in PostgreSQL. The bytes, SHA-256, opaque version UUID, managed
record and operation receipt commit in one transaction. This avoids external
storage work under SQL locks and intermediate missing-byte states. Ordinary
attachments remain in MinIO. The generic managed file identity/wire record stays
`type: file`; the new content protocol is advertised separately.

Tradeoffs: configuration saves add bounded database/WAL traffic and base64 adds
roughly one third to HTTP payload size. Only the current byte copy is stored;
receipts retain result metadata, not an unlimited configuration history. Local
conflict copies and coherent backups remain required. No YAML is parsed or
rewritten by file synchronization: invalid/empty/binary bytes are preserved so
an unfinished edit is recoverable. Registry inspection validates YAML separately.

## Protocol

Authenticate with a current user's session bearer token. Agent keys and relay
grants do not authorize this endpoint. Read permission allows fetching; publishing
requires current file-edit permission, independently of comment permission.

`GET /v1/managed/shares/{share_id}/entries/{entry_id}/base-content` returns:

```json
{
  "protocol_version": 1,
  "state": {
    "path": "Planning.base",
    "record": {"id": "<entry UUID>", "version": 0, "type": "file", "hash": "<SHA-256>", "mimetype": "application/yaml", "synctime": 123},
    "generation": "<UUID or null before initialization>",
    "content_version": "<UUID or null before guarded publication>"
  },
  "content_base64": "<exact bytes in base64>"
}
```

For a legacy file, the server verifies bounded MinIO bytes against the current
inventory hash without holding a database transaction. It then rechecks access
and the entire observed state. A claimed entry without a hash returns
`content_base64: null`; an accepted zero-byte file returns an empty string.
The first guarded save selects PostgreSQL as this entry's authoritative source.
Subsequent reads never fall back to the legacy object.

`PUT` to the same endpoint accepts:

```json
{
  "operation_id": "<new UUID retained for exact retries>",
  "expected": {"path": "...", "record": {}, "generation": null, "content_version": null},
  "content_base64": "<the exact bytes to publish>"
}
```

Echo the complete `state` from the last synchronized version as `expected`.
The example's empty record is only shorthand; a valid managed record is required.
The response contains `protocol_version`, `operation_id` and the accepted `state`.
The server computes the digest and sync timestamp; the client does not select
the new version UUID. A version changes even for a same-byte save, preventing a
stale write from passing merely because content changed away and back again.

Mismatch returns `409 base_content_conflict`; no content, inventory or receipt
changes commit. The client must retain its local bytes and resolve the conflict.
An accepted operation can be retried exactly after a lost response. Reusing its
ID with a different expected state, file or byte digest fails. Receipt replay
still requires current edit permission and the same generation. It returns the
original accepted state, which may be older than a later save: reconcile it with
the current inventory rather than replacing newer state.

`POST .../base-content/withdraw` accepts the same exact publication envelope.
Under the publication lock it either returns `{status: "accepted", result: ...}`
or persists a withdrawal receipt and returns `status: "withdrawn"` with the exact
operation/entry IDs. Every later publication of that request receives
`410 base_content_withdrawn`. Different intent cannot reuse the operation ID.
Current file edit permission is required. This recovery route remains available
when new guarded saves are disabled; a timeout never establishes withdrawal.

Decoded content is limited to 256 KiB. The HTTP handler caps the streamed JSON
body before parsing or opening a request database session. Errors do not echo
configuration bodies or database parameters. Legacy reads have two bounded
slots per process and use the existing storage timeouts.

## Compatibility and lifecycle

The managed manifest adds `base_content` with `protocol_version: 1`, `enabled`,
`recovery: true`,
and an authorized entry-ID-to-version map. Hidden/deleted entries are omitted;
no bytes are loaded for this metadata. This also identifies guarded files renamed
away from `.base`. Collaboration capabilities advertise `features.bases_sync`.

When enabled, `.base` metadata must be published through this protocol; new files
first claim a managed `type: file` identity without a hash. Generic inventory
moves/deletes remain supported. Once an entry has guarded content, generic
metadata changes and legacy byte uploads cannot bypass its protection even after
the preview flag is disabled or its extension changes. Generic inventory reads
refresh entry state after obtaining the share lock so a cached ORM record cannot
overwrite a concurrent publication.

Existing file-token HEAD/download routes serve guarded bytes with the same
current permission check and require the token's digest to match. Stale tokens
receive a conflict. Legacy uploads receive `428 base_content_protocol_required`.
An upload already in flight can finish writing its old MinIO object, but that
object is no longer authoritative after guarded publication. Do not delete old
objects until retention/backup policy accounts for them.

Older clients are not safe offline Bases clients: their timestamp-based local
push/pull decisions still need replacement. Server protection is necessary but
does not prevent an old client overwriting its own unsynchronized local file.
The flag remains off until upgraded clients preserve local baselines and conflicts.

Migration `202611060004` adds current content and receipt tables. Hard entry/share
deletion cascades both; soft deletion denies access while retaining data. Changing
generation requires an explicit coherent restore, never a fallback to old bytes.
The migration refuses downgrade while guarded content exists. Disabling the flag
does not export/migrate that content back into mutable storage. Full rollback,
per-share storage accounting, retention and backup/export acceptance remain open.

## Verification and remaining client work

The server regression covers content/state/receipt atomicity, stale saves, same-
byte version changes, permission and generation checks, invalid/oversized bodies,
legacy-read revocation, registry integration, compatibility routes, inventory
bypass rejection, structural moves and authorized manifest metadata.

Actual PostgreSQL/MinIO checks prove competing saves and duplicate retries,
rename-versus-publication races, obsolete legacy uploads, populated downgrade
refusal, schema parity and deletion cascades. Existing discussion and picture
concurrency/storage proofs pass with the migration.

The plugin now has protocol/transport modules, account-scoped durable baselines,
pending requests, prepared downloads, conflict copies and three-way comparison.
The actual HTTP fixture verifies those modules and PostgreSQL verifies concurrent
save/withdrawal. They are not yet connected to native file reconciliation.

Next: explicit Bases sync settings, the native sync worker and disk-write guards,
conflict review/export controls, then native/offline/mobile tests.
Never choose a winning configuration using device timestamps, automatically
overwrite a differing local file with no known baseline, or silently fall back
to the old upload protocol when the guarded API refuses a request.
