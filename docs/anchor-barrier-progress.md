# Text-anchor barrier: private relay implementation

Status: November 5, 2026. The private relay registration/validation endpoints and
their storage/lifecycle checks are implemented and **disabled by default**.
Control-plane generation ownership and receipt consumption now have a gated
public text-command integration and a real plugin/control-plane/relay proof.
Native selection/highlights, coherent restore, and performance acceptance remain
unfinished. Whole-note commands remain the default preview. No live service was
restarted or configured.

## Live placement

`DocWithSyncKv` now constructs its in-memory Yrs document with UTF-16 offsets,
matching Yjs and CodeMirror, instead of Yrs's default byte offsets. Its
`resolve_text_anchors` method resolves a bounded batch against that document
under one read transaction and captures dependency state once. It does not fetch,
export or reparse a full update for each comment.

The batch accepts at most 32 anchors and 256 KiB of decoded dependency/position
bytes total; each dependency is at most 64 KiB and each position 256 bytes.
It checks state-vector **and delete-set** inclusion. Results distinguish attached,
waiting for sync, unanchored and invalid. Relative positions in a different shared
type reject. The known empty `contents` root can be materialized without inserting
content so an empty-note caret has a valid position. Other malformed input fails
the batch. No authorization or document-generation claim is made by placement.

Nine deterministic Yjs vectors exercise original/prefix/deleted/Unicode/end
positions, unapplied/applied deletion-only dependencies, wrong shared type and
empty-note caret. Rust runs them through the full-update helper and through live
documents before and after actual KV checkpoint/reload. The fixture copies and
generator are shared with the plugin. Global Yjs/Yrs GC was not disabled.

## Checkpoint ordering

`SyncKv` serializes all persistence attempts through an async mutex. A caller
whose dirty flag is clear still waits for an in-flight write, so it cannot mistake
an already reserved snapshot for completed storage. A newer checkpoint cannot
overtake an older one. The reservation has a drop guard: storage/encoding/lease
errors and cancellation mark it dirty and signal retry without needing another
user edit. Updates arriving during storage remain pending or enter the next
snapshot. Existing offline compare-and-set write leases remain supported.

The filesystem store now writes into a same-directory temporary file, synchronizes
the file, atomically replaces the destination, and synchronizes directory links
on Unix. New ancestor-directory links are also synchronized. A failed partial
write retains the previous snapshot; temporary files are removed normally and
excluded from listing if left by a crash. Non-Unix targets retain atomic file
replacement but cannot advertise the Unix directory-durability guarantee.

This is not a guarantee against hardware/controller faults. Tests inject a partial
write failure and exercise replacement/reload; they do not simulate power loss on
the Pi's storage. Filesystem I/O remains synchronous, as in the existing backend;
measure checkpoint latency before enabling the barrier for normal traffic.

## Private endpoint and lifecycle increment

The [private protocol](anchor-barrier-protocol.md) implements authenticated
generation registration and validation. It requires server credentials even when
the ordinary relay is configured for anonymous development. Ordinary document
tokens fail. Enabling it without authentication or a supported durable store fails
startup. Only Unix filesystem storage currently opts into durability; remote
backends remain gated pending their own evidence.

Requests bind managed document/share IDs, generation, request ID, expiry, and a
SHA-256 digest of the exact HTTP body. Registration binds only an existing document
and rejects any conflicting explicit routing channel. Existing managed notes
without a channel bind through the authenticated control-plane authority, without
changing their routing. Registration never rotates an existing generation.
Validation checks the persisted binding, resolves the batch and captures the
observed snapshot in one transaction, then awaits ordered storage. Missing
dependencies and malformed positions return distinct failures; valid dependencies
whose selected text disappeared can return an unanchored placement.

Admission is bounded to four operations, with no waiting queue and a nonblocking
per-document lock. The body limit is 512 KiB and collection deadline five seconds;
request lifetime is at most 30 seconds. Existing decoded batch/anchor limits apply
independently. Snapshot preflight also rejects count-based allocation bombs,
truncation, overflow and trailing data before Yrs allocates decoded collections.

