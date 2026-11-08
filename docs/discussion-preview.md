# Note and selected-text discussion protocol preview

Status: implementation slice, 2026-11-05. This is not the complete comments
feature. Both `COLLABORATION_POLICY_ENABLED` and
`COLLABORATION_DISCUSSIONS_ENABLED` default to false. The capabilities endpoint
continues to report `comments: false` and `bases: false`; it advertises
`discussion_commands`, the `note` target, body version, reaction keys, limits,
and the session requirement separately. A separate text-anchor flag advertises
`discussion_text_anchors` and the `text` target only when configured with private
relay authority. No deployment is implied by this file.

The November 6 increment also advertises `discussion_block_anchors` with the
text-anchor capability. Blocks remain text targets with bounded structural
metadata; this is not a separate document identity or a complete Reading-view
adapter.

An independent image-occurrence capability now supports whole-image, pin and
region anchors on embeds in managed Markdown notes. It remains disabled by
default; see [image payloads and upgrade requirements](image-discussion-preview.md).
Direct image-file scopes and images inside message bodies are not yet supported.

Apply Alembic through `202611050003` in an isolated preview database. The additive
migration creates discussion scopes, threads, anchor revisions, messages,
message revisions, reactions, command receipts, and events. It adds a per-share
commit counter/epoch and server-owned managed-entry generation. It does not write
comments into Markdown or Yjs.
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
message tombstones, not deleted history. This also applies to authors and
moderators: ordinary history and picture endpoints cannot retrieve removed
content, and removed threads reject ordinary thread/message/anchor-history reads.
Separately authorized, audited reads are available in the default-off
[moderation recovery preview](discussion-moderation-recovery-preview.md).
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

### Read-only receipt inspection

`POST /v1/collaboration/commands/status` accepts the same exact envelope and
returns one of:

- `{"status":"accepted","result": ...}` with the original receipt metadata;
- `{"status":"withdrawn","operation_id": ..., "entry_id": ...}`;
- `{"status":"unknown","operation_id": ..., "entry_id": ...}`.

The separate `discussion_command_status` capability is enabled with the command
preview. This is a bounded, `Cache-Control: no-store` read, using POST to keep the
private request out of URL logs. It does not create a receipt, scope, event,
withdrawal or mutation. It never uploads pictures or calls the relay to validate
an old anchor. Exact actor/operation/request/scope matching and current session,
membership and note read access still apply. Accepted Bases receipts also require
read access to their current saved-view target and record dependencies. Comment
permission is not required for inspection; sending or withdrawing still requires
it. Other actors receive no information about the original actor's receipt.

`unknown` means that no receipt is present in the current database. It does **not**
mean never accepted: restore or receipt loss can remove evidence. It must never
trigger an automatic resend or replacement. Authorization failures, unsupported
servers and network failures are errors, not `unknown`. Responses contain no
comment body, history or picture bytes. This supplies a recovery building block;
archive-request reconciliation UI and restore-epoch command fencing remain
separate unfinished work.

### Restore-generation command guard (server foundation)

Migration `202611070005` adds nullable
`shares.collaboration_restore_generation`, separate from the cursor epoch.
`NULL` denotes the original lineage and accepts existing commands without a
header. A restored share with a generation UUID requires that exact value in
`X-Relay-Restore-Generation` for both commands and withdrawals. Mismatch or a
missing header returns `409 restore_generation_changed` before receipt lookup,
state changes or private anchor validation. The check is repeated under the
share lock after private anchor validation, so rotation during the network hop
cannot commit an old command. Existing accepted receipts remain inspectable
through the read-only status route. Generation is transport context, excluded
from the immutable command hash.

Capabilities expose `restore_generation` and `discussion_command_restore_guard`.
Ordinary permission/cursor resets do not change this generation. There is no
automatic rotation or restore activation command yet. The client transport/store
now preserve explicit bindings and fence local old-generation work, with actual
HTTP evidence from `discussions_client.py --restore-guard`. Integration with the
ordinary session's intent creation and lifecycle, recovery review, and guards for picture,
group-capture, marker, permission and inbox mutations are still unfinished.
This capability describes **discussion commands and withdrawals only**; it is
not a complete service restore boundary. Do not manually activate it for a live
share or claim the collaboration restore procedure is complete.

The client must retain an operation's original generation across retries,
offline storage, export/import and dependent commands. It must never replace that
generation with a freshly fetched value merely to retry an old request. A newer
generation requires explicit reconciliation against the restored receipts and
entities. Ordinary downgrade refuses to drop this column while any share has an
active generation. Running older application code against a restored database
would bypass application-level guards; the future deployment/restore coordinator
must enforce its recorded minimum server/client compatibility, not rely on the
column alone.

