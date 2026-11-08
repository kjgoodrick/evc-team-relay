# Restricted discussion recovery preview

November 7, 2026. Ordinary discussion reads hide removed bodies and pictures from
every role. This separate administrative API allows an explicit, audited online
read within the retention window. It does not restore a message, enqueue a
command, populate an ordinary client cache or reopen a thread. Native recovery
controls and complete release acceptance remain unfinished. An independently
gated retention worker now erases expired content and retires its last picture
references for the existing object sweep.

## Enablement and authority

Apply migration `202611070003`, then enable
`COLLABORATION_MODERATION_RECOVERY_ENABLED=true` alongside collaboration policy
and discussions. The flag defaults off; capability
`discussion_moderation_recovery` advertises it. Share owners and server
administrators must still have a current authenticated session, readable managed
entry and authorized exact discussion target. Authorship, read/comment permission
and knowledge of a picture/message ID do not grant recovery rights.

Every request is a POST with a nonblank plain-text `reason` of at most 512
characters. `protocol_version` is 1. This API has no GET, outbox or polling path.
It rejects live messages in live threads, removed content with missing clocks,
expired windows and purged content. The normal History and picture routes remain
unavailable even after a successful administrative read.

`COLLABORATION_REMOVED_CONTENT_RETENTION_DAYS` defaults to 30 (configurable from
1 through 3,650). The earliest applicable message/thread removal timestamp starts
the window. A later thread removal cannot extend an already removed message's
window. Authorization and the clock are rechecked after picture storage reads.
The flag disables recovery immediately without making ordinary reads less strict.

## Endpoints

All paths below are under
`/v1/collaboration/shares/{share}/entries/{entry}/threads/{thread}/messages/{message}`.

| POST suffix | Request | Response |
| --- | --- | --- |
| `/moderation-history` | Reason, optional `after` revision and `limit` (default 20, maximum 50) | Immutable revisions with bodies, actors/timestamps and removal reasons, removal flags, deadline, audit ID and keyset pagination. |
| `/moderation-pictures/{picture}` | Reason | Sanitized public picture metadata and audit ID; no private object keys. |
| `/moderation-pictures/{picture}/{content\|thumbnail}` | Reason | Verified sanitized image bytes, `X-Discussion-Audit-Id`, `Cache-Control: no-store` and `nosniff`. |

A picture must be attached to the same scope and referenced by a revision of the
exact requested message. A staged, expired, unrelated or unavailable picture is
not recoverable through that target. Only existing sanitized renditions are
returned; no original uploads or external URL fetches are added.

History reads cap aggregate bodies near 384 KiB and encoded responses at 512 KiB.
There are two concurrent administrative database captures per process, 5-second
PostgreSQL lock waits and 10-second statement limits. Picture downloads share the
existing bounded picture-download slots. No database authorization lock spans
object-store I/O. Each page/file request independently reauthorizes; callers must
stop after denial rather than presenting old content as newly authorized.

## Audit and failure behavior

Every disclosed page, metadata response and picture creates a
`discussion_recovery_read` audit record. It records actor, share/entry/thread/
message identities, the supplied reason, disclosed revision numbers and picture/
variant where applicable. It stores no comment bodies, image bytes or object keys.
Existing administrative audit-log access rules apply.

Response bytes are prepared under authorization locks and returned only after
the audit transaction commits. Failed commits roll back and disclose no content.
Audit storage cannot be disabled or bypassed by a caller. A lost response may
leave a committed audit record; an explicit retry creates a new read/audit record.
Audit records establish that disclosure was authorized and prepared, not that a
human necessarily received or viewed the response.

## Migration and rollback

Threads and messages gain durable `deleted_at` and `content_purged_at` fields and
removal-time indexes. New removals set their timestamp in the same transaction as
the tombstone/event. Upgrade preserves all content and backfills message clocks
from their edit/current-revision timestamp and thread clocks from the removal
event. Missing historical evidence gets a conservative new window from migration
time, never an invented creation-time removal date. Upgrade performs no purge.

The audit enum label is additive. Schema downgrade does not remove audit records
or the PostgreSQL enum label. **Do not roll the server back to code unable to read
that label after recovery has produced audit records.** The safe preview rollback
is to disable this feature while retaining the audit-aware backend/migration.
A full production rollback rehearsal remains required. Client rollback must also
preserve the ordinary removed-content boundary.

The recovery deadline applies even with cleanup disabled. Administrator recovery
controls, cache/offline disclosure acceptance and native/mobile tests remain.
Existing exports or offline copies cannot be retroactively erased.

