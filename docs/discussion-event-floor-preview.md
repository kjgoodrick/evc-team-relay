# Retained discussion event boundary

This is a compatibility foundation for event retention. It does **not** enable
a pruning worker, remove production events or impose a new history policy.
Active discussions, revisions, operation receipts, drafts and queued work are
unchanged. The agreed event replay window remains 30 days when safe pruning is
eventually enabled.

## Durable boundary and read admission

Migration `202611080002` adds `shares.discussion_event_floor`, initially zero.
Events at or below that sequence may no longer be available. Database constraints
require zero through the share's current collaboration sequence. A future worker
must advance the boundary and remove its event prefix atomically under the share
write lock; readers already hold the corresponding share read lock.

The change endpoint accepts `sync_protocol=1`, declaring support for the existing
current-state snapshot recovery protocol. After normal authentication, scope/view
authorization and cursor validation:

- A cursor at or above the boundary can read the retained tail, including from a
  legacy client. Equality is valid because changes start strictly after a cursor.
- A client below the boundary receives 409 `snapshot_required` when it declares
  protocol 1, or 426 `discussion_sync_upgrade_required` otherwise.
- No cursor means sequence zero, so an initial request cannot silently treat a
  pruned tail as the complete discussion history.
- A zero boundary preserves ordinary initial event replay. Invalid/expired
  cursors retain their existing reset behavior.

The plugin declares protocol 1 on change reads. It already reauthorizes, clears
obsolete remote state, installs a complete snapshot and resumes catch-up after a
cursor reset. Its own drafts, queued work and original picture sources survive.
The new upgrade error is sanitized and pauses synchronization. Disabling snapshot
creation does not erase a nonzero boundary or make partial replay safe.

## Restore and rollback

Restore plans capture the event boundary with the share sequence, epoch and
generation. A changed boundary invalidates a prepared rotation plan even if no
new discussion was posted. As with other schema changes, generate a fresh
maintenance plan against the exact current migration head.

The migration can be reversed while all boundaries are zero. Once any boundary
is nonzero, downgrade refuses before removing the constraint or column: removing
this metadata could make an older server report incomplete history as complete.
Do not clear the boundary to force a code rollback; a coherent restore procedure
must retain the data and compatible reader behavior.

## Remaining retention work

Automatic pruning, its age/prefix index and bounded worker, complete recovery for
scopes above the current 5,000-record/8 MiB snapshot limit, and operator enablement
remain unfinished. **Do not prune events based only on this boundary protocol.**
It prevents silent partial replay but does not itself guarantee that every scope
can obtain a new baseline. No production capability has been enabled by this work.

## Evidence

The server selection passes 151 discussion/snapshot/restore tests. Cases cover
missing/old/current cursors, zero and nonzero boundaries, disabled snapshots,
authorization ordering, invalid database values and stale restore plans. The
focused client selection passes 94 tests, including encoded protocol requests,
sanitized upgrade errors and existing snapshot/reset/draft preservation behavior.

The disposable PostgreSQL proof runs the real migrations against an existing
share, checks zero defaults and reversible unused boundaries, verifies an active
reader blocks a retention writer, removes only synthetic fixture events, recovers
all current discussions through a snapshot, and refuses an unsafe downgrade.
The separate real HTTP/client snapshot fixture also passes. Owned containers,
processes and temporary data are cleaned up. Native/mobile acceptance and
production enablement are separate.

The full plugin regression passes **4,599 tests across 309 suites** (41 tests in
27 opt-in suites skipped). Main types, changed-source lint/formatting and the
preview build pass with the existing 15 build warnings. The test-type ratchet
remains 205 errors/44 signatures against the frozen 207/46 baseline. The existing
disposable PostgreSQL restore-administration drill also passes at the new schema
head, including CLI rotation/receipt retry, competing plans, superseded receipts,
connected-client refusal and protected receipt downgrade.

Run `tests/integration/discussion_event_floor_postgres.py` against an empty
loopback PostgreSQL database named `collaboration_test`, using
`COLLABORATION_TEST_DATABASE_URL`. It is not a production maintenance command.
