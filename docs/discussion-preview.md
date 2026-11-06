# Whole-note discussion protocol preview

Status: implementation slice, 2026-11-05. This is not the complete comments
feature. Both `COLLABORATION_POLICY_ENABLED` and
`COLLABORATION_DISCUSSIONS_ENABLED` default to false. The capabilities endpoint
continues to report `comments: false` and `bases: false`; it advertises
`discussion_commands`, the `note` target, body version, reaction keys, limits,
and the session requirement separately. No deployment is implied by this file.

Apply Alembic through `202611050002` in an isolated preview database. The additive
migration creates discussion scopes, threads, anchor revisions, messages,
message revisions, reactions, command receipts, and events. It adds a per-share
commit counter and epoch. It does not write comments into Markdown or Yjs.
Downgrading deletes discussion data and is not a populated-database rollback.

## Authentication and authorization

Every route requires a native/password **session-backed access token**. Older
sessionless tokens require sign-in again. Agent keys and relay document grants
are not discussion credentials. Every read and command retry checks active
user, session, membership, managed entry identity, and current path read policy.
Writes also require the independent comment policy. Read-only note reviewers
can therefore comment without acquiring a note-write grant. Missing and hidden
targets return the same `target_unavailable` response.

Only active managed Markdown entries are currently accepted. Share owners and
server administrators moderate; any authorized commenter can resolve/reopen.
Only authors edit their messages. A moderator can delete another author's
message with a reason, or remove a thread with a reason. Ordinary readers see
message tombstones, not deleted history. Authors/moderators can inspect deleted
message history; removed thread bodies are inspectable only by moderators.
Authorized readers can fetch removal state metadata without the body or anchor. Account
deletion nulls author references and retains history; share deletion cascades
discussion content and receipts. There is no private-thread feature.

Commands acquire row locks in this order: user SHARE, session SHARE, share
UPDATE, membership SHARE, then thread/message. Reads use a share SHARE lock.
Existing managed inventory/policy writers lock that share. User/session/member
revocation conflicts with the corresponding guard. Share/user deletion locks
the parent before ORM cascades. Token validity is checked again after waits.
All operations are short database transactions, with no network I/O under locks.
Service entry points own their transaction and reject sessions with unrelated
pending writes instead of discarding them. Production uses `autoflush=False`;
explicit flushes establish foreign keys and policy revisions before reads.

## Commands

`POST /v1/collaboration/commands`, with a JSON envelope:

```json
{
  "protocol_version": 1,
  "operation_id": "11000000-0000-4000-8000-000000000001",
  "share_id": "22000000-0000-4000-8000-000000000001",
  "entry_id": "33000000-0000-4000-8000-000000000001",
  "command": {
    "type": "thread.create",
    "thread_id": "44000000-0000-4000-8000-000000000001",
    "message_id": "55000000-0000-4000-8000-000000000001",
    "anchor": {"version": 1, "type": "note"},
    "body": {
      "version": 1,
      "blocks": [{"type": "paragraph", "children": [{"type": "text", "text": "Review this note."}]}]
    }
  }
}
```

`entry_id` is the stable managed-file UUID, not a path or server discussion-scope
UUID. Clients create immutable operation/thread/message UUIDs before sending.
The server supplies authors, timestamps, revisions, scope ID, and sequence.
Receipts are keyed by actor and operation ID and hash the complete canonical
request. An identical retry returns the original result **after current
authorization**. Changed reuse returns `operation_id_conflict`. Mutations,
receipt, sequence, and event commit atomically; failed validation rolls back
all of them. Keep the exact payload and operation ID after a lost response.

| Type | Additional fields | Offline queue |
| --- | --- | --- |
| `thread.create` | `thread_id`, `message_id`, `anchor`, `body` | Yes |
| `reply.create` | `thread_id`, `message_id`, `body`; optional `reopen` | Ordinary replies only |
| `message.edit` | `thread_id`, `message_id`, `expected_revision`, `body` | Own edits |
| `reaction.set` | `thread_id`, `message_id`, `emoji`, `present` boolean | Yes; desired state, not toggle |
| `thread.state` | `thread_id`, `expected_state_revision`, `state` (`open`/`resolved`) | No |
| `message.delete` | `thread_id`, `message_id`, `expected_revision`, optional `reason` | No |
| `thread.remove` | `thread_id`, `expected_state_revision`, nonblank `reason` | No |

Ordinary replies never reopen. **Send and reopen** uses `reply.create` with
`reopen: true` and `expected_state_revision`; message insertion and reopening
are one transaction. An ordinary reply must omit that precondition. Stale
message/thread revision returns 409 and leaves the submitted draft untouched
on the client. Editing creates an immutable revision; deletion appends a null
body revision. Reactions use `thumbs_up`, `thumbs_down`, `heart`, `laugh`,
`celebrate`, `eyes`, `question`, or `check`. Each user has at most one of each
reaction per message. A no-op desired state still receives a receipt and event
with `changed: false`.

Accepted results contain `operation_id`, `scope_id`, `thread_id`, `sequence`,
`state`, `state_revision`, `removed`, and `changed`. Message commands additionally
contain `message_id`, `message_revision`, and `message_deleted`.

