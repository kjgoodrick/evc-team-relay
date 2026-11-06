# Saved-view discussion protocol preview

This increment implements standalone Bases discussion targets for saved views,
records/items, cells and note/file property headers. It includes server
authorization and client wire/offline-queue support. **Native Bases commenting
is not connected yet.** Groups, formula/summary dependencies, inline definitions,
Time & Line targets and presence remain required work.

Creation requires `COLLABORATION_BASES_DISCUSSIONS_ENABLED=true` together with
the existing policy, discussions and registry flags. All remain opt-in. The
general `comments` and `bases` release capabilities remain false. The new
`discussion_bases` capability lists exactly `view`, `item`, `cell`, `property`
in `discussion_base_targets`; it does not advertise other targets. Upgrade
clients before enabling creation. Disabling this creation flag preserves reads,
replies and recovery for existing discussions while the discussion API remains
available.

## Identity and target capture

`POST /v1/collaboration/shares/{share}/entries/{base}/bases/target-context`
accepts `{view_id, record_id?}`. A reader can capture authoritative definition,
saved-view and optional record lineage. The server holds the share write lock
and initializes a missing record generation once. This writes collaboration
metadata, never `.base` or Markdown bytes. The response contains:

```json
{
  "protocol_version": 1,
  "entry_id": "<managed .base entry UUID>",
  "definition_id": "<registry definition UUID>",
  "view_id": "<saved-view UUID>",
  "generation": "<definition generation UUID>",
  "registry_revision": 1,
  "record_id": "<optional managed Markdown entry UUID>",
  "record_generation": "<present only with record_id>"
}
```

Creation uses the existing transactional `thread.create` command. Its anchor is:

```json
{
  "version": 1,
  "type": "base",
  "definition_id": "<registry definition UUID>",
  "view_id": "<saved-view UUID>",
  "generation": "<definition generation UUID>",
  "registry_revision": 1,
  "target": {
    "type": "cell",
    "record_id": "<managed Markdown entry UUID>",
    "record_generation": "<record generation UUID>",
    "property": {"source": "note", "name": "status"}
  }
}
```

Other targets are `{type: "view"}`, an `item` with the same record fields, or
`{type: "property", property: {source, name}}`. Properties accept only `note`
and `file` namespaces, with bounded valid Unicode names. They identify the
property, not a captured cell value. Client-claimed dependency arrays, formulas
and unknown target kinds are rejected. Formula/summary/group targets need their
own verified identity and dependency contracts before implementation.

New threads require a currently active view and a registry definition matching
the managed file's current hash. The captured registry revision must prove that
the view existed. An older captured revision survives an ordinary inspected
rename; it is not a requirement that every queued comment use the latest revision.
Archived/unmatched views keep existing thread history and replies. They refuse
new threads until explicitly associated with an active view. A generation change
never reinterprets the old anchor as a new record or definition.

## Authorization and transactions

One existing `DiscussionScope(kind="base")` belongs to the managed definition
entry. Required saved-view filters, typed anchors and view-bound cursors preserve
the product's saved-view discussion namespace without another database scope
migration. Native cache and surface integration must preserve that distinction.

All reads require membership and definition read access. Item/cell threads also
require current read access to the same-share, live Markdown record and its
matching generation. Comment mutations require comment access to both entries.
View/property threads have definition-level authority. There is no separate
property ACL. Mentions additionally require recipient access to the record.

These checks cover thread/message/history/state reads, mutations, accepted retry
receipts, recovery receipts, event/list filtering and attached pictures. The
picture route rechecks after object I/O; no database lock is held across object
reads. An independently authorized reference can make the same picture readable.
Staged unattached pictures retain the existing private-owner behavior.

The existing share lock serializes policy, inventory and discussion changes.
Context refreshes stale ORM identities after lock acquisition. Exact duplicate
commands still commit one message/event/receipt. Existing online-only moderation
and atomic Send and reopen semantics apply unchanged.

## Lists and catch-up

For Bases, both existing `GET .../threads` and `GET .../changes` require
`view_id=<UUID>`. Missing views are rejected, and a view from another definition
cannot be used. Ordinary note/image requests retain their earlier response shape.

Thread lists include `view_id` and `scanned_through`. They bound candidate rows,
then filter inaccessible records. Consequently an empty page can have
`has_more=true`; `next_after` equals `scanned_through` and must advance. Clients
must not infer that the list has ended from an empty authorized result.

Change pages include `view_id` and `filtered=true`. Every returned event repeats
the matching view ID. The opaque cursor is bound to the account/share/entry/view
and can advance over hidden events. These pages can also be empty with more
remaining. Hidden thread/record identifiers and bodies are not returned.

Managed read-rule and comment-rule changes rotate the share's existing
collaboration epoch. Inventory moves, deletion and type changes also rotate it;
ordinary content/hash updates and new entries do not. An old change cursor then
returns `snapshot_required`, including after access is restored. Clients clear
their old remote snapshot and replay under current authority while retaining
drafts/outbox work. No event retention/pruning is enabled by this increment.

The client validates the requested view, forward page progress, target lineage
and response entry identity. Empty filtered pages are accepted only with explicit
matching Bases evidence. The ordinary note/image validators remain strict.
`base_definition_not_synced` retries with bounded backoff; disabled creation
pauses delivery; an unavailable saved view requires review. Pending work retains
its original anchor and operation ID. The native note/image session explicitly
refuses Base creation until a Bases surface is connected.

## Validation and remaining work

- 220 server regression tests passed, including 30 Bases discussion cases.
- Disposable PostgreSQL, migrated from empty, verified concurrent reader capture,
  duplicate sends, actual saved-view query filtering, reads/writes blocked behind
  revocation, stale ORM rejection and cursor invalidation after a record move.
- Actual plugin/HTTP tests verified durable draft and queued-target restart,
  delivery through comment-only access after a view rename, exact retry, distinct
  view lists, wrong-view rejection, a fully hidden update page, and regrant reset.
  The existing content and registry HTTP fixtures passed alongside them.
- Unit tests validate all four target kinds, malformed/unsupported anchors,
  capability negotiation, context binding, pagination and error classification.
  The plugin regression passed 1,892 tests with eleven opt-in tests skipped;
  preview build, lint and type gates passed with the existing baselines.

Run `tests/integration/base_discussions_postgres.py` with
`COLLABORATION_TEST_DATABASE_URL` pointing at an **empty disposable localhost
database named `collaboration_test`**. The client fixture is
`tests/integration/base_content_client.py --plugin <plugin-checkout>` and starts
its own temporary loopback HTTP/SQLite service. Neither fixture uses a native
vault or existing service.

Remaining: native saved-view selection, per-view remote caches/workers, durable
context capture and dependency preparation, target-aware mention choices and
permission UI, every Bases rendering adapter, all other agreed target kinds,
inline/provisional identities, image/Bases presence, mobile and resource checks.
This evidence establishes a protocol increment, not release readiness.
