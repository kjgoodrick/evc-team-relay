# Discussion search preview

The server and Obsidian sidebar implement current-comment search and filters for
open/resolved state, matching messages written by the current user, and matching
messages mentioning that user. Search can cover the current note/saved Bases
view or the connected shared folder. Native desktop/mobile acceptance and the
locally derived unanchored-target filter remain unfinished.

## Schema and enablement

Migration `202611070002` adds a derived current-message text projection, current
mention references and a PostgreSQL GIN expression index. It backfills only the
current nondeleted revision in bounded batches. The immutable history tables
are unchanged. Projection updates commit with the message, revision, event and
receipt; rejected/rolled-back commands cannot change search independently.

Apply the normal migrations before running the new service source. Set
`COLLABORATION_SEARCH_ENABLED=true` together with the policy/discussion preview
settings to expose search. The setting defaults to false; capabilities report
`discussion_search`, while the overall release capability remains incomplete.
Index maintenance continues while search is disabled, so enabling it requires
no second catch-up worker. No broker or separate search service is introduced.

Downgrading this migration discards only derived search data. Re-upgrading
rebuilds it from authoritative current revisions. A schema downgrade must use
matching older service source; the new command implementation expects the new
tables. This is not a complete collaboration backup/rollback rehearsal.

No production migration, setting or deployment was performed for this preview.

## Search contract

`POST /v1/collaboration/shares/{share_id}/search` accepts:

```json
{
  "protocol_version": 1,
  "query": "review image",
  "state": "all",
  "mine": false,
  "mentions": false,
  "entry_id": "optional-managed-file-uuid",
  "view_id": "required-with-a-Bases-file",
  "cursor": null,
  "limit": 30
}
```

Omit optional IDs when searching the shared folder. A view requires its original
file. The text is at most 256 printable Unicode characters and 32 normalized
words; limit is 1–50. An empty query browses by the other filters. POST keeps
private search terms out of URL/access-log query strings. Validation and storage
errors return stable codes without reflecting query parameters.

Search matches all words after Unicode compatibility normalization and case
folding. It indexes visible text, code, list text, link labels and picture alt
text. It excludes link destinations, UUIDs, mention display names, picture
contents/OCR, removed messages and old revisions. It does not implement stemming,
prefixes, phrases or search-operator syntax. The words are normalized before
PostgreSQL tokenization; the SQLite test fixture uses the same normalized words.

A page returns `protocol_version`, `items` and `next_cursor`. Each item carries
the current thread summary, file ID/kind/path, original saved-view ID (null
outside Bases), matching message ID/revision/creation sequence, and a plain-text
excerpt of at most 480 characters. There is one latest matching message per
thread. Results are ordered by thread creation, newest first. Both personal
filters, when selected, must match that same message.

## Authorization, pagination and resources

Reads lock the current user/session, share and membership using the discussion
lock order. Managed-file permission precedence, explicit denies and literal
case-sensitive paths are applied in SQL. Up to 100 candidate threads are then
checked with the existing exact Bases target validator before their IDs enter
the full-text query. Record generations, groups, formulas, lanes and marker
dependencies retain the same authorization rules as ordinary thread reads.

The server returns no denied result IDs, excerpts or total counts. Continuations
represent bounded scanning, so an empty page can still offer a continuation.
Cursors are encrypted and bound to account, share, share epoch and normalized
filters, with a one-hour expiry. They retain the initial upper creation sequence;
new threads appear when starting a new search. Every page rechecks current
permissions, message revisions and thread state. This is live browsing, not a
historical snapshot or a snapshot of message contents.

Database operations have transaction-local five-second lock and ten-second
statement limits. Client responses are bounded to 512 KiB and validated for
scope, result order, identities, revisions, state and excerpt lengths.

The explicit PostgreSQL text-search configuration matches the indexed
expression, following the [PostgreSQL index documentation](https://www.postgresql.org/docs/current/textsearch-tables.html).
The query uses plain all-word parsing rather than accepting user-provided query
operators; PostgreSQL describes that distinction in its [query parsing documentation](https://www.postgresql.org/docs/current/textsearch-controls.html).

## Sidebar behavior

**Search and filter comments** opens the form without replacing the comment
composer or its draft. Typing changes only the local form; submitting starts a
search. The panel keeps at most one request in flight and coalesces a newer
submission behind it. Superseded responses cannot restore old results.

Results use text nodes, not returned HTML. Opening a hit uses the existing
authorized direct-link navigation and fetches the current message again. Renames,
different local mounts and the original saved Bases view retain their identities.
The sidebar never treats a result excerpt as authorization to open its target.

Visible results are rechecked every 15 seconds. Unchanged results preserve
keyboard focus. Hidden/disposed panels stop polling; known permission loss or
logout clears results immediately. Results and queries are not persisted in the
local cache. Search currently requires a connection; ordinary cached discussions
and queued offline work remain available through their existing interfaces.

Pagination retains at most 50 previous cursors. A nonmatching scan page offers
**Continue searching**. A changed/expired cursor requires restarting the search.
The unanchored filter requires editor-derived target state and is not presented
as a server text-search filter.

## Evidence and remaining acceptance

- Full plugin suite: 2,878 tests in 224 suites pass; 18 opt-in tests in 17 suites
  are skipped. Preview build/main types and the frozen test-type gate pass.
  Existing 15 Svelte warnings and 206 known test-type errors/45 signatures remain.
- Real Python service/TypeScript client search passes current-message matching,
  own-message/mention/state filters, message navigation, edits and live revocation.
- Disposable PostgreSQL passes populated backfill, schema/index parity, command
  rollback, edits, concurrent permission-writer blocking and populated index
  downgrade/rebuild. A selective query uses the GIN index over 3,001 synthetic
  documents. This proves index use, not a release latency or battery budget.
- Server regression: 392 tests pass across discussions, search, pictures,
  recovery, inbox, Bases targets, timeline targets and collaboration policy.
  Existing dependency and synthetic-fixture warnings remain.
- Native desktop/mobile, accessibility, resource measurements, broader
  popout use and the unanchored-target filter remain required release work.
