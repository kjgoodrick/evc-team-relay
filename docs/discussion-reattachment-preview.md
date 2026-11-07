# Discussion target reattachment

The collaboration API can move an existing discussion to another target in the
same note, directly opened image, or saved Bases view. Replies, message history,
reactions, subscriptions and resolved state stay on the original thread. Each
changed target adds an immutable revision with actor and time. The client
provides explicit review, retry/cancellation recovery and target history.

This is a development preview. Native text-to-text and text-to-whole-note moves,
including a competing reviewer move and stale-save recovery, have desktop
evidence. The full image/Bases/timeline/mobile acceptance matrix remains.

## Enablement and compatibility

Set `COLLABORATION_REATTACH_ENABLED=true` alongside the existing policy and
discussion flags. The new flag defaults to false and is advertised as
`features.discussion_reattach`. Target-specific capabilities still apply. Text,
block and embedded-image moves require the existing private relay anchor barrier.
Direct managed-image geometry moves do not require that text barrier.

No schema migration is needed: the implementation uses the existing immutable
`DiscussionAnchorRevision` table, thread anchor revision and command receipts.
It does not introduce a service, broker or Markdown sidecar. No production
configuration, deployment or service restart was performed for this increment.

Target moves emit the existing protocol-v1 `thread.state` event with additional
`change_type: "thread.reattach"` and `anchor_revision` fields. The activity
sequence advances; state revision does not. Existing clients hydrate current
thread/anchor state on this event, so a new event name cannot stop their feed.
The overall release `comments` capability remains false.

## Command and authorization

`POST /v1/collaboration/commands` uses the ordinary versioned envelope:

```json
{
  "protocol_version": 1,
  "operation_id": "stable-uuid-for-this-exact-request",
  "share_id": "share-uuid",
  "entry_id": "original-file-uuid",
  "command": {
    "type": "thread.reattach",
    "thread_id": "existing-thread-uuid",
    "expected_anchor_revision": 2,
    "anchor": { "version": 1, "type": "note" }
  }
}
```

The caller needs current read/comment access to the original thread and the new
target; note edit or moderation permission is not required. Removed discussions
cannot be moved. The expected revision must be a positive integer, including
when the target is unchanged. A stale revision returns `stale_anchor_revision`.
An identical target is a receipt-backed no-op and does not append history.
The result includes the accepted `anchor_revision` and `changed` flag.

The managed entry and target family cannot change. Bases retain definition ID,
saved view ID and generation. Record targets retain the same record read
dependency; member-backed groups and timeline lanes/ranges retain the same
server-owned witness dependency set. View/property/definition targets can move
within their common view access boundary. A cell-to-whole-view move that would
broaden readership fails with `reattach_readers_changed`. The server compares
file dependencies, not a snapshot of today's users, so later rule changes cannot
make the move silently broaden access. It does not rewrite comment mentions or
transfer a discussion to a different note/view.

Text and embedded-image commands authorize and check the current anchor revision
before contacting the private relay. SQL locks are released for that network
hop. The commit transaction reacquires authority and identity locks, checks the
revision again, and validates the proof against the exact immutable command,
document generation and dependency state. The new placement must be attached;
collapsed or unresolved replacement positions are rejected. Target revision,
thread activity, event and receipt commit together or all roll back.

## Recovery and bounded reads

The client persists an exact online request before sending and never puts it in
the reconnect outbox. Explicit Retry uses those bytes and operation ID. The
existing `/commands/withdraw` endpoint returns an accepted receipt or installs a
permanent cancellation receipt that fences a delayed send. Accepted retries are
checked before the new feature gate or private relay request, allowing receipt
recovery after the flag is disabled. Current authorization is always required.

`GET /v1/collaboration/shares/{share_id}/entries/{entry_id}/threads/{thread_id}/anchor-history`
accepts `after` (revision, default 0) and `limit` (default 10, maximum 20).
The response contains `entry_id`, a Bases `view_id` when applicable, current
`thread`, ordered `revisions`, `has_more` and `next_after`. Revision rows include
`revision`, `actor_id`, `created_at`, `anchor` and `unavailable`. Current thread
access is checked first; each historical Bases target is checked independently.
Unavailable historical targets return a null anchor rather than their metadata.
Body pagination also enforces the existing 384 KiB budget. The client stores
only the displayed history page in memory and clears it on offline/read loss,
document hiding or modal closure.

The implementation uses the existing 64 KiB command limit, expanded to 256 KiB
for bounded source proof or group/lane witness evidence. Timeline ranges with
lanes receive the same limit as their group witnesses. No historical quote or
anchor is copied into the event stream.

## Evidence and remaining work

- 193 related discussion/policy/Bases/group regression cases passed before the
  final history guard. The final focused reattachment/policy run passes 28 cases,
  including independent authorization of imported historical dependencies.
- The actual Python/Rust/TypeScript fixture proves durable text moves, a lost
  successful response recovered by withdrawal, retained history, stale revision
  rejection, cancelled delayed sends and compatible change events.
- The disposable PostgreSQL proof covers concurrent moves (one acceptance, one
  stale rejection), receipt retry after disabling the gate, rollback after flush,
  lock release during relay I/O, and permission/cancellation changes before
  commit. It uses an empty loopback test database; it is not migration proof.
- Native Obsidian 1.13.7 on macOS moved a resolved discussion from beta to gamma,
  retained both messages, rejected a stale whole-note move after Bob's competing
  revision, cancelled that stale request, and accepted a newly reviewed move.
  Native history and independent server reads confirm four target revisions,
  correct actors, two messages and unchanged state revision 2.

Reattachment does not yet recover a Bases thread whose original record/view
identity has become inaccessible through deletion or generation replacement.
Those cases need the broader recovery workflow; authorization is not bypassed
to make an old thread discoverable. Native image, block/Reading, Bases/timeline,
multiwindow, mobile/touch, accessibility and resource acceptance remain. See
the [client preview](../../evc-team-relay-obsidian/docs/discussion-reattachment-preview.md)
and [project status](../../../docs/Plans/obsidian-presence-comments-status.md).
