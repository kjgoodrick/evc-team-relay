# Saved-view discussion protocol preview

This increment implements standalone Bases discussion targets for saved views,
records/items, cells and note/file property headers. It includes server
authorization, client sessions/offline queues and an explicit native sidebar
target picker. Contextual native table/cards/list controls now connect capture,
markers and exact navigation; integrated table behavior has desktop evidence.
The server also maintains a definition catalogue for formulas, summaries and
grouping rules. Their discussion targets, computed-value dependencies, inline definitions,
Time & Line targets and presence remain required work.

Creation requires `COLLABORATION_BASES_DISCUSSIONS_ENABLED=true` together with
the existing policy, discussions and registry flags. All remain opt-in. The
general `comments` and `bases` release capabilities remain false. The new
`discussion_bases` capability lists exactly `view`, `item`, `cell`, `property`
in `discussion_base_targets`; it does not advertise other targets. Upgrade
clients before enabling creation. Disabling this creation flag preserves reads,
replies and recovery for existing discussions while the discussion API remains
available.

## Supported file records

`discussion_base_record_types` advertises `markdown`, `image`, `audio`, `video`,
`pdf`, `canvas` and `file` when creation is enabled. Older servers that omit this
field accept only Markdown records. The client validates and caches the list,
ignores unknown future kinds, and reverts to the old limit after a downgrade.
Folders, missing/deleted records and records from another share are refused.
This is support for Bases rows/cells representing those files; it does not add
PDF-page, Canvas-node or attachment-content annotations.

Note-property cells require Markdown records. File-property cells and item
threads support every advertised file kind. The server derives access from the
record ID and keeps the same permission intersection for every kind. There is
no attachment upload/download or file-edit authority granted by commenting.

A managed inventory type conversion now rotates `collaboration_generation`
within the existing share-locked transaction. Exact conversion retries do not
rotate it again; converting back starts another generation and cannot revive
old anchors. Renames and ordinary content updates preserve generation. Old
threads/drafts are retained under their original lineage rather than applied to
new file semantics. No database migration is required.

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
  "record_id": "<optional managed file record UUID>",
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
    "record_id": "<managed file record UUID>",
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
own capture/dependency contracts before implementation. The definition identity
catalogue below supplies part of that contract; it does not enable new anchors.

## Formula, summary and grouping definition catalogue

`POST /v1/collaboration/shares/{share}/entries/{base}/bases/target-definitions`
accepts `{view_id, expected_revision?, after?}`. Continuing a page requires its
registry revision; any intervening registry change returns `stale_base_revision`.
`features.discussion_base_definition_catalog` advertises this read interface.
The existing four discussion target kinds remain unchanged.

An authorized reader receives definition/view/generation/registry lineage,
`targets`, `has_more`, `next_after`, and `computed_values: false`. Each target has
an opaque `id`, a `kind` (`formula`, `summary`, or `group`), its last-changing
`revision`, a semantic `fingerprint`, and a typed `property` locator. A summary
also identifies its configured summary name. Formula names are view-local even
though their expressions are declared globally in the `.base` file. A grouping
definition identifies the rule; it does not identify any particular lane/value.

The catalogue reads only the verified server-held configuration. It accepts no
expressions, computed values, member lists or client dependency claims. A
definition hash must still match the synchronized file, and the saved view must
be active. The reader needs no file-edit/comment permission to inspect it.
The existing share lock serializes initialization with synchronization and
permission changes. No object storage access or expression evaluation occurs
inside that lock.

Migration `202611060005` adds server-owned identities outside vault content.
Registry updates refresh them in the same transaction. Existing registries are
initialized lazily through the catalogue. A view rename, layout/column reorder,
or group direction/order change preserves semantic identity. Editing a relevant
definition advances its revision; deletion, ambiguity and archival deactivate
it. Restoring the same configuration advances the revision again. A formula
rename gets a separate identity. Gaps caused by older writers conservatively
invalidate proof, including an unobserved change followed by a revert.

