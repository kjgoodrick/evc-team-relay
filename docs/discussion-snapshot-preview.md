# Current-state discussion synchronization preview

`COLLABORATION_SNAPSHOT_ENABLED` independently enables the
`discussion_snapshot` capability. Policy and discussion commands must also be
enabled. The default is off; existing clients and deployments continue using
retained events. This change does not prune events or deploy a service.

## Contract and consistency

`GET /v1/collaboration/shares/{share}/entries/{entry}/snapshot` returns one complete
authorized baseline. Bases requests require the exact saved `view_id`. Its
version-1 JSON includes `entry_id`, optional `view_id`, `restore_generation`,
`through_sequence`, an opaque `cursor`, and ordered groups of current thread
metadata, current anchor and current messages with aggregate reactions. Empty
scopes return an empty baseline without creating database rows.

The ordinary actor/session/share/entry permission guards stay locked until all
JSON is encoded. Comment and permission writers acquire conflicting locks. The
share sequence at that boundary is H; the returned ordinary change cursor starts
strictly after H. It carries the same actor/share/scope/view/permission epoch and
expiry checks as ordinary change polling. An edit committed after the boundary
therefore appears in catch-up, not halfway through the baseline.

Current dependency authorization applies separately to each Bases thread.
Removed threads are omitted. Removed messages retain their metadata with no body
or reactions for every role, including moderators. Historical bodies/anchors,
picture bytes, private inbox state and participant names are not part of a
baseline. Missing current revisions, empty threads and malformed current bodies
fail the entire response; they cannot become apparently complete partial state.
The HTTP response uses `Cache-Control: no-store`.

## Resource limits

One response is limited to 5,000 thread/message records and 8 MiB of encoded JSON.
Enumeration permits at most 10,000 scanned records and a ten-second enumeration
deadline. PostgreSQL uses a five-second lock timeout and ten-second statement
timeout. Two snapshot operations per server process may run simultaneously;
excess requests receive a retryable 503 with `Retry-After: 2`, without a queue.
Capacity is released on success or failure. SQL failures roll back and expose
sanitized errors. These bounds are not a global cross-process quota.

Rows stream in batches of 50. Message reactions use one aggregate query per
batch, rather than one per message. Thread/dependency checks still cost work per
thread. JSON assembly and encoding materialize the bounded response in memory;
this is a small-group baseline, not a large resumable snapshot service.

Disabled snapshots return 404 `discussion_snapshot_disabled`; a scope exceeding
the bounds returns 413 `discussion_snapshot_limit`, never a partial baseline.
The client can fall back to retained events for these two explicit cases.

## Client behavior and compatibility

Compatible clients request a baseline on first synchronization or after an
expired/invalid cursor reset. They validate entry/view identity, restore
generation, ordering, uniqueness, record shapes and whole-response limits before
changing their cache. One IndexedDB transaction replaces remote records and the
cursor, increments the cache generation and removes this scope's downloaded
picture renditions. Newer current-state reads/tombstones survive an older
baseline. Saved drafts, queued work and original picture sources remain intact.
Late readers/downloads cannot write through the old cache generation/epoch.

Cursor resets reauthorize before fetching the new baseline. Access denial clears
remote content and pauses synchronization while preserving local unsent work.
Busy/network failures use normal backoff. A stopped or replaced context cannot
install a late baseline. An unobserved restore generation is refused; receiving
a snapshot does not authorize switching file histories or resending queued work.

Legacy servers keep their existing event replay path. A durable retained-event
boundary and explicit change-reader admission are now implemented separately;
see the [event-boundary contract](discussion-event-floor-preview.md). Event
pruning and larger/resumable baseline handling remain unfinished. **Do not prune
event history based only on these capabilities**: scopes over the snapshot limits
still depend on retained replay.

## Evidence

- Server tests cover current state, exact catch-up, empty scopes, removed content
  across roles, view/dependency access, corrupt storage, limits, rollback and
  restore generation.
- The snapshot/discussion/export/restore-generation regression passes 134 tests;
  changed Python sources pass Ruff and formatting checks.
- Client tests cover bounded transport, atomic replacement/rollback, concurrent
  installs, newer reads, account/scope isolation, original pictures, late writes,
  expired cursors, authorization, cancellation and explicit legacy fallback.
- Real TypeScript HTTP integration creates/edits/reacts/replies, replaces stale
  cache, recovers an invalid cursor and preserves drafts/queued work after a
  permission revoke.
- Disposable PostgreSQL proves edit and permission writers wait through final
  response encoding, the cursor captures the next edit, and a subsequent read
  refuses revoked access. A 121-message baseline uses 16 SELECTs and took 12.6 ms
  on this local fixture. This is not physical mobile or network performance.

Run `tests/integration/discussions_client.py --plugin <plugin-path> --snapshot`
with the control-plane virtualenv and plugin Node toolchain. The PostgreSQL proof
is `tests/integration/discussion_snapshot_postgres.py`; it requires an empty
loopback database named `collaboration_test` through
`COLLABORATION_TEST_DATABASE_URL`. Both use synthetic data only.

Native desktop/mobile acceptance, physical resource measurements and production
enablement are separate and remain pending.
