# Authorized location channel preview

The control plane now provides an opt-in, ephemeral channel for following an
already visible participant between authorized Markdown notes. This is a server
foundation: the Obsidian client transport and cross-note navigation are still
unfinished. Current-note Jump/Follow continues to use managed Yjs awareness.
No production service or live vault has been changed by this work.

Enable locally with `COLLABORATION_PRESENCE_ENABLED=true`. The share capability
response advertises `presence_locations_preview`; this does not advertise the
complete presence feature. The default is false. Tickets, active locations and
subscriptions exist only in the serving application's memory, with no database
history, replay, offline queue or activity feed.

## Authentication and lifecycle

1. A client uses its regular session-backed bearer token to POST
   `/v1/collaboration/socket-ticket`. Document/file grants and sessionless
   credentials are rejected. The response contains a protocol version, a
   single-use ticket valid for 30 seconds and `/v1/collaboration/events`.
2. Open that WebSocket without query parameters. Within five seconds send
   `{"type":"authenticate","ticket":"…","instance_id":"<UUID>"}`.
   Neither the login token nor the ticket belongs in the URL or diagnostics.
3. The server consumes the ticket, revalidates the live account and login session,
   and returns `ready` with protocol version 1, the opaque public session ID,
   instance ID and access-token expiry. The instance ID is random for one plugin
   lifetime and stays stable across that instance's note changes. The server
   binds it to the authenticated account and public session; it is not authority
   by itself. One live connection owns each resulting tuple.
4. A replacement ticket can refresh an existing channel using another
   `authenticate` frame. The user, login session and instance must remain the
   same. The response is `authenticated` with the new expiry. Changing accounts
   or login sessions requires a new connection.
5. `{"type":"ping"}` receives `pong`. Clients should ping every ten seconds
   while active and stop/withdraw when the host is backgrounded. The server
   closes channels idle for 30 seconds and rechecks session validity and active
   destination permissions every five seconds.

The companion optional managed-awareness field `instanceId` accepts a canonical
UUID. It connects an already authenticated note participant to this channel's
account/session/instance tuple; the gateway continues to replace account and
session identity. Older awareness states without this field remain valid but
cannot establish cross-note continuity.

## Publishing and following

`presence` has exactly `type` and `location`. A location is null, or an object
with `kind: "note"`, `share_id`, `entry_id` and `surface: "editor" | "reading"`.
The share and entry IDs are UUIDs. Publication requires current read access to
that live Markdown entry. There are no path, title, pixel position, cursor or
selection fields. Cursor/selection/viewport updates stay on the note's existing
Yjs connection. Publish only changes of active note/surface or disappearance.

`follow` has `type`, `subscription_id` (UUID), `target` and `from`:

- `target` contains `user_id`, `session_id` (the opaque 32-hex public ID), and
  `instance_id` previously observed in authenticated note awareness.
- `from` is a location descriptor for the shared context. The target must still
  be visible in that entry, and both users must still have read access.
- Accepted requests receive `following` with their subscription ID, followed by
  a `location` event. Each viewer has at most one subscription.
- An available event contains its subscription ID, `status: "available"`, and
  the location descriptor. Before every delivery, the server freshly checks
  both users' sessions and read access to that exact destination. If the checked
  publisher or destination changes during authorization, it retries the latest
  snapshot instead of delivering the old one.
- A hidden, disconnected, deleted, revoked or inaccessible target produces only
  `status: "unavailable"` and the subscription ID. No destination IDs, path,
  title or distinction between these causes is included. That subscription is
  removed; it does not silently resume when access or visibility returns.
- `unfollow` has `type` and `subscription_id`. A stale subscription ID cannot
  cancel a newer subscription.

Access is checked again periodically even if neither person moves. A delayed
denial for an old note cannot clear a publisher's newer authorized location.
Disconnect/shutdown removes connection ownership and subscriptions and awaits
the channel's receive/delivery/revalidation tasks before cleanup completes.

## Bounds and deployment constraint

The initial implementation supports one designated application process for both
ticket issuance and the WebSocket endpoint. Tickets and subscriptions cannot
cross workers or replicas. Keep this preview disabled on an arbitrary
multi-process deployment until routing/ownership or an ephemeral shared bus is
implemented and tested. Restarting the process loses all transient state.

Bounds are 128 active plus pending connections, 512 pending tickets globally,
eight pending tickets per user, 4,096 UTF-8 bytes per incoming message, ten
messages per second with a burst of 20, and 16 pending control responses per
viewer. Movement has one replaceable latest state, not an accumulating queue;
1,000 changes during a blocked send collapse to the newest location afterward.
Each send has a five-second timeout. Slow or abusive channels close without
changing their independent note synchronization connection.

Close codes are 4400 for invalid messages, 4403 for authentication/access or
idle expiry, 4408 for handshake/send timeout, 4409 for duplicate instance
ownership, and 4429 for capacity/rate/slow-consumer limits. JSON duplicate keys,
non-finite numbers, unexpected fields and unsupported location kinds reject.

## Evidence and remaining work

`tests/test_collaboration_presence.py` exercises the actual ASGI application and
database authorization with bounded WebSocket receives, independent database
connections, private destinations, revocation, deletion, session refresh,
single-use ticket expiry, disconnect/cleanup and ownership. Controlled async
tests cover concurrent destination replacement and slow-consumer coalescing.
The test database is disposable file-backed SQLite; these tests are not a
production PostgreSQL load benchmark or native Obsidian acceptance.

Still required: the client transport, capability negotiation, background and
account lifecycle, credential refresh/backoff, cross-note binding/navigation,
ordering across awareness departure and location arrival, reconnect UX,
real-network/native/mobile evidence, and measured resource budgets. In particular,
the current-note controller must not stop an explicitly active cross-note follow
merely because the publisher leaves its previous Yjs room; manual navigation,
privacy changes and lost authorization must still stop it promptly.
