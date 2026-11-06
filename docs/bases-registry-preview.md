# Bases registry preview

This increment implements a server-owned identity registry for synchronized
standalone `.base` files. It does **not** enable Bases comments, inline base
definitions, a native adapter, or timeline annotations. `features.bases` remains
false. `features.bases_registry` advertises this narrower protocol only when
both `COLLABORATION_POLICY_ENABLED` and
`COLLABORATION_BASES_REGISTRY_ENABLED` are enabled. The latter defaults false.

The subsequent [conditional content protocol](base-content-preview.md) supplies
verified PostgreSQL bytes after a file's first guarded save. Legacy definitions
still use the verified MinIO read described below. Neither server protocol is a
completed native Bases feature.

The registry never writes identities or comments into a vault file. A managed
entry's UUID identifies the standalone base; its collaboration generation guards
restore lineage. Server-generated view UUIDs are independent of array position,
display name and layout. This follows the agreed saved-view discussion scope.

## Inspect a synchronized definition

`POST /v1/collaboration/shares/{share_id}/entries/{entry_id}/bases/inspect`

Authenticate with an active user's session bearer token. A managed read grant
on the actual `.base` file is sufficient; inspecting does not require note-write
or comment permission. The body is:

```json
{"content_sha256":"<lowercase SHA-256 of the local synchronized definition>"}
```

The server first checks the current managed inventory's hash. It releases its
SQL transaction before reading `web-assets/{share_id}/{entry_id}` from private
object storage, verifies the downloaded bytes against that hash, and parses the
definition. It then obtains fresh authorization and a share/entry lock, rechecks
generation and hash, and creates or updates the registry atomically. A hash
mismatch waits for definition synchronization; it never registers client-supplied
view descriptors as authoritative. An unchanged verified hash uses the stored
snapshot without another object download or new revision.

The response contains `protocol_version: 1`, `definition_id`, `entry_id`,
`generation`, `revision`, `content_sha256`, and `views`. Each current view has
`slot`, `id`, `name`, `type`, and `descriptor_sha256`. A slot locates a view in
this particular verified snapshot; it is never a persistent target identity.
An `id: null` means the current association is unresolved. The response also
contains an `unmatched` page of historical identities awaiting review.

