# Discussion inbox preview

The server and Obsidian client preview are implemented, including local cache,
queued preference/read changes, foreground refresh and original-target navigation.
Native desktop mention/read/follow/mute/dismiss, reconnect and exact-reply
navigation checks now pass in a disposable actual-service fixture. Initial
small-group query measurements and PostgreSQL streaming checks also pass. The
remaining desktop/mobile and resource matrix is pending. This is not
a production enablement recommendation.

## Enablement and migration

Apply migration `202611060007` with the other control-plane migrations. The
`COLLABORATION_INBOX_ENABLED` setting defaults to false and also requires the
discussion and policy previews. `/v1/collaboration/capabilities` reports the
`discussion_inbox` feature. It does not change the overall `comments: false`
release capability.

When enabled at application startup, the SQL-backed projector runs in each
control-plane process. Claims use row locks and expiring leases, so it does not
require one designated process or an external broker. Disabling the setting
stops new candidates, API access and projection; existing jobs and user state
are retained. Enabling the feature does not synthesize notifications for old
comments. Existing participants still follow their threads by default.

No production setting or service was changed to implement or test this preview.

## Delivery rules

- A new thread notifies mentioned recipients, excluding its author.
- A reply notifies mentioned recipients and followers, excluding its author.
  A recipient receives one candidate per event; a mention takes precedence.
- Authors and participants follow by default. A separate explicit preference
  overrides this default, including after the user participates again.
- Mentions alone do not automatically subscribe their recipients. Unsubscribed
  users can still receive mentions; mute suppresses both mentions and replies.
- Edits notify recipients added relative to the previous immutable revision.
  Ordinary edits, reactions, resolution and reopening do not produce notices.
- If a mention is removed before projection, it is suppressed. A new reply
  still notifies an eligible subscriber as a reply in that case.
- Recipient account, membership, entry and exact Bases target dependencies
  are reauthorized when projecting and reading. Mute/unsubscribe are also
  rechecked at projection. Rejected candidates are completed without delivery;
  restoring access does not replay those notifications.
- Deleted messages do not appear in the inbox. If a newer message is removed,
  an earlier delivered, readable message can still represent the thread.

Candidate creation shares the comment's transaction with the revision, event,
receipt and picture references. Retrying an accepted command cannot enqueue
again. The primary key is `(share_id, sequence, user_id)`, deliberately stronger
than an event/recipient/reason key. Candidates contain identifiers, reason and
time; they do not copy comment bodies or display names.

Projection updates the inbox row and completes the candidate in one transaction.
A crash before commit leaves both unchanged. Expired claims can be reclaimed;
an old token cannot complete a successor's claim. Claims hold no authority locks
across transactions. Projection locks the recipient, share, membership and job
in that order, consistent with discussion and permission writers.

## API contract

All routes below require a session-backed credential and the enabled feature.
UUIDs identify the managed entry/thread, not a path. Readers may manage their
own inbox and subscriptions without comment/edit permission.

| Route under `/v1/collaboration` | Behavior |
| --- | --- |
| `GET /shares/{share}/inbox` | Per-share activity page, newest first. Query: `limit` (1–100, default 50), opaque `cursor`, `unread_only`, `include_muted`, `include_dismissed`. |
| `GET /shares/{share}/entries/{entry}/threads/{thread}/subscription` | Current effective `following`, `muted`, and `version`; version zero means the implicit participation default. |
| `POST .../subscription` | Body: `expected_version`, `following`, `muted`. Compare-and-set; a lost-response retry of the immediately accepted state is acknowledged. Conflicting changes return `stale_subscription_version`. |
| `POST .../inbox` | Body: `seen_version` and `action` (`read`, `unread`, `dismiss`). Returns the current inbox state. |

An inbox page contains `items` and `next_cursor` (null when exhausted). Each item
contains `thread_id`, `entry_id`, `scope_id`, `kind`, current share-relative
`path`, `view_id` (null outside Bases), `message_id`, current `message_revision`,
`reason`, `last_sequence`, UTC `activity_at`, effective `subscription`, `version`,
`read_version`, `dismissed_version`, and `unread`.