Disposable PostgreSQL evidence covers current-head migration/schema parity,
empty downgrade/upgrade, active-generation downgrade refusal, current receipt
reads and old-write rejection, plus rotation during an in-flight private anchor
validation. The fixture is removed afterward. No production generation is set.

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
HTML, unsupported anchors, and message-body images reject; they are not silently stripped.
When text anchors are enabled, only text/image-anchor create envelopes may exceed 64 KiB, up
to 256 KiB; their decoded dependency remains limited to 64 KiB. Read-body budgets
also reserve space for anchor bytes.
Page size is at most 100. Message/history pages also limit aggregate serialized
body bytes to 384 KiB and leave room for metadata. Reaction responses aggregate
counts and `mine` rather than returning unbounded user arrays. Validators and
storage errors do not echo message bodies in API error responses.

## Selected-text preview

`GET /shares/{share_id}/entries/{entry_id}/anchor-context` under the collaboration
prefix returns protocol version, entry ID and generation to authorized note
readers. Persist that generation with captured relative positions; never replace
it silently in queued work after refresh or restore. A text anchor contains
`version: 1`, `type: text`, `document_id`, `generation`, canonical base64 `start`,
`end`, and `dependency`, original UTF-16 `selected_length`, and bounded `context`
(`quote`, `prefix`, `suffix`, `truncated`). Client-supplied proof fields reject.

Block capture may include `structure: {kind: "blocks", blocks: [...]}` with
1–32 descriptors. Each descriptor has a supported `kind` (paragraph, heading,
list item, quote, callout, code, table, HTML, rule, or frontmatter) and optionally
an existing `explicit_id`. The canonical range and dependency validation are
unchanged. The server validates the metadata's bounds and shape; it does not
claim to verify Markdown semantics. Descriptors are not sent to the private
relay. An absent structure is omitted from canonical serialization, preserving
existing text-command receipt hashes. Comments do not insert block IDs into
the note. Native block and Reading-view acceptance remain pending.

Creation obtains a durable observation through the
[private relay protocol](anchor-barrier-protocol.md), outside database locks,
then rechecks current authorization and generation before commit. Missing note
dependencies return 409 `anchor_dependency_not_ready` with `Retry-After`; retain
the identical command for retry after note sync. Generation mismatches and invalid
positions need review. Valid deleted selections may become unanchored discussions.
Ordinary replies do not repeat anchor validation. See
[integration evidence and remaining gates](anchor-barrier-progress.md).

## Reading and replay

All paths below start with
`/v1/collaboration/shares/{share_id}/entries/{entry_id}`:

- `GET /threads?after=0&limit=50`: nonremoved threads, ordered by creation.
- `GET /participants?after={user_id}&limit=50`: active members who can read this
  exact note, with names and stable IDs for mentions. Keyset pages expose only
  eligible participants and their IDs; no email addresses are returned. Every
  page rechecks current actor/session/note access.
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

Private pictures in comment bodies now have a separately gated server protocol,
bucket and immutable revision references. See the
[picture preview protocol](comment-picture-preview.md) for authorization,
limits, tests and the unfinished client/offline/cleanup work. The capability is
disabled by default and is not a completed native feature.

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

Remaining before a complete feature: native selection/highlight UI, block and
reading/image mapping, Bases identity/adapters,
Time & Line bridge, image uploads, inbox, notifications, authenticated presence,
event WebSockets/snapshots/retention, coherent backups, and native mobile and
performance acceptance. These routes alone do not advertise those capabilities.

## Ordinary reads after removal (November 7)

The former author/moderator exceptions in ordinary reads have been removed to
match the agreed deletion policy. All roles now receive `target_unavailable`
from deleted-message history and removed-thread body/message/anchor-history
reads. The explicit thread-state endpoint still returns safe removal metadata.
Deleted-message body serialization also forces `null` independently of the
stored current-revision body. Normal picture reads require a surviving live
message in a live, readable thread; a retained historical reference in a deleted
message alone no longer authorizes its metadata, content or thumbnail.

Authorization is rechecked after picture object I/O, including deletion while a
download is in flight. Retained database history has not been purged by this
change. The separate [audited recovery API](discussion-moderation-recovery-preview.md)
is available behind its own disabled flag; native administrator controls and
timed cleanup remain unfinished. It cannot be reached through an ordinary
History button. Existing local/exported copies are not retroactively erased.

The discussion, picture, export and search regression set has 156 passing tests
across its run and focused correction rerun. Removed-thread/body/history checks
cover readers, authors and moderators; picture checks include all three roles,
metadata/content/thumbnails and deletion during storage reads. Actual client HTTP
and MinIO checks also pass. The client hides tombstone History and invalidates
open or delayed history for authors/moderators. No production service changed.