For this initial compiler, every formula is included in the dependency hash of
formula targets and formula-based grouping. Summaries conservatively include
all formula/custom-summary definitions and their result-set filter/limit inputs;
sort is included when a limit makes it significant. This can request review
after an unrelated formula edit, but covers dynamic and transitive references
without attempting an incomplete expression parser. Later computed-value
capture still requires independently verified record/access dependencies.

Read pages contain at most 100 definitions. Each view supports at most 256 and
each `.base` at most 1,024 active definitions. Exceeding either budget disables
this catalogue with `base_definition_targets_limit`, while ordinary Bases sync
and existing view/record/cell/property discussions continue. Ambiguous property
aliases are omitted instead of choosing one assignment. Unavailable identities
remain stored for future target review/repair. Downgrade refuses to discard a
nonempty identity table; export/migration is required first.

Remaining work: client catalogue/cache integration; typed formula/summary/group
anchors; group values and computed-output authorization; native adapters,
markers/navigation and offline capture/recovery. This endpoint does not make
those comments usable yet. The full feature scope remains in the repository's
implementation plan.

The input structure follows the official [Bases syntax](https://obsidian.md/help/bases/syntax)
and [table summary documentation](https://obsidian.md/help/bases/views/table).

## Discussion target validity

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
require current read access to the same-share, live file record and its
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
its original anchor and operation ID. Native Bases sessions use the local scope
`base:<entry UUID>:<view UUID>` for separate drafts, queues, caches and cursors.
The transport extracts the managed entry for HTTP and validates the view. Schema
version 9 preserves older local work while preventing old workers from opening
the new queues. This requires no server migration.

The sidebar explicitly selects a saved discussion view independently of the
native layout. Its fallback picker captures view, managed file record, cell
and note/file property-header targets. It searches local shared files, including
records outside the current view filters; it does not infer a record from the
rendered row's text or order. Offline capture uses verified cached context and
record grants, while delivery rechecks the server. Native markers/navigation,
query-aware capture and the remaining target kinds still require adapters.

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

The subsequent client increment passed **1,906 plugin tests** (twelve opt-in
tests skipped), the three actual HTTP suites/four tests, preview build, lint and
the existing type gates. Added session coverage verifies offline restart/replay,
per-view local entities/cursors and cache refresh after record revocation and
regrant. Desktop Obsidian 1.13.7 accepted all four target kinds, preserved a cell
draft through reload and kept views separate without changing source bytes.
Cached comments and queued offline work also survived a native reload.
The final offline test exposed missing cached authority after a snapshot reset.
The strengthened actual HTTP fixture reproduced that failure; the session now
reauthorizes before replay, preserving the verified read grant/generation for
offline startup. A subsequent native check now verifies automatic reset/regrant cache
replacement and an offline reload; broader reviewer/mobile acceptance remains. Offline saved-view choice can use the existing registry cache
when a same-account discussion session retains read access and no inventory
provider exists; known inventory denial is never overridden.

Remaining: target-aware mention choices and permission UI, every Bases inline
rendering/navigation adapter, all other agreed target kinds,
inline/provisional identities, image/Bases presence, mobile and resource
checks. This evidence establishes a sidebar preview, not release readiness.


The file-record increment passed 77 Bases/inventory tests plus 165 related server
regressions, 1,916 plugin tests (twelve opt-in skips), the three actual HTTP
suites/four tests and the preview build/lint/type gates. Coverage includes every
advertised kind, all record-dependent
mutation/read/receipt paths, picture access after object I/O, type conversion,
exact retries, content changes and conversion back. The actual client HTTP
fixture now runs the session/offline/revocation matrix on an image file-property
cell and checks content updates, rename, conversion and retained stale drafts.

Native Obsidian 1.13.7 selected image, CSV and `.base` records, sent image-cell
and CSV-item comments, restored an image-cell draft and retained an offline CSV
queue after reload. Two policy changes forced automatic cache generations 1 → 2
→ 3 while preserving the owner's read grant and two threads. Both remained
readable after services stopped and the vault reloaded. This establishes the
native snapshot-reset cache fix; it is not a native reviewer-revocation or mobile
matrix. The source/image/CSV bytes were unchanged, and the disposable services,
database and test vault were closed. Final queued native delivery was not tested;
actual HTTP verifies restart and replay with comment-only access.