The client must resolve the managed entry through its local inventory, then use
the original saved view for Bases navigation. The path is a current label, not
identity or authority. Message bodies and anchors come from the ordinary
authorized discussion endpoints. Do not display cached message previews after
known access loss.

Each share is paginated separately so the server never locks every share for a
global inbox. The client can aggregate its configured shares using `activity_at`
and stable share/thread identifiers. Cursors are encrypted, expire after one day,
and are bound to recipient, share and filters. Invalid/changed cursors return
`inbox_cursor_reset`. Every page rechecks access; there are no unfiltered totals.
Activity pagination is not an immutable snapshot: reconcile refreshed windows
rather than permanently appending cached rows. The client replaces each loaded
window with freshly authorized pages, removing denied references that disappear.

## Read and dismissal semantics

`version` increments once per delivered candidate. It is independent of the
share sequence. This matters when a delayed older job is projected after newer
activity has already been read: it still becomes unread.

Read/dismiss record only the explicitly observed version, with monotonic maxima.
They never mark a concurrently delivered later version read. A client must not
send a current server version merely because the sidebar opened; it sends the
version corresponding to activity actually presented to the user. An ahead-of-
server version returns `inbox_version_unavailable`.

Dismiss hides the observed activity and also marks it read. A new delivery
reappears. Mark-unread requires the exact current notification version, so an
old device cannot undo a newer notification's state; it also restores a
dismissed entry. Read/unread choices apply in server acceptance order, while
notification versions protect concurrent newly delivered activity. Opening a
thread does not itself mutate inbox state.

## Resource and recovery behavior

The worker wakes every two seconds, stops a batch after 100 candidates or after
observing five seconds between candidates, and claims for 60 seconds. PostgreSQL
transactions have a five-second lock timeout and ten-second statement timeout;
an individual wait can exceed the between-candidate batch budget. Each projection is a short SQL
transaction without object-store, relay or network I/O. Errors retain the claim
until expiry and log an exception class rather than bound SQL/message contents.
Shutdown signals the worker and drains its active database task. A stopped batch
may leave a claim to expire; it does not discard queued notifications.

Indexes support pending jobs, recipient/share activity, thread followers and
delivered-message fallback. Inbox reads join thread/scope/file/message/subscription
metadata in one streamed query, with a correlated participation check only when
there is no explicit subscription. File rules load once per request after the
share read lock. Every target still passes its ordinary authorization checks;
Bases dependencies and deleted-message fallbacks may require additional queries.
Rows stream in batches of 100, and the cursor closes before transaction commit.
The permission-filtered scan can still inspect many previously accessible
entries. Large denied backlogs, long-thread participation scans and Bases query
cost remain release work. Bodies and pictures are loaded separately. There is no email,
native OS notification adapter, mobile background promise or custom push system.

The new tables must be included in coherent backups. Downgrade refuses to remove
populated notification, subscription or inbox tables; export/migration is
required first. Completed candidate retention, full backup/export/restore and
cache controls remain part of the broader implementation plan.

## Obsidian client preview

The plugin's Comment inbox command and sidebar aggregate configured shared
folders. Filters cover unread, all, mentions, muted and dismissed entries.
Read/unread, dismiss/restore and follow/mute choices persist locally while
offline. Readers may follow a discussion without receiving a notification first.
Opening an entry does not mark it read. References resolve through the current
managed inventory, then the original saved Bases view; a stale display path or
different active view cannot redirect navigation. Thread content is fetched
through the ordinary authorized route without advancing the event replay cursor.

The cache holds at most 500 entries and 500 separately loaded subscription
preferences per account/vault/share. Paging beyond that budget starts an explicit
older window with a return-to-latest control. Its start cursor survives refresh
and restart; expiry returns to the latest authorized page. At most 200 pending
choices are retained, independently of notification cache eviction.

Foreground refresh runs every 30 seconds, reauthorizing at most ten 50-entry
pages; background work stops. An IndexedDB lease serializes inbox delivery across
windows. Retry-After persists across restart, and ambiguous requests retain their
exact payload. Preference conflicts retain rejected choices for explicit review;
later dependent choices cannot overwrite another device's accepted preference.
Read acknowledgments cannot expose a newly arrived unseen notification version
for an accidental subsequent mark-read. Known access loss removes cached
references and fences delayed responses while retaining identifier-only queued
choices. Cache version 16 closes older workers lacking this cleanup behavior.

