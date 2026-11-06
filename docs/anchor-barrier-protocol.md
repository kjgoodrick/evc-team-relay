# Private text-anchor barrier, protocol 1

This is a relay-to-control-plane protocol, not a public comment API. It is
disabled unless `[server].collaboration_anchor_barriers = true` in relay TOML.
Enabling it requires a configured authenticator and a storage backend that opts
into durable checkpoints. Currently that is Unix `FileSystemStore`, using its
file/directory-synchronized atomic replacement. Memory and remote stores fail
closed. No deployment configuration is changed by implementing this protocol.

The control-plane and plugin protocol integration is implemented behind an
additional text-anchor preview flag. Native selection/highlight UI and coherent
restore remain unfinished. Never accept a client-provided receipt.

## Transport and admission

- `POST /internal/collaboration/register` binds a first document generation.
- `POST /internal/collaboration/validate` observes placements and checkpoints them.
- Both require `Authorization: Bearer <server-token>`. Document, file and prefix
  credentials are not internal authority. Anonymous-development fallback is off.
- Use the configured private relay origin and server credential. A response is
  trusted only as the direct result of this authenticated internal call; it is not
  a transferable signed bearer proof.
- Authentication precedes body collection. Four operations may be admitted at
  once, including bodies still arriving. There is no unbounded waiting queue.
- JSON body: at most 512 KiB; collection deadline: five seconds. Per-document
  admission is nonblocking. Busy callers receive 429 and must back off.
- Responses containing observations use `Cache-Control: no-store`. Errors contain
  a fixed code, never body bytes, credential values, storage exception details,
  note text, or snapshot contents.

## Request

Both endpoints require these fields and reject unknown fields:

| Field | Contract |
| --- | --- |
| `protocol_version` | Integer `1`. |
| `request_id` | Lowercase UUID; identifies this private request. |
| `document_id` | Existing managed entry UUID; also the relay document ID. |
| `share_id` | Managed share UUID verified by the control plane and bound in relay metadata; any explicit routing channel must agree. |
| `generation` | Control-plane-owned UUID for this document's CRDT lineage. |
| `expires_at_ms` | Unix epoch milliseconds, greater than now and at most 30 seconds ahead. |
| `anchors` | Omitted/empty for registration; 1–32 inputs for validation. |

An anchor input has `dependency`, `start`, `end`, and `selected_length`.
The first three are canonical padded standard base64. `dependency` is the Yjs
v1 snapshot encoding (delete set followed by state vector), capped at 64 KiB
decoded. Start/end are encoded Yjs relative positions, each capped at 256 decoded
bytes. `selected_length` is the original UTF-16 length as an unsigned 32-bit
integer. Combined decoded evidence across the batch is at most 256 KiB.

These are independent limits. A small JSON body can still fail decoded-count,
position-length, snapshot structure, or delete-range-overflow checks. No evidence
is truncated or repaired by the relay.

## Generation registration

The relay verifies existing storage/cache before binding `collaboration_generation`
and `collaboration_share_id` in SyncKv metadata. Existing managed notes normally
have no stored routing channel: their inventory belongs to the control plane.
The authenticated private caller establishes the first ownership binding after
checking that inventory. An explicit conflicting channel rejects. Registration
does not add a channel, create a parent document, or change event delivery.
Registration never initializes an unknown document. A missing file is
404; a storage outage is 503. An existing identical binding is idempotent; a
different generation or partial/mismatched binding is 409.

The metadata is outside the Yjs document and does not insert note content or
grant a reviewer note-write access. The control plane must first persist a stable
generation under its own managed-entry transaction, then register it outside SQL
locks. Concurrent retries must use that same generation. Generation rotation and
coherent restore require a separate explicit workflow, still to be implemented.

## Validation response

Registration and validation return `protocol_version`, `request_id`,
`document_id`, `share_id`, `generation`, `request_sha256`, `expires_at_ms`, and
`checkpointed_at_ms`. `request_sha256` is lowercase hex SHA-256 of the **exact
received HTTP body bytes**, not a reserialized object. The caller must retain the
bytes it sent and compare every binding and the expiry before use.

Validation additionally returns:

- `observed_snapshot_sha256`: SHA-256 of the encoded snapshot captured in the
  same transaction as placement, without returning a full document or snapshot.
- `placements`: one result per input, in input order. An `attached` result has
  UTF-16 `from` and `to` offsets. `unanchored` means dependencies were applied but
  the original selection no longer resolves to a usable range.

The checkpoint completes after the observation. Later edits may naturally move
or remove the target; a receipt does not freeze the document. Clients resolve
stored relative positions against their own synchronized state, not by treating
the observed offsets as permanent positions.

## Errors and lifetime

| Status/code | Meaning |
| --- | --- |
| 401 `server_auth_required` | Missing, invalid, wrong-audience or wrong-kind authority. |
| 404 `document_unavailable` | No existing document; never create it through a barrier. |
| 409 `share_mismatch` / `generation_mismatch` | Identity/lineage does not match. |
| 409 `dependency_not_ready` | Required insertions or deletions have not reached the relay. |
| 422 `invalid_anchor` | Invalid binary evidence, wrong shared type, or position size. |
| 422 `invalid_request` / `invalid_batch` / `batch_too_large` | Invalid envelope or limits. |
| 413 `request_too_large` | Body limit exceeded or body could not be collected. |
| 429 `barrier_busy` / `document_busy` | Bounded admission is occupied. |
| 408 `request_expired` | Body deadline or private request lifetime elapsed. |
| 503 `checkpoint_failed` / `barrier_failed` / `shutting_down` | No valid receipt; retry after recovery. |

After body validation, a server-owned tracked task holds its admission permit and
document lock until it finishes. HTTP disconnect and timeout do not cancel the
underlying storage future. Expired operations may finish saving, but cannot issue
a valid receipt. Graceful process shutdown stops admission and drains these tasks.
This deliberately bounds stuck-store resource use rather than allowing overlapping
writes after an uncertain cancellation.

The control plane must map dependency waits and unsynchronized new-note targets
to retained queued work, recheck access and generation at SQL commit, and refuse
expired/mismatched responses. Duplicate public command receipts must still be
reauthorized. No PostgreSQL lock may span the private network call.