After admission, work belongs to the server task tracker. HTTP cancellation or
deadline expiration stops waiting but does not drop an in-flight storage future.
Permits and document locks remain held until completion; expired work cannot
return a receipt. Both the library server and executable drain tracked storage
work during graceful shutdown.

Eviction now takes the same per-document load lock, atomically rechecks references,
removes the idle document from admission, and finishes its final checkpoint before
allowing reload. Failed writes restore the live document to the cache. Public load
helpers use that lock too. This closes the old-copy/new-copy persistence race.

## Control-plane and client integration

Migration `202611050003` adds a server-owned managed-entry generation. New entries
receive one; legacy entries receive one lazily under a row lock through the
authorized `anchor-context` endpoint. Inventory records cannot supply it. The
private HTTP client registers and validates outside SQL locks, bounds admission
and responses, disables environment proxies/redirects, and verifies every binding,
the exact request digest, checkpoint time and expiry. Public commands cannot
supply proofs. Final authorization reacquires user/session/share/member/entry
guards and checks generation and expiry again before commit. Accepted duplicate
requests still reauthorize but return the original receipt without another RPC.

`COLLABORATION_TEXT_ANCHORS_ENABLED` defaults false and also requires the existing
policy/discussion flags plus a separate `COLLABORATION_ANCHOR_RELAY_TOKEN` secret.
The relay verifier must allow that credential's key to issue server tokens; the
normal document key need not acquire that authority. Transport uses the origin
from `MANAGED_RELAY_URL`, converting WS schemes to HTTP schemes. Full `comments`
and `bases` capabilities remain false. Text commands have a 256 KiB public bound,
whole-note commands retain 64 KiB, and dependency evidence retains its 64 KiB
decoded bound. Thread-read body budgets include the stored anchor bytes.

The plugin negotiates text capability, caches authorized document generations for
offline capture, validates wire targets and treats missing note dependencies as
retryable queued work. Refresh never rewrites an existing queued anchor's
generation. Revocation clears remote generation context while retaining authored
local work. Context clipping preserves valid Unicode without shifting UTF-16
selection offsets. Native selection capture and highlight wiring are still next.

Validation: 60 focused control-plane tests; nine Rust private-barrier tests;
1,540 plugin regression tests (three opt-in suites skipped); type/build/lint gates.
The separate real-connection fixture passes read-only selected-text comments,
document-token rejection on private routes, deletion-only dependency waits,
lost-response/identical retry, filesystem restart, restored placement, and a new
validation after restart. Disposable PostgreSQL passes migration/schema parity,
empty-table downgrade/upgrade, lock release during the private hop, and rejection
after concurrent generation change or comment revocation. No production data or
service was used; the PostgreSQL fixture was removed after testing.

## Remaining release gates

1. Add native selection/source/Reading mapping, block targets, decorations,
   navigation, persistent target-aware drafts, and explicit reattachment/recovery.
2. Implement coherent backup/restore and deliberate generation rotation across
   SQL and relay storage. The current endpoint refuses rotation; a process restart
   preserving both stores is tested, but is not a cross-store restore protocol.
3. Benchmark long-lived documents, concurrent edits/compaction, batch cost, and
   typing responsiveness. Prove remote-store semantics before opting those
   backends in. Preserve all drafts if a normal workload exceeds evidence limits;
   revisit limits/staging using measurements before enabling normal traffic.

Validation on this Mac: complete `y-sweet-core --lib` **189 passed** and
`relay --lib` **43 passed**, followed by **8 private barrier tests passed** after
adding explicit decoded-boundary and expired-wait cases. The private cases include
all nine shared anchor vectors through registration, validation and filesystem
eviction/reload; auth/unknown targets/generation mismatch; failed writes; client
cancellation and shutdown draining; and reload waiting for final storage.
Compilation and changed-file formatting pass. The plugin fixture suite's earlier
**8 passed** remains prior evidence; no plugin code changed in this increment.
