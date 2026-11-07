# Authorized location channel preview

The control plane now provides an opt-in, ephemeral channel for following an
already visible participant between authorized Markdown notes. The Obsidian
preview now connects to this channel and follows participants across notes;
precise positions and current-note Jump/Follow still use managed Yjs awareness.
The server and plugin transport also support explicitly negotiated version-two
image-file and saved Bases-view locations and authorized surface rosters. The
native host now opts in for directly opened managed images, with participant
controls and whole-image Jump/Follow sharing its note socket. Native image
acceptance, image pointers/embeds and Bases/timeline bindings remain unfinished.
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

## Current image context

`GET /v1/collaboration/image-location?share_id=UUID&entry_id=UUID` is advertised
by `presence_image_context_preview` when presence is enabled. It requires a live
normal session and current managed-entry read access, independently of discussion
flags or comment permission. Scoped/sessionless credentials are rejected. It
returns only `{protocol_version: 1, location: ImageLocation}` with
`Cache-Control: no-store`; no paths, URLs, content bytes or comment data are read
or returned. Deleted/non-image entries and invalid current hashes are rejected.
Replacement returns the current generation/hash for local verification.

This endpoint does not authorize future publication or delivery: the socket
continues to check the descriptor and both parties' current access independently.
The client must also verify its actual local rendered image before publishing.

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

## Opt-in version-two surface locations

`features.presence_surface_locations_preview: true` advertises support for the
version-two location envelope, under the existing presence enable switch. The
capability response and ticket response retain their own version-one formats.
This does not advertise every surface adapter or require discussion permission.
Bases publication additionally requires the Bases registry feature to be enabled.

Include `protocol_version: 2` on the first `authenticate` frame to select the
extended socket protocol. `ready.protocol_version` confirms the selection. An
omitted version selects one, preserving old client behavior. Renewal may omit
the version or repeat the selected value; it cannot change protocol on a live
connection. Strings, booleans, fractional numbers and unknown versions reject.

Version two accepts the original note descriptor plus these exact descriptors:

| Kind | Required fields beyond `kind`, `share_id`, `entry_id` | Authorization |
| --- | --- | --- |
| `image` | `generation` (UUID), `content_sha256` (64 lowercase hex characters) | Live managed image entry, current generation and exact current hash, current read access. |
| `base` | `generation`, `definition_id`, `view_id` (UUIDs), `registry_revision` (positive safe integer) | Live `.base` entry, matching generation, server-owned definition and active saved view, current registry revision and synchronized content hash, current read access. |

No title, path, external URL, cell value, formula output, pointer or arbitrary
target object is accepted. The server reads authorization/identity metadata; it
does not fetch image bytes, parse a base or evaluate a formula for presence.
Embedded-image occurrences and fine-grained Bases/timeline targets need separate
adapters and negotiated schemas before they can be sent.

Initial Follow must reference the same kind and context as the publisher. Notes
may differ in editor/Reading mode; images must reference the same generation and
bytes, and Bases must reference the same saved view/definition/generation/revision.
Being in another view of the same base does not establish a shared context. An
already established Follow may cross these surfaces after the usual checks for
both users. Read-only reviewers do not need comment or edit permission.

New descriptors are never sent to version-one followers. Moving to an unsupported
surface returns the existing generic `unavailable` event and removes the follow
subscription, without leaking identifiers or silently resuming later. Version-one
publishers cannot send the new descriptors. Image replacement, registry change,
unmatched/deleted views, generation changes and revocation invalidate the old
location on delivery or the existing five-second recheck. A subsequent publisher
refresh must use freshly verified metadata; an ended Follow remains ended.

All existing bounds, snapshot-race checks, ephemeral storage and deployment
constraints apply unchanged. Tests exercise version negotiation, old-client
isolation, image replacement, registry/view identity changes, idle revocation and
read-only access against the actual ASGI application and disposable database.
This is not native or production-load evidence.

Client surface bindings/rendering/navigation, image coordinates,
cell/row/group/lane targets and native acceptance remain outstanding.

## Authorized surface rosters

`features.presence_surface_roster_preview: true` additionally advertises roster
commands on version-two connections. The plugin's `LocationChannel` selects
version two only when its caller opts in with `surfaceLocations: true` and the
server advertises support. The current native note host does not opt in yet.
Older servers retain note publication and Follow; non-note locations are
withdrawn and roster commands are not sent.

- `watch` has exactly `type`, `subscription_id` (UUID) and `location` (an image or
  saved-view descriptor). Each connection observes at most one context. Notes
  retain their existing managed-awareness room. A new watch replaces the old one.
- `unwatch` has exactly `type` and `subscription_id`. A stale ID cannot cancel a
  newer watch. Observing and publishing are independent: a hidden viewer can see
  authorized participants without appearing in their roster.
