# Managed awareness identity and ownership

The managed WebSocket gateway now authenticates and bounds Yjs awareness in
addition to checking document permissions. This is a foundation for the planned
presence feature; it does not implement the active-surface controller, jump,
follow, image pointers or Bases adapters.

`apps/control-plane/app/services/presence_protocol.py` validates one complete
protocol frame, permits one awareness client ID per socket, validates relative
positions against the `contents` text type, and replaces all client identity
fields. Names and picture URLs come from the current account's most recently
updated linked OAuth profile; accounts without a profile use the email local
part as a display name. Unsupported avatar URL schemes and credential-bearing
URLs are removed. No full email, login session UUID, token, path or arbitrary
client state is forwarded as presence metadata.

`sessionId` is a stable HMAC-derived public identifier for an authenticated login
session. Sessionless legacy credentials receive a per-connection random identity;
they do not establish cross-note session continuity. The next presence controller
must account for that distinction. Cursor state is not stored in SQL or the Yjs
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
multi-client updates and invalid cursor shapes reject. These constraints do not
replace the remaining ingress/coalescing and performance work.

Managed close codes:

| Code | Meaning |
| --- | --- |
| 4400 | Invalid managed protocol or awareness payload. |
| 4403 | Authorization lost or connection otherwise unavailable. |
| 4409 | Awareness ID unavailable, including a live collision or room capacity. |

The boundary is mandatory for managed sockets. Upgrade the plugin before the
gateway: older providers may forward peers' timeout tombstones or answer queries
with several clients and will be rejected. This work has not been deployed.
Explicit client collision/capacity recovery and the complete version/rollout UX
remain required before release; the current provider has its ordinary reconnect
behavior. Do not advertise the complete presence feature from this checkpoint.

Validation uses protocol tests, Rust room tests, the plugin regression suite and
actual FastAPI/Rust/Yjs connections. Run the disposable presence fixture from
`apps/control-plane` with the plugin checkout and a built relay:

```sh
.venv/bin/python tests/integration/managed_stack.py --presence --plugin /path/to/evc-team-relay-obsidian
```

The fixture verifies authenticated OAuth display data, read-only cursor updates,
spoofing rejection, socket ID switching/collision rejection, bounded payloads,
disconnect/reconnect and live permission revocation. Existing managed-sync and
text/block-comment fixtures also pass with the new boundary. Native desktop and
mobile presence acceptance is still pending.
