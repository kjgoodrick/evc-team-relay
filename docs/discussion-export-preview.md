# Portable discussion export preview

November 7, 2026. The server can produce a bounded, read-only JSON snapshot of
one readable note, directly opened image, or explicitly selected saved Bases
view. The client now saves verified local archives with optional sanitized
picture files, native note/saved-view controls and whole-folder collections.
Import/restore remains unfinished. See the
[client preview](../../evc-team-relay-obsidian/docs/discussion-export-preview.md).
It is not a coherent service backup and cannot be replayed as an outbox.

## Contract

Enable `COLLABORATION_EXPORT_ENABLED=true` with collaboration policy/discussions.
It defaults to false and advertises `features.discussion_export`. A read-only
member may export what they can already read; comment/edit permission is not
required. `GET /v1/collaboration/shares/{share}/entries/{entry}/export` accepts
`view_id` for Bases, where it is required. Normal entry, membership and exact
current Bases target/dependency authorization apply. Other saved views and
inaccessible record targets are omitted. Empty scopes remain empty; export never
initializes an identity, adds a receipt/event or writes a discussion.

The response has `Cache-Control: no-store`, format
`team-relay-discussion-export`, version 1, export/capture/exporter identities,
share/scope/entry identity and current path, note collaboration generation when
present, selected view, collaboration epoch and sequence, explicit inclusion
flags and an ordered array of typed records:

- `thread`: current state and revisions, author, creation/activity sequences and
  timestamp. Removed threads and their anchors/bodies are omitted, even for a
  moderator.
- `anchor_revision`: immutable target history. Each historical Bases target is
  authorized independently; unavailable old targets are redacted rather than
  copied with their quotes or dependencies.
- `message`: current revision, author, sequence and timestamps. A deleted message
  retains only its visible tombstone metadata. Its former bodies, reasons and
  reactions are never exported, including for its author or a moderator.
- `message_revision`: full rich bodies and actor/timestamp references for live
  messages, including previous versions. No command receipts, leases, tokens,
  private inbox state or moderation-only recovery bodies are included.
- `reaction`: the same emoji counts and exporter-relative `mine` flag available
  through ordinary message reads; it does not expand the participant list.
- `picture`: metadata for pictures referenced by exported live history. Original
  source hashes/lengths and sanitized rendition descriptors contain no private
  object-store keys. Missing/expired references are marked unavailable. No picture
  bytes or externally hosted images are fetched by this endpoint.
- `identity`: UUID references already visible in the exported author/actor/mention
  fields. No account emails, credentials or extra participant directory are added.

Current message content is the `message_revision` matching a message's `revision`.
References use IDs, never inferred ownership from a matching path. JSON source,
including Markdown/code, links and anchor quotes, is data and must not be rendered
as executable HTML. The local writer generates its own filenames,
verifies hashes/readback and writes its completion marker last. Picture downloads
reauthorize through the normal picture endpoint and verify the captured
rendition hash; a missing or changed picture cannot silently produce a complete
archive. External image bytes require separate explicit selection if supported.

## Consistency and resource limits

One snapshot holds the existing actor/session/share read locks until it has been
fully encoded. Comment/permission writers use the conflicting lock order, so the
JSON cannot combine a pre-edit body with a post-edit revision or midway permission
revocation. No object-store/network operation is performed under these locks.
Downloads and multi-entry collection will use separate authorized requests; this
scope snapshot does not claim vault-wide atomicity.

The endpoint allows two concurrent exports per process, at most 5,000 output
records, 10,000 scanned records and 8 MiB of encoded JSON. A ten-second work
deadline, five-second PostgreSQL lock timeout and ten-second statement timeout
bound failed attempts. Limit failures return a stable error with no partial
archive. Capacity is released on success, denial, limit or storage failure.

Rows stream in bounded batches. Messages, revisions and reaction aggregates are
read per thread, not once per reply. The intended current scale is small teams;
large scopes that exceed these limits require narrower/export paging work before
raising bounds. These are protective limits, not a claim of mobile latency or
battery acceptance.

## Evidence

- 15 focused API tests: read-only export, history/reactions, no writes, empty
  scopes, enablement, removed-body exclusion for reader/author/moderator, saved-view
  isolation, inaccessible current and historic targets, picture metadata without
  object I/O, limits, failure/capacity recovery and query count independent of
  reply count.
- 191 related discussion, Bases, picture and reattachment tests pass. The final
  two additional focused cases cover historical target redaction and capability
  negotiation. Ruff and whitespace checks pass.
- Disposable PostgreSQL proves an edit and permission writer wait for the complete
  encoded snapshot and subsequent export denies the revoked reader. A 121-message
  archive uses 16 SELECTs, the same as one message, and encodes 74,411 bytes in
  15.2 ms on the disposable local PostgreSQL fixture. This is not a mobile or
  production latency claim.
- The actual TypeScript transport/parser/archive writer passes against loopback
  HTTP and MinIO: a reader without comment permission exports history and a
  historical picture, hashes match saved files, removed bodies/reasons remain
  absent even for the owner, and revocation between capture and picture download
  leaves no completed archive. The fixture and bucket/container were removed.

No migration, production deployment, live-vault installation or automatic import
is included. Local saved-work recovery remains a separate account/device copy.


## Shared-folder scope catalog

`GET /v1/collaboration/shares/{share}/export-catalog` is independently negotiated
through `discussion_export_catalog` under the same default-off export flag. It
uses actor/session/share read locks and normal managed path-rule precedence in
SQL, then validates exact current Bases target/dependency access. Only scopes
with a readable, nonremoved thread are included. Multiple threads deduplicate
into one note/image or saved-view scope. Authorized archived views retain their
name/state and are not reactivated. Deleted entries, removed threads, mismatched
entry kinds and inaccessible record/dependency targets are excluded.

The response format `team-relay-discussion-export-catalog`, version 1, contains a
catalog ID, capture time, exporter/share, server epoch/sequence and ordered scopes
(scope/file IDs, path/kind, optional view ID/name/state). No bodies, counts, secret
keys or additional participant data are returned. It is a bounded atomic inventory,
not a promise that later scope snapshots are simultaneous. The client checks
each scope identity/kind/epoch and sequence against this inventory, then records
separate capture times and writes a non-atomic collection manifest.

The catalog shares the two-export process bound, statement/lock/work timeouts,
10,000 scan-row budget and no-partial-response behavior. It allows at most 256
scopes. Duplicate known scopes skip repeated target validation after one readable
thread establishes discovery. Per-scope snapshot reads still validate every
included thread independently. No discussion/registry identity is initialized.

Updated evidence: 24 focused server export/catalog tests pass. A disposable
PostgreSQL check proves a discussion writer waits for encoded catalog completion.
The actual TypeScript collection writer passes through loopback HTTP/MinIO and
real filesystem readback with two scopes, sanitized historical picture content,
completion hashes and revoked download refusal. Native client collection evidence
is recorded in the linked client preview. Larger/paged catalogs, reviewed import
and coherent service backup/restore remain unfinished.