- An available `roster` event has `type`, `subscription_id`, `status: "available"`
  and `peers`. Each peer has only `target: {user_id, session_id, instance_id}` and
  the authenticated display `name`. There are no paths, emails, avatars, cells,
  URLs or client-supplied profiles. The receiving instance is omitted; another
  session/instance of the same account remains distinct.
- Only publishers in the exact watched image version or saved-view identity are
  candidates. The server rechecks the viewer's access, then each publisher's live
  session and access before delivery. The returned identity can initiate the
  existing Follow flow from that context; observation alone never starts Follow.
- Losing viewer access or the watched identity returns only `type`,
  `subscription_id`, `status: "unavailable"` and ends that watch. Publisher
  revocation/departure removes that peer. Idle authorization is rechecked every
  five seconds, including current display names.

The client clears peers immediately on unwatch, scope change, transport loss or
shutdown. A transport reconnect re-observes the desired context with a fresh ID
and fresh authorization; it never replays Follow. An unavailable watch stays
ended until the scope changes or explicit Retry. Late events from an old socket
or subscription cannot repopulate the current roster.

Rooms and observers are bounded by the 128-connection global limit: each
connection publishes to at most one room and watches at most one room. A roster
has at most 127 peers, names of at most 255 code points and a 192 KiB frame budget.
Other server/client frames retain the 4,096-byte limit. Clients reject duplicate
identities and unknown fields. The broker keeps one replaceable dirty indication
per viewer; 1,000 pending membership changes collapse to the latest snapshot.
No roster or activity history is persisted.

Roster and Follow work share one serialized socket writer, with a five-second
combined lock/send deadline. Authorization snapshots are checked again after
waiting for the writer; changed scopes, sessions and room members are discarded
and recomputed. A completed old denial cannot cancel a newer subscription.
This is membership transport, not a cursor-movement stream: fine-grained motion
still needs typed state and measured update/authorization costs before release.

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
non-finite numbers, unexpected fields and location kinds unsupported by the
negotiated protocol reject.

## Evidence and remaining work

`tests/test_collaboration_presence.py` exercises the actual ASGI application and
database authorization with bounded WebSocket receives, independent database
connections, private destinations, revocation, deletion, session refresh,
single-use ticket expiry, disconnect/cleanup and ownership. Controlled async
tests cover concurrent destination replacement and slow-consumer coalescing.
The test database is disposable file-backed SQLite; these tests are not a
production PostgreSQL load benchmark or native Obsidian acceptance.

The version-two location/roster tests cover negotiation, read-only/hidden
observation, saved-view isolation, replacement/revocation, stale unwatch,
disconnect cleanup, delayed authorization/socket writes and membership
coalescing. Image-context cases verify read-only access with discussion features
disabled, current replacement metadata, cache headers, malformed metadata and
revoked/deleted/scoped/sessionless rejection. **191 tests** pass across the location, managed-awareness protocol,
collaboration-policy and Bases-registry suites. Changed Python files pass lint/format checks. The regression set is
scoped, not a claim that the complete control-plane suite was run; existing
test-fixture/library warnings remain. No database migration changed.

The plugin transport negotiates this capability, uses fresh session credentials,
renews tickets on the same socket, bounds retries and pending sends, and withdraws
on foreground/account changes. A viewer may hide their own presence while still
following. The controller tolerates departure from the previous note's awareness
room while waiting for the authorized destination, with an eight-second deadline.
It opens only a unique authorized local entry and preserves the viewer's editing
or Reading mode. Manual navigation/interaction, backgrounding, unavailable
destinations and connection loss stop Follow. Reconnecting republishes current
presence but requires the user to start Follow again; it does not replay the old
subscription.

Run the actual Python/Rust/plugin transport fixture from `apps/control-plane`:

```sh
python tests/integration/managed_stack.py --presence-locations --plugin ../../../evc-team-relay-obsidian
```

It exercises authenticated note discovery, a second shared note, same-socket
credential refresh, inaccessible destinations and idle permission revocation.
The native desktop run also verified cross-note Follow in Source, Live Preview
and Reading, interruption, hidden-peer removal and reload, using a disposable
vault and a protocol peer. Two-native-client/mobile acceptance, reconnect UX
polish, native image acceptance, image pointers/embeds, Bases/timeline adapters
and measured resource budgets remain unfinished.
The single-process deployment constraint above remains in force.

The actual image/Bases client transport fixture runs with:

```sh
python tests/integration/base_content_client.py --plugin ../../../evc-team-relay-obsidian
```

Alongside the existing Bases HTTP cases, this exercises real WebSocket roster
discovery, image-to-Bases Follow, saved-view separation, hidden observation,
same-socket renewal, reconnect, explicit Follow restart and idle read revocation.
It also verifies fresh image-context reads and denial after read revocation.
It seeds synthetic identity metadata in a separate share and removes its
disposable loopback service/database afterward. This is not a native surface or
production resource benchmark.
