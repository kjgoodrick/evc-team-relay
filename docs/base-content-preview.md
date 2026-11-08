# Conditional Bases file publication

This server protects standalone managed `.base` configuration saves. The upgraded
Obsidian client now has a native sync/recovery preview, including explicit removal
review. This does not enable Bases comments. `MANAGED_BASES_SYNC_ENABLED` defaults
false. Complete the remaining native/offline/mobile acceptance before production
enablement.

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
  "restore_guard": true,
  "restore_generation": null,
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
`recovery: true`, and an authorized entry-ID-to-version map in `versions`.
Hidden/deleted entries are omitted from that map. An additional optional `retired`
map associates deleted guarded entry IDs with their last path, only when current
path permission allows the ordinary `deleted` tombstone. It contains no content
or historical bodies. Hidden paths remain absent, including after permission
revocation. No bytes are loaded for this metadata. Both maps identify guarded
files renamed away from `.base`; a new file at the same path has a different ID
and must never be treated as the deleted file. Clients require the matching
ordinary tombstone as well before entering removal recovery. Absence from the
manifest does not establish deletion. Collaboration capabilities advertise
`features.bases_sync`.

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

The plugin now connects the protocol to native file reconciliation with explicit
settings, bounded scheduling, guarded disk writes, inventory coordination and
review/export controls. Removal retains its baseline, pending requests and the
observed local file in the account-scoped cache. Explicit review moves the file
intact into the excluded recovery folder, recording intent before the move and
acknowledging it afterwards. A new or newly authorized file at the same path blocks
that action. Removal never implicitly withdraws unknown publication or restores
the old identity.

The deletion increment passed 40 content/managed-share tests and native Obsidian
1.13.7 checks with actual disposable HTTP services. A server-directed rename,
restart, remote deletion while the vault was closed, and an unsent local edit
were followed by native review/move/export. The removed identity stayed deleted
and the baseline/offline edit remained recoverable. The earlier PostgreSQL
serialization checks continue to cover the unchanged mutation protocol; this
increment adds manifest metadata without a schema migration.

Next: guided restore from recovery, complete local-delete handling, remaining
native rename/parent-folder races, cache reclamation and mobile acceptance.
An external rename to an unsupported extension was observed as native deletion;
the remaining bytes stayed on disk but their old managed identity was not kept.
Do not claim arbitrary filesystem renames preserve identity.
Never choose a winning configuration using device timestamps, automatically
overwrite a differing local file with no known baseline, or silently fall back
to the old upload protocol when the guarded API refuses a request.


## Read-only delivery inspection

`POST /v1/managed/shares/{share}/entries/{entry}/base-content/status` accepts the
same bounded `PublishBaseContent` envelope and returns `accepted` with the exact
historical result, or `withdrawn`/`unknown` with the operation and entry IDs. It
requires current read access and verifies actor ownership, entry identity and
the original canonical hash. Another actor receives unknown; an altered request
or a different entry cannot disclose the receipt. It does not publish, withdraw,
create a receipt or require current edit permission. New-publication disablement
and changed current source/restore generations do not hide an otherwise readable
historical receipt. Deleted/inaccessible entries remain unavailable.

Unknown is an observation, not proof of non-acceptance: a restored database may
have lost an earlier receipt. The client therefore retains the exact pending
request and shows the result without automatically settling, replacing or
retrying it. Restore-generation binding for registry/inventory mutations and explicit
restored-work reconciliation remain unfinished.

`tests/integration/base_content_status_postgres.py` requires an empty disposable
loopback `collaboration_test` database. It verifies no INSERT/UPDATE/DELETE occurs
for known/unknown inspection, and current read revocation wins while a receipt
check waits for its share lock. The real plugin HTTP harness checks accepted,
withdrawn and unknown cases with unchanged source and local pending state.


## Restore-generation publication guard

Content reads advertise `restore_guard` and `restore_generation` (NULL for the
original server lineage). PUT and withdrawal require that original generation
in `X-Relay-Restore-Generation` when nonnull. The current locked share is checked
before existing receipts or new content/withdrawal changes. The header is not
part of the canonical publication body/hash. Historical `/status` inspection
remains read-only and requires no mutation header.

The Bases client persists this binding on each pending save. Each entry keeps a
revision-checked observation acquired before the remote read under its native
entry lock; stale observations and downgrades from a known restored generation
are refused. Existing baselines and pending bytes are retained. An old pending
request cannot publish/withdraw; a late old acceptance cannot settle it or
acknowledge managed inventory after a newer observation. Pre-restore local edits
and interrupted downloads become explicit conflicts. Fresh local initialization
on a restored share also requires review when there is no proven baseline.
Reviewed replacement work binds to that reviewed current snapshot. Original
NULL-lineage compatibility remains.

The separate Bases IndexedDB is version 3 to fence workers that would otherwise
drop the binding. Existing records and recoveries survive the upgrade. Native
acceptance, registry/inventory fencing, reconciliation of historical pending
requests and the coordinated restore/rollback workflow are still unfinished;
do not enable a live restore solely on this content guard.
