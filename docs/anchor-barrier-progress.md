# Text-anchor barrier: private relay implementation

Status: November 5, 2026. The private relay registration/validation endpoints and
their storage/lifecycle checks are implemented and **disabled by default**.
Control-plane generation ownership, receipt consumption, and native selection
integration remain unfinished. Whole-note discussion commands remain the only
accepted public discussion target. No live service was restarted or configured.

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
with matching stored routing channel, and never rotates an existing generation.
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

## Remaining contract before accepting selected-text comments

1. Add control-plane managed document generations and restore rules, validation
   before the relay call and again at commit, and request/receipt identity binding.
   Do network work outside PostgreSQL authorization locks, then recheck under them.
   Persist the generation before registering it; consume responses only from the
   private authenticated call, never from client-supplied proof fields.
2. Align public command and private barrier byte limits. Base64 of a maximum 64 KiB
   dependency alone exceeds the current 64 KiB whole-note command limit. Do not
   silently enable text anchors under that envelope; choose explicit bounded
   limits or dependency-token staging and add cross-language protocol vectors.
3. Exercise the complete control-plane/relay/plugin boundary over real HTTP,
   coherent restore/generation changes and process restart. Current route tests
   use the Axum service with actual filesystem storage and deterministic pause/
   failure injection; they do not substitute for that end-to-end acceptance.
4. Benchmark long-lived documents, concurrent edits/compaction, batch cost, and
   typing responsiveness. Prove remote-store semantics before opting those
   backends in. Add native selection/source/Reading mapping and queued note-sync
   dependencies. Whole-note public capabilities stay unchanged until these gates.

Validation on this Mac: complete `y-sweet-core --lib` **189 passed** and
`relay --lib` **43 passed**, followed by **8 private barrier tests passed** after
adding explicit decoded-boundary and expired-wait cases. The private cases include
all nine shared anchor vectors through registration, validation and filesystem
eviction/reload; auth/unknown targets/generation mismatch; failed writes; client
cancellation and shutdown draining; and reload waiting for final storage.
Compilation and changed-file formatting pass. The plugin fixture suite's earlier
**8 passed** remains prior evidence; no plugin code changed in this increment.
