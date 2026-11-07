# Discussion inbox preview

The server portion of the in-app inbox is implemented. The Obsidian inbox UI,
local cache, queued preference/read changes, foreground refresh and navigation
are not connected yet. This is not a complete end-user feature or a production
enablement recommendation.

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
Activity pagination is not an immutable snapshot: refresh the first page after
draining older pages, and reconcile changes rather than permanently appending
cached rows. A later authorization/cache protocol must also remove denied rows
that disappear from refreshed results.

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
delivered-message fallback. The current permission-filtered page scan uses
bounded fetch batches but can inspect many previously accessible entries; bulk
authorization/query-count and large-backlog performance measurement remain
release work. Bodies and pictures are loaded separately. There is no email,
native OS notification adapter, mobile background promise or custom push system.

The new tables must be included in coherent backups. Downgrade refuses to remove
populated notification, subscription or inbox tables; export/migration is
required first. Completed candidate retention, full backup/export/restore and
client cache reconciliation remain part of the broader implementation plan.

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

These checks prove the server increment. They do not prove the absent Obsidian
inbox interface, native/mobile lifecycle, local cache or end-user navigation.
