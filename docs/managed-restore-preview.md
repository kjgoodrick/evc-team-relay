# Managed inventory restore guard

This is one part of the unfinished coordinated collaboration restore workflow.
Do not activate restore generations in a live deployment based on this guard
alone. File-content grants/uploads, document sessions, other writers, minimum
client versions, complete local-work reconciliation and the coordinated
database/relay/private-object restore drill remain separate work.

## Protocol

Managed inventory GET and successful batch responses add `restore_guard: true`
and `restore_generation: UUID | null`. The generation belongs to the share,
independently of entry collaboration generations or visibility epochs. Claims
return the same context with `won`, `record` and `permission`.

`POST /v1/managed/index/{share_id}` and `.../claim` accept the optional
`X-Relay-Restore-Generation` UUID header. Missing means the original null lineage.
Clients retain the binding from the state against which work was prepared; they
must not replace it with a new generation after a conflict or reconnection.
Bodies and inventory compare-and-swap semantics remain unchanged.

The server refreshes the full share row after acquiring its update lock and
checks the generation before any batch work, activity changes, exact no-op or
existing-file claim adoption. Mismatch returns `409 restore_generation_changed`.
All mutations remain subject to current membership/path permission and existing
structural/content guards. Headerless old clients work on original shares and
cannot mutate inventory on restored shares.

Inventory reads hold a share read lock while collecting the filtered records,
permissions, tombstones and Bases versions. Cached ORM identities are expired
before entering this boundary. The read commits before returning, including a
manifest returned after a successful batch; it leaves no idle authority lock.
These entry points require a session without pending caller-owned writes.

## Client preservation

The plugin's private folder Y.Doc retains restore context in its existing cached
manifest. Every batch, activity update, conflict retry, reviewed local-layout
choice and file claim uses the binding captured before asynchronous work. Claim
bodies are copied before waiting behind another inventory exchange. Bases content
acknowledgment also checks the original pending request's binding before advancing
inventory metadata.

A client with a known non-null binding reads the current manifest before a new
batch or claim, preventing writes to a server that has dropped restore support.
This adds one inventory read per such write; ordinary original-lineage writes
retain their current request count. Restore detection precedes automatic conflict
rebasing. A fresh empty client can join a restored share. A client with prior or
unproven local work cannot automatically adopt the restored inventory.

On a changed binding the client retains its prior snapshot, current local
records, pending activity and observed restored snapshot in a durable
`managed_base_v1.restoreReview` record. It preserves the existing file metadata,
conflicts and activity maps; later local edits remain in those maps. Inventory
writes, claims, ordinary conflict choices and metadata acknowledgments pause.
Known inventory authority is withheld, background retries stop, and restarting
reports the retained pause without replaying work. The conflict dialog explains
the pause and hides ordinary layout choices. There is deliberately no automatic
resume or discard operation; a complete restore reconciliation flow is unfinished.
The record is metadata recovery evidence, not a verified backup of file/CRDT bytes.

This local preservation does not fence an older installed plugin's private cache
worker or drain unrelated document/content writers. Minimum compatible versions
and writer shutdown are required before reopening a restored deployment.

## Verification

- Unit tests cover old/no-op/delete/move/type/activity batches, existing/new
  claims, current-bound success and unchanged inventory after rejection.
- `tests/integration/managed_restore_postgres.py` preloads stale ORM rows, waits
  behind the share lock and rotates the generation. No-op, move, activity and
  both claim forms reject. A waiting reader observes the restored path/generation
  and releases its lock before return.
- `base_content_client.py --restore-guard` also runs the actual inventory client
  over disposable loopback HTTP: offline work is preserved through reopening;
  fresh bound claims, moves and activity succeed.
- Client tests cover in-flight/queued claims, original-body/header separation,
  restore and support-downgrade preflight, paused reopening, old Bases
  acknowledgments and the dialog's disabled ordinary recovery choices.

No live service or native vault is modified by these fixtures. Native acceptance,
safe resume, byte-level recovery exports and the complete restore drill remain.
