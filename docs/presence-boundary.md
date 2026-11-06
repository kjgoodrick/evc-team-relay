# Managed awareness identity and ownership

The managed WebSocket gateway now authenticates and bounds Yjs awareness in
addition to checking document permissions. This is a foundation for the planned
presence feature. The plugin now has a foreground-note controller and privacy/
status/retry controls, a current-note roster and current-note Jump/Follow.
Authorized cross-note following, image pointers and Bases adapters remain.

`apps/control-plane/app/services/presence_protocol.py` validates one complete
protocol frame, permits one awareness client ID per socket, validates relative
positions against the `contents` text type, and replaces all client identity
fields. Names and picture URLs come from the current account's most recently
updated linked OAuth profile; accounts without a profile use the email local
part as a display name. Unsupported avatar URL schemes and credential-bearing
URLs are removed. No full email, login session UUID, token, path or arbitrary
client state is forwarded as presence metadata. The optional `surface` field
accepts only `editor` or `reading`; Reading rejects a non-null editor cursor.
Payloads without a surface retain their previous trusted shape.

Optional `viewport` carries a validated relative position in `contents`, never
pixel offsets or a path. Optional `selection` is a validated anchor/head pair
accepted only for the Reading surface; its editor `cursor` remains null.
Missing fields retain compatibility with the previous trusted state shape.
The 4,096-byte payload/state bound applies to the complete state including these
fields and authenticated profile data. This does not add a location directory or
authorize cross-document subscriptions.

`sessionId` is a stable HMAC-derived public identifier for an authenticated login
session. Sessionless legacy credentials receive a per-connection random identity;
they do not establish cross-note session continuity. Future grouped rosters and
follow must account for that distinction. Cursor state is not stored in SQL or the Yjs
document. Authentication and entry authorization still run on every incoming and
outgoing frame, with the existing periodic revocation check.

The Rust room owns awareness IDs across all gateway connections/workers. An ID
cannot be updated or removed by a second live socket, and an explicit null state
does not release ownership until its socket closes. Disconnect removes the
owned state without advancing another client's clock. The updated plugin advances
its own clock on reconnect, so presence returns without waiting for a heartbeat.
Its query responses and timeout handling publish only its own ID; initial remote
state comes from the relay.

Current bounds are 4,096 bytes per awareness payload/state, 128 owned clients per
room, JavaScript-safe client IDs and clocks no larger than `2^31 - 1`. Sync frames
retain the existing 16 MiB bound. Unknown client message types, duplicate JSON
keys, non-finite values, truncated/overlong integers, extra/trailing messages,
multi-client updates and invalid cursor shapes reject. The plugin now coalesces ordinary updates at 100 ms with a final trailing
update; disappearance flushes immediately. Ingress rate limiting, socket
backpressure and native performance acceptance remain unfinished.

Managed close codes:

| Code | Meaning |
| --- | --- |
| 4400 | Invalid managed protocol or awareness payload. |
| 4403 | Authorization lost or connection otherwise unavailable. |
| 4409 | Awareness ID already owned by another socket or the relay. |
| 4429 | Awareness room has reached its 128-owner capacity. |

The boundary is mandatory for managed sockets. Upgrade the plugin before the
gateway: older providers may forward peers' timeout tombstones or answer queries
with several clients and will be rejected. This work has not been deployed.
The plugin changes only its transient awareness ID after a collision, with three
automatic attempts. It never changes Y.Doc.clientID or document history. It
ignores incoming state for its own ID, protecting local selection from a colliding
initial room snapshot. Capacity does not rotate IDs: the client reconnects for
normal note sync while withholding awareness, then retries presence after
30/60/120 seconds (capped at 120 seconds). Invalid presence or exhausted collision
recovery suspends publication until explicit retry. Queries, warm-up retries and
heartbeats respect suspension. The provider exposes availability and an explicit
retry API, now used by the plugin's native presence controls. Background note
providers start silent. Only the active, authorized, foreground Markdown view
publishes, with an exact document/view binding. Reading publishes no editor
caret. Privacy immediately removes local presence without disconnecting sync.
The client also suppresses stale account/session grants until refreshed; the
gateway remains the authority for authentication and access.

The complete version/rollout UX remains required before release. Do not advertise
the complete presence feature from this checkpoint.

Validation uses protocol tests, Rust room tests, the plugin regression suite and
actual FastAPI/Rust/Yjs connections. Run the disposable presence fixture from
`apps/control-plane` with the plugin checkout and a built relay:

```sh
.venv/bin/python tests/integration/managed_stack.py --presence --plugin /path/to/evc-team-relay-obsidian
```

The fixture verifies authenticated OAuth display data, read-only cursor updates,
spoofing rejection, socket ID switching/collision rejection, bounded payloads,
automatic collision recovery without changing document identity/content,
disconnect/reconnect, editor/Reading transitions, privacy disappearance with
note sync still connected, semantic Reading selection/viewport round trips and
live permission revocation. Existing managed-sync and
text/block-comment fixtures also pass with the new boundary. Native desktop and
mobile two-client presence acceptance is still pending. A disposable desktop
Obsidian instance plus a protocol observer has verified foreground-note
publication, privacy persistence, Reading transitions and popout transfer; this
does not establish two-native-client rendering or mobile acceptance.