## Expired-content cleanup

Apply migration `202611070004` (a message/revision index on picture references),
then independently opt into `COLLABORATION_REMOVED_CONTENT_CLEANUP_ENABLED=true`.
It defaults off. The worker starts after 30 seconds and runs every 60 seconds by
default (`COLLABORATION_REMOVED_CONTENT_CLEANUP_INTERVAL_SECONDS`, 10–86,400).
Changing environment settings requires the normal controlled process restart;
editing an environment file alone does not stop a running worker.

Each batch selects at most 100 messages, configurable through
`COLLABORATION_REMOVED_CONTENT_CLEANUP_BATCH_SIZE` (1–1,000). It uses the same
retention window as administrative recovery. Missing clocks and live/recent
content are preserved. A removed thread makes all its messages eligible at the
thread deadline; an earlier individual removal can make a message eligible
sooner. Each transaction reacquires the share lock and rechecks identity,
eligibility and configuration before modifying rows.

For each eligible message, one transaction clears every revision's body and
removal reason, reactions, search projections and historical picture references,
and records `content_purged_at`. IDs, revision numbers, authors/times, metadata-only
events, audit records and idempotency receipts remain. Replaying a previously
accepted receipt does not recreate content; a new command cannot reuse its IDs.
When all children of an expired removed thread are purged, its target payloads
and removal reason are erased too. Such a thread is unavailable even through
the safe state route. Bases receipts and change feeds cannot use erased target
evidence to authorize a read; saved-view lists, exports and inboxes omit it.

An attached picture becomes expired in the same transaction only if the message
held its last retained reference in that scope. Other live or retained-history
references protect it. SQL purge performs no object-store I/O. Physical
reclamation requires the existing independent
`COLLABORATION_PICTURE_CLEANUP_ENABLED` sweep; that sweep retires the namespace
before deletion and retries interrupted object work. These newly expired
pictures do not wait an additional orphan grace period. Disabling the picture
sweep leaves bytes stored but unavailable through ordinary or recovery routes.

PostgreSQL skips shares held by another transaction, uses 5-second lock and
10-second statement limits, and applies a cooperative 30-second batch budget.
Each message/finalized thread commits independently, so interruptions preserve
completed progress and roll back the active erasure. Slow statements may run
until their statement timeout; the budget is not a hard process deadline.
Shutdown signals cancellation and drains active work. Progress is stored in the
purge timestamps, so restart requires no external cursor. Maintenance logs contain
counts and exception classes, never bodies or target payloads.

Before coherent backup or restore, quiesce writes and drain/disable **both**
cleanup workers in every process. Keep them disabled while restoring and
verifying the matching database and private object-store snapshot. The broader
coherent backup/restore workflow and rollout rehearsal are still outstanding;
independent live SQL/object copies do not establish a coherent discussion backup.
Disabling cleanup or downgrading its index does not recover already erased
content. SQL erasure is logical deletion, not forensic erasure of database pages,
transaction logs, backups, audit reasons or client-held copies; those stores have
their own retention policies.

## Verification

Focused API tests cover moderator/author/reader boundaries, explicit reasons,
missing/expired/purged targets, mixed identities, session loss, current Bases
record access, bounded pages, audit transaction failures and read capacity. Picture
checks cover exact message references, both renditions and expiration, purge,
session/feature/storage changes and audit failure during download. SQLite
migration tests preserve content and verify timestamp backfill/downgrade/re-upgrade.

The actual PostgreSQL drill starts with the old audit enum, upgrades it, preserves
removal time/body data, proves an inserted audit rolls back on commit failure,
and verifies session revocation waits until the audit and prepared read commit.
A subsequent revoked-session read fails. Actual TypeScript/HTTP tests verify
explicit recovery of rich history while ordinary routes remain denied. Actual
HTTP/MinIO tests verify audited metadata and exact sanitized picture bytes.
All task-created services/containers are stopped and removed after these drills.
See the root implementation status for final regression counts.

Retention verification also covers bounded child/anchor erasure, missing clocks,
configuration changes, cancellation/failure rollback, receipt replay, shared
picture references and safe purged-Bases polling/export/inbox behavior. A real
PostgreSQL drill verifies that cleanup skips an active audited read, rolls back
interrupted erasure and preserves shared pictures until their last reference is
removed. The actual HTTP/MinIO drill verifies SQL-only erasure first, subsequent
private-object reclamation, preservation of live pictures, restart idempotency
and denied administrative recovery after purge. These drills use disposable
fixtures and leave existing services unchanged.
