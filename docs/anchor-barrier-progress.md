# Text-anchor barrier: relay prerequisites

Status: November 5, 2026. Live placement and checkpoint prerequisites are
implemented; **no trusted anchor receipt endpoint is enabled or complete**.
Whole-note discussion commands remain the only accepted discussion target.

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

## Remaining contract before accepting selected-text comments

1. Add a disabled-by-default private relay endpoint with fail-closed server
   authentication, body/batch/concurrency limits, explicit document generation,
   request digest and bounded lifetime. Never accept ordinary browser/document
   credentials as internal barrier authority, or create an unknown managed doc
   merely because a barrier request names it.
2. Run checkpoint work under a **server-owned lifetime**, separate from the HTTP
   request. Canceling an S3/remote-store future does not prove its remote write was
   canceled; an old request may still finish. The new cancellation guard preserves
   retryability, but alone cannot make such an uncertain write a durable receipt.
   Keep writes ordered to completion and let disconnected clients stop waiting.
3. Establish supported-backend durability, reject memory-only/unsupported stores,
   then issue a receipt only after the requested state is applied and checkpointed.
   Return dependency-wait separately from malformed or subsequently removed targets.
4. Add managed document generations and restore rules, control-plane validation
   before the relay call and again at commit, and request/receipt identity binding.
   Do network work outside PostgreSQL authorization locks, then recheck under them.
5. Align public command and private barrier byte limits. Base64 of a maximum 64 KiB
   dependency alone exceeds the current 64 KiB whole-note command limit. Do not
   silently enable text anchors under that envelope; choose explicit bounded
   limits or dependency-token staging and add cross-language protocol vectors.
6. Exercise concurrent edits, compaction, unknown documents, missing deletes,
   generation changes, checkpoint failure/cancellation, process restart and
   unauthorized replay through real HTTP and storage. Benchmark batch cost and
   typing responsiveness. Add native selection/source/Reading mapping separately.

Validation on this Mac: `y-sweet-core --lib` **188 passed**; `relay --lib`
**37 passed**, including new checkpoint failure/ordering and filesystem tests.
Plugin text-anchor suite **8 passed**. Rustfmt was added to the temporary task
toolchain and passes for the changed Rust files. No source was deployed, no
running relay was restarted, and no new anchor capability is advertised.