Configuration is valid YAML in Obsidian's format. The registry parses it as
data, without evaluating formulas, filters or custom view code. Unknown
view-specific fields remain part of its descriptor. Current limits are 256 KiB,
64 saved views, 20,000 YAML nodes and 32 levels of nesting. Duplicate mapping
keys, non-string keys, duplicate view names, aliases/merge keys, non-JSON tags,
unsafe integers and non-finite numbers are rejected. Plain `On` remains a string,
and calendar dates remain strings. These limits describe the preview; valid
hand-authored YAML using unsupported constructs needs a clear host error, not a
rewritten file. See [Obsidian's Bases syntax](https://obsidian.md/help/bases/syntax)
and [PyYAML's loader documentation](https://pyyaml.org/wiki/PyYAMLDocumentation).

Two inspections per process may read/parse object data concurrently; excess
work receives `429 base_inspection_busy`. Reads are bounded and use the existing
storage connection/read timeouts. No SQL lock spans object I/O or YAML parsing.

## Reconciliation and lifecycle

Automatic reconciliation matches exact descriptors through reordering, then
unique unchanged names through settings/layout changes. A pure rename requires
a name-free descriptor that is unique across both complete snapshots. It does
not become "unique" merely because an identical sibling was matched first.
An ordinary copy whose original remains identifiable gets a new view identity.

Old-only views are archived. A subsequently created same-named view gets a new
identity. Remaining old/new combinations are unmatched; current unassociated
slots get no identity. Previously unmatched or archived identities never
automatically reappear. Historical views retain their last descriptor.

These are snapshot comparisons, not proof of user intent. A delete/recreate or
exchange of indistinguishable definitions entirely between observations cannot
be discovered from file bytes alone. Native create/rename/duplicate/delete hooks
must submit explicit transitions, and ambiguity repair needs a review UI before
this is a complete Bases feature. Hidden UUIDs copied by Obsidian duplication
are not trusted as canonical identity.

## Explicit editor repair and observed transitions

`POST /v1/collaboration/shares/{share_id}/entries/{entry_id}/bases/reconcile`

This requires **current write permission on the base**, independently of comment
permission. It accepts:

```json
{
  "content_sha256": "<verified current definition SHA-256>",
  "operation_id": "<new UUID retained for exact retries>",
  "expected_revision": 3,
  "bindings": [
    {"slot": 0, "view_id": "<known view UUID from this definition>"},
    {"slot": 1, "view_id": null}
  ],
  "archive": ["<known retired view UUID from this definition>"]
}
```

Every current slot must occur exactly once. Reusing one view ID in two slots,
simultaneously binding and archiving it, or using another definition's identity
is rejected. `null` explicitly creates a new identity. A known archived identity
can be deliberately restored. Unused active identities become unmatched unless
explicitly archived; none are silently merged or deleted.

The same verified-byte/auth boundary applies. Compare-and-swap checks the registry
revision under the share lock. One concurrent repair wins; the other receives
`409 stale_base_revision`. Accepted operations record actor, exact request hash,
revision and result. Exact retry returns the stored result only after checking
current base edit permission. Reusing an operation ID with another payload or
base fails. A receipt response describes its accepted revision and may be older
than a later registry update; clients must not replace a newer cache with it.
The registry audit stores identity summaries and results, not a complete version
history of all base configuration bytes.

`GET .../bases/views?state=unmatched|archived&after=<optional UUID>` returns at
most 100 identities with `has_more` and `next_after`. Every page rechecks current
base read access and generation. There is no path-based fallback after revocation.

Relevant error codes include `base_definition_not_synced`,
`base_definition_changed`, `base_generation_changed`, `invalid_base_definition`,
`base_definition_too_large`, `base_editor_required`, `incomplete_base_bindings`,
`base_view_unavailable`, `stale_base_revision`, and `operation_id_conflict`.

## Persistence and compatibility

Migration `202611060003` adds `collaboration_bases`,
`collaboration_base_views`, and `collaboration_base_revisions`. The managed entry
remains the access boundary. Soft deletion denies access without repurposing
identity; hard deletion of the entry/share cascades registry rows. Registry
history, view identities and operation receipts must be included in coherent
backups. Restore repair and complete rollback acceptance remain unfinished.

The parser dependency is PyYAML 6.0.3 in `uv.lock`. Existing discussion endpoints
keep their note/image type and comment-permission rules; only the registry calls
the generalized context helper for file records and separate editor authority.
Old clients ignore the additional capability. No production flag was enabled.

## Verified checks and remaining work

`tests/test_bases_registry.py` covers snapshot lifecycle, parsing limits,
permission/metadata changes during I/O, exact receipts, explicit repair, paging
and unchanged-hash reuse. The migration proof verifies PostgreSQL schema parity
and empty-table downgrade/upgrade. `tests/integration/bases_registry_stack.py`
uses actual PostgreSQL and MinIO to verify concurrent initialization, repair
CAS, HTTP lifecycle, no share lock during storage I/O, mid-read revocation and
deletion cascades. Existing discussion/picture PostgreSQL/MinIO proofs also pass.

Still required: explicit `.base` sync settings and client conditional publication,
client registry/cache/provisional-ID handling, inline definition lineage,
native transition hooks and repair UI, view-scoped discussion authorization,
cell/property/group/formula/summary targets, Time & Line integration, presence,
and native desktop/mobile acceptance. A dependable server registry alone does
not establish those features.

The initial registry only detected mutable-object hash mismatches. The new
guarded content service atomically publishes configuration and inventory in
PostgreSQL; this registry reads that authoritative copy when present. Client
baselines, local conflict preservation and native integration remain necessary
before enabling Bases collaboration in a live vault.