## Bodies and bounds

The strict version-1 AST accepts paragraphs, code blocks, and flat lists.
Paragraphs contain text (optional unique bold/italic/code/strike marks), HTTP(S)
links containing text, or mentions with a stable `user_id`. Links disallow
credentials and control characters. Mentions must currently name an active
share member/owner who can read the target; at most 50 unique recipients. They
do not grant access or send notifications yet. Future delivery must recheck
recipient permissions. Rendering must create text/DOM nodes, never trust HTML.

Bodies are nonblank and at most 32 KiB UTF-8. The entire streamed POST body is
limited to 64 KiB before parsing, including chunked requests. Unknown fields,
HTML, unsupported anchors, and images reject; they are not silently stripped.
Page size is at most 100. Message/history pages also limit aggregate serialized
body bytes to 384 KiB and leave room for metadata. Reaction responses aggregate
counts and `mine` rather than returning unbounded user arrays. Validators and
storage errors do not echo message bodies in API error responses.

## Reading and replay

All paths below start with
`/v1/collaboration/shares/{share_id}/entries/{entry_id}`:

- `GET /threads?after=0&limit=50`: nonremoved threads, ordered by creation.
- `GET /threads/{thread_id}?after=0&limit=50`: current thread/anchor and message
  page, including current bodies, revisions, and reaction aggregates.
- `GET /threads/{thread_id}/state`: current state/anchor without message bodies;
  removed threads return state metadata only, with no anchor or removal reason.
  Entry read authorization still applies, including after revocation.
- `GET /threads/{thread_id}/messages/{message_id}`: one current message/reaction
  aggregate, plus its thread and anchor. Enforces the same thread visibility and
  entry/session authorization as a full thread read. This lets clients update
  old replies without downloading the entire conversation.
- `GET /threads/{thread_id}/messages/{message_id}/history?after=0&limit=50`:
  immutable revision page, subject to deletion/moderation rules.
- `GET /changes?cursor=...&limit=100`: ordered event metadata for this entry.

The first three return `has_more` and `next_after` (creation sequence or history
revision). They are **live keyset browsing**, not point-in-time snapshots.
Clients must not delete cached items merely because they are absent from a page.
Events contain IDs/revisions/state and actor/operation identity, not bodies or
snippets. Treat them as invalidations, fetch currently authorized entities, and
commit cache updates plus checkpoint together. Replayed events do not reconstruct
historical bodies or establish current authorization.

Replay returns `events`, `cursor`, `has_more`. The encrypted authenticated cursor
is bound to actor, share, entry, epoch, and sequence; it expires after 30 days.
Changing the JWT secret also invalidates it. Invalid/expired/foreign/old-epoch
cursors return `snapshot_required` (409). This preview retains **all events**:
recovery clears the local checkpoint and replays from no cursor, then refetches
current entities. It has no frozen snapshot endpoint or event pruning yet.
The initial empty-share cursor has no epoch; the first accepted command creates
an epoch and may require this reset. Any future restore must rotate epochs;
the restore workflow is not implemented in this slice.

## Error handling

The control plane's existing envelope is used, not FastAPI's `detail` shape:

```json
{"error":{"code":409,"message":{"code":"stale_message_revision"},"request_id":"..."}}
```

401 pauses until valid same-account credentials are available. 403/404 reject
the queued operation, preserve its body for recovery, and invalidate affected
cached content. 409 command conflicts require review, not automatic payload
rewriting. 413/422 reject locally recoverable drafts. Retry transient network,
429, and 5xx failures with the original operation ID and bounded backoff; 503
`retryable_transaction` includes database contention/retry failures. A successful
HTTP response must still match the operation/thread/message IDs before local
acknowledgment. Never log comment bodies, credentials, or replay cursors.

## Validation and remaining work

Focused discussion/policy tests cover permissions, retries, immutable history,
revocation, strict input, bounds, tombstones, pagination, and autoflush-disabled
sessions. `tests/integration/discussions_postgres.py` additionally runs the full
Alembic chain, schema parity, empty-table downgrade/upgrade, concurrent duplicate
sends/edits, SQL row-lock conflicts, post-revocation replay, and account/share
deletion against disposable PostgreSQL 16. The script refuses nonloopback or
non-`collaboration_test` database targets. Do not run it against service data.

`python tests/integration/discussions_client.py --plugin ../../../evc-team-relay-obsidian`
runs the plugin's actual outbox/transport against a disposable loopback FastAPI
server and temporary SQLite database. It covers an accepted response lost before
local acknowledgment, identical retry, dependent replies, edits/history,
reactions, policy revocation and retained local drafts. It requires the plugin's
locked npm dependencies and Node on PATH, and stops/removes its fixture afterward.
It does not replace the PostgreSQL concurrency proof or native mobile checks.

Remaining before a complete feature: plugin/network/UI integration, durable
live text-anchor barrier, reading/image mapping, Bases identity/adapters,
Time & Line bridge, image uploads, inbox, notifications, authenticated presence,
event WebSockets/snapshots/retention, coherent backups, and native mobile and
performance acceptance. These routes alone do not advertise those capabilities.