## Verification

`tests/test_discussion_inbox.py` and `tests/test_base_inbox.py` exercise the HTTP
contract, command rollback, exact retries, subscriptions, reader-only access,
mute/unsubscribe, mention edits, read/dismiss races, lease recovery, removals,
revocation, saved-view/record intersection authorization, pagination and shutdown.

`tests/integration/discussion_inbox_postgres.py` requires an empty, disposable
localhost `collaboration_test` database. It verifies migration/ORM agreement,
empty downgrade/upgrade, concurrent duplicate commands and claims, a projector
commit failure, concurrent completion, version updates, read-vs-reply races,
conflicting subscription changes, recipient revocation while a projector waits,
and refusal to downgrade populated state. It removes its synthetic schema on
completion. The existing Bases PostgreSQL suite also passes with the new head.

`tests/integration/discussions_client.py --plugin <plugin checkout> --inbox`
starts a disposable actual HTTP application with its startup/shutdown projector.
The plugin fixture checks mention delivery, duplicate-command deduplication,
targeted thread retrieval, offline read/follow, lost acknowledgment and database
reopen recovery, a concurrent subscribed reply, and current-access revocation.
It uses synthetic accounts and no production service.

Plugin store/protocol/DOM/host tests cover multiwindow leases, delayed responses,
read-version races, persisted retry delays, reader-only preferences, bounded older
windows, filters, keyboard focus, account changes, local file identity and saved
view navigation. The [native desktop record](../../evc-team-relay-obsidian/docs/comment-inbox-native-preview.md)
describes the verified subset and limits. Automated tests do not establish touch,
battery or OS-notification behavior.

The November 7 native run found that SQLite discussion/history timestamps lost
their UTC offset after a database round trip; inbox timestamps were already
normalized. Discussion thread/message/history/target-history serialization now
returns explicit UTC, preserving correct local display. The new HTTP regression
checks eight timestamps through all affected read routes and failed before the
fix. The discussion, inbox, search and reattachment set passes 152 tests; changed
Python files pass Ruff. No schema change is needed. The native fixture used the
old running server module, so corrected native date rendering remains to verify.

## Small-group query measurements, November 7

The original list endpoint repeated metadata, permission and subscription reads
per item. `tests/test_discussion_inbox_resources.py` measures the full HTTP route
as a non-owner reader, using three reads per synthetic case. It enforces a
20-SELECT ceiling for ordinary Markdown pages and verifies that no message
revision bodies are queried. Timings are observations, not CI pass thresholds.

| Notifications | Before SELECTs | After SELECTs | Before median | After median |
| --- | ---: | ---: | ---: | ---: |
| 1, one note | 17 | 11 | 5.02 ms | 3.11 ms |
| 10, one note | 71 | 11 | 9.50 ms | 4.26 ms |
| 50, one note | 311 | 11 | 34.78 ms | 5.88 ms |
| 50, separate notes | Not measured | 11 | Not measured | 9.28 ms |

These are local in-memory SQLite/TestClient measurements, not Pi/network/mobile
performance claims. A disposable PostgreSQL container limited to one CPU and
256 MiB also returned 50 rows with 11 SELECTs in each of three samples (median
8.22 ms for the service call over localhost). Its test asserts use of a named
server-side cursor, verifies the remaining older page, and holds a permission
writer's share lock while an inbox read waits. That read sees the committed
denial; the next request sees a restored grant rather than cached rules.

The 40-test inbox/Bases/resource set passes. The expanded PostgreSQL runner also
passes its existing migration, projection, concurrency, revocation and downgrade
checks. The actual HTTP client/startup-worker/offline-recovery scenario passes.
Logs: `/private/tmp/inbox-query-{before,regression,postgres,real-stack}.log`.
The temporary PostgreSQL container was stopped and removed. No schema or client
wire-format change is needed.
