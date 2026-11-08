# Authorized location channel preview

The control plane now provides an opt-in, ephemeral channel for following an
already visible participant between authorized Markdown notes. The Obsidian
preview now connects to this channel and follows participants across notes;
precise positions and current-note Jump/Follow still use managed Yjs awareness.
The server and plugin transport also support explicitly negotiated version-two
image-file and saved Bases-view locations and authorized surface rosters. The
native host now opts in for directly opened managed images, with participant
controls and whole-image Jump/Follow sharing its note socket. The foreground host
also connects verified saved Bases views, participant controls and transient
whole-view navigation through the existing native and Time & Line adapters.
Precise cell/record/header pointers and navigation are now connected through
those adapters with automated host evidence. Native acceptance, visible
split-pane image observation and group/lane/range movement remain unfinished.
Direct images now have a separate authorized pointer/viewport stream. Embedded
images also have a negotiated transport and exact source anchors; the foreground
plugin host now connects capture, overlays and image-aware navigation with
automated host evidence. Native desktop/mobile acceptance remains unfinished.
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

## Read-only Bases context

`GET /v1/collaboration/base-context?share_id=UUID&entry_id=UUID` is advertised by
`presence_base_context_preview` only when both presence and the Bases registry
are enabled. It uses normal session credentials and current managed-entry read
access, independently of comment permission and the discussion feature flag.
The response has `Cache-Control: no-store` and contains protocol version 1,
share/entry/generation/definition IDs, registry revision, synchronized SHA-256
and up to 64 active views with ID, slot, name and type. Sparse slots are valid;
unmatched and archived views are excluded.

This lookup does not invoke registry inspection/reconciliation, initialize IDs,
read object storage or change any database/file state. A single bounded SQL
statement reads the current identities and revision consistently, selecting only
view names/types from descriptors. It does not load the saved configuration,
filters, formulas or record data. Missing/stale definitions, changed generations,
invalid hashes and malformed identity metadata reject without returning a context.
Normal Bases sync/identity management must establish the registry first.

The client negotiates this capability independently, validates/freezes the result,
and fences late HTTP results after channel shutdown/replacement. Its native
adapter verifies local disk bytes, runtime configuration and saved-view slots.
The foreground host now uses this for Bases participants, whole-view navigation
and the precise cell/record/header stream below. Socket publication/delivery
still rechecks both users' current permissions and exact saved-view identity.

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
Embedded-image occurrences use the separate negotiated stream below. Fine-grained
Bases cell/record/header positions use the separate negotiated stream below;
groups, lanes and timeline ranges still need their target extensions and adapters.

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

Fine-grained Bases/timeline group/lane/range targets, visible split-pane
image observation and native desktop/mobile acceptance remain outstanding. Direct image coordinates use
the separate stream below and do not change the room identity.

## Authorized surface rosters

`features.presence_surface_roster_preview: true` additionally advertises roster
commands on version-two connections. The plugin's `LocationChannel` selects
version two only when its caller opts in with `surfaceLocations: true` and the
server advertises support. The current native note/image/Bases host opts in.
Older servers retain note publication and Follow; non-note locations are
withdrawn and roster commands are not sent.

- `watch` has `type`, `subscription_id` (UUID) and `location` (an image or
  saved-view descriptor). Each connection observes at most one context. Notes
  retain their existing managed-awareness room. A new watch replaces the old one.
  The separately negotiated image stream additionally allows `image_states: true`
  only on an image watch. Bases positions similarly require `base_states: true`
  on a saved-view watch. Old watchers receive no unsolicited movement frames.
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

## Image movement

`presence_image_states_preview` advertises version-two `image_state` frames with
exactly `type`, `location` (ImageLocation) and `state`. The state is null or exactly
`{pointer, viewport}`: pointer is null or `{x,y}`; viewport is null or
`{x,y,width,height}`. All numbers are finite normalized fractions in [0,1];
viewport dimensions must be positive and its extent must fit within the image.
Booleans, strings, unknown fields, arbitrary identities and non-image locations
reject. State is accepted only for the sender's current visible exact image and
fresh session/read authorization. Each movement uses one authorization session.

An opted-in observer receives independent `image_states` snapshots with the
watch subscription ID and `status`. Available snapshots contain `peers` with only
authenticated `target` and `state`; empty snapshots clear all geometry. Unavailable
snapshots omit peers and end observation. The client uses roster names; movement
does not rebuild participant identity or trigger cross-file Follow events.

Every connection stores one latest state and each observer one dirty event.
The server coalesces concurrent movement for 16 ms, reauthorizes the viewer and publishers,
then reads current states. It never queues a path/history. Movement does not
invalidate membership authorization in progress; context/token/revision changes
still fence both authorization results and writes waiting on the shared socket.
The same 192 KiB bound, 127-peer limit, message-rate limit and five-second idle
authorization checks apply. Hiding, context changes and disconnect clear state.
Client coalescing allows at most eight ordinary movement frames per second;
immediate clearing still consumes the existing burst budget.

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
revoked/deleted/scoped/sessionless rejection. Movement cases cover malformed
coordinates, legacy watchers, read-only publishers, stale versions/revocation,
context changes during socket waits and 1,001 coalesced moves without roster work.
Bases-context cases additionally verify active-only identities, a single coherent
metadata query with no writes/configuration load, sparse views, revision changes,
read/session revocation, stale generations/hashes, malformed metadata and gates.
**265 tests** pass across the location, managed-awareness protocol,
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
polish, native image/embedded-image acceptance, fine-grained Bases/timeline presence
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
The read-only Bases context is fetched through the actual client transport and
its active views reconstruct the exact saved-view locations. Native source/view
binding has separate automated adapter tests; no native Bases presence run is
claimed by this transport fixture.
Pointer/viewport delivery is verified separately from membership, including
surface departure and permission revocation.
It seeds synthetic identity metadata in a separate share and removes its
disposable loopback service/database afterward. This is not a native surface or
production resource benchmark.


## Embedded images in Markdown notes

`presence_embedded_images_preview` advertises a version-two extension on the same
location socket. It deliberately does not extend Yjs awareness: a note reader
may lack permission to read an embedded managed image, so delivery needs a
separate authorization check for each viewer and asset. Existing note cursors,
selections, membership and Follow continue on their existing paths.

Publish `{type: "image_embed", location: NoteLocation, embed: EmbeddedImage|null}`
only while visible in that note. One connection holds at most one current embed.
`EmbeddedImage` has exactly:

- `version: 1`;
- `occurrence: {anchor, head}` using bounded Yjs relative positions in `contents`;
- `source_sha256`, the lowercase SHA-256 of the exact Markdown image occurrence;
- `asset`: either `{kind: "managed", entry_id, generation, sha256}` or
  `{kind: "external", url_sha256, version_evidence: "unknown"}`;
- `basis: {width, height, orientation: "display"}`, positive integer intrinsic
  dimensions no larger than 1,000,000 per axis;
- `state: {pointer, viewport}`, with the same normalized bounds as direct images.

No source text, image URL, path, pixel bytes or supplied user identity is accepted.
External URLs are hashed by the client and remain explicitly unversioned; the
server does not fetch them. The server validates bounded source positions but
cannot establish their meaning inside the document. A rendering client must
resolve the exact relative interval, verify its source hash, identify the actual
local asset/version and check intrinsic dimensions before showing coordinates.
Matching by URL, filename or ordinal alone is insufficient for repeated embeds.

`watch_embeds` has exactly `type`, `subscription_id` and `location: NoteLocation`.
It shares the one surface watch with `watch`; changing watch kind replaces the
previous observation. A hidden viewer can observe. Editor and Reading locations
of the same note share the embedded-image room. This watch emits only
`{type: "image_embeds", subscription_id, status: "available", peers}`; each peer
contains the authenticated `target` tuple and its `embed`. Names must be joined
with authenticated note awareness by the complete user/session/instance tuple.
There is no duplicate roster or client-supplied name in the movement stream.
A note access denial produces `status: "unavailable"` without peers and ends the
watch. An asset denial omits that peer's entire embed, including IDs/hashes,
without ending the still-authorized note watch.

Publisher and viewer must each have current read access to both the note and
managed image. The image must belong to that share and match its current
entry/generation/hash. Comment/edit permission is unnecessary. Session, note and
asset checks run at publication/delivery and during idle revalidation. Losing
image access clears the embed without hiding a still-readable note. Hiding,
departure and disconnect clear it too; there is no persistence or reconnect replay.

The client coalesces ordinary movements to 125 ms; the server retains one latest
state and batches at 16 ms. Occurrence, asset, basis and source-hash changes bump
a separate semantic revision that fences pending authorization and socket writes.
Geometry updates do not invalidate permission work and cannot starve delivery.
Movement never dirties a duplicate note roster or the publisher's own observer.
The existing 4 KiB inbound, 127-peer/192 KiB outbound, rate and single-process
limits apply. This is not measured mobile or production-load evidence.

Validation at this checkpoint adds permission/version/repeated-occurrence and
controlled delayed-write tests, plus real client-to-server WebSocket delivery
and asset-denial checks. The foreground plugin host now integrates native source/
render verification, pointer capture, overlays and Jump/Follow, with automated
CodeMirror/Reading host tests. Native desktop/mobile, split-pane and performance
acceptance remain required. This protocol capability does not advertise complete
release acceptance.

## Precise Bases position transport

`presence_base_states_preview` is enabled only with presence and the Bases
registry. Version-two `watch` can request `base_states: true` for an exact saved
view. The separate `base_state` publication has `type`, `location` (the current
Base descriptor) and `state` (null or the shape below). A `base_states` snapshot
has `subscription_id`, `status` and, when available, `peers: [{target, state}]`.
The peer `target` is the authenticated user/session/instance tuple. Names remain
in the independent roster; movement does not rebuild its controls.

```json
{
  "version": 1,
  "pointer": {
    "target": {
      "type": "cell",
      "record_id": "<UUID>",
      "record_generation": "<UUID>",
      "property": {"source": "note", "name": "status"}
    },
    "offset": {"x": 0.25, "y": 0.5}
  },
  "viewport": null
}
```

Both pointer and viewport are optional positions (explicit null); the viewport
represents a semantic point near its center rather than a pixel scroll offset.
Offsets are finite normalized fractions of the locally verified target bounds.
Targets are record `item`, record `cell`, or `property` header. Items/cells pin
record ID and generation. Cells/headers carry a property namespace (`note`,
`file`, `formula`) and bounded name (128 code points, 256 UTF-8 bytes). No cell
values, formula results, filenames, contents or client identity are accepted.
This schema describes where someone is pointing, not the formula's output or
permission to evaluate any dependencies. Native adapters must resolve and prove
the target against the current saved view before rendering/navigation.

`GET /v1/collaboration/base-record-context?share_id=UUID&entry_id=UUID` returns
only `{protocol_version: 1, share_id, entry_id, generation, type}` with no-store.
It requires a current session and record read access; it returns existing
lineage without initializing missing IDs, reading note/object bytes, requiring
comments or writing to the database. Supported record types are markdown, image
and file. Missing lineage is unavailable until normal identity management
establishes it. A host must bound/reuse these reads rather than fetch on movement.

The server independently checks the saved Base and every referenced record for
both publisher and viewer. It requires the current record generation; note
properties require a Markdown record. A record denial omits the publisher's
entire precise state, including IDs/property names/offsets, while its authorized
saved-view roster presence remains. Publisher ingress denial closes the channel;
idle record revocation withdraws its position without removing a still-readable
Base location. Both surfaces and positions are rechecked on delivery and during
the five-second idle pass. Record IDs must belong to the Base's share.

Record/property/generation changes fence pending authorization and socket sends.
Geometry changes within the same targets coalesce to the latest state without
invalidating an in-flight authorization or marking the roster dirty. The existing
single socket, movement worker, 125 ms client coalescing, 16 ms server batch,
128-connection limit, 4 KiB ingress and 192 KiB snapshot limits apply. No position
is persisted or replayed after reconnect; hiding, surface departure and unload
clear it. Old watchers never receive unsolicited position frames.

Transport checks include actual client/server cell delivery, normal credential
lookup, independent idle record revocation with preserved Base roster, clearing,
controlled delayed sends after semantic changes, and 1,001 geometry updates
without roster work. **Native capture, overlays and position-aware Jump/Follow
are now connected to this stream**, with table/cards/list and Time & Line bridge
host tests against simulated native layout. The client bounds record lookups,
checks local access/generation, materializes virtualized cells and cancels stale
navigation. Group/lane/range extensions and native
Obsidian desktop/mobile, accessibility and resource acceptance remain required.

## Version-two Bases group and range positions

The `presence_base_targets_v2_preview` capability requires presence and the Bases
registry. Observers opt in with `base_target_version: 2` on an existing
`base_states: true` watch. A version-two state adds `group` and `timeline_range`
targets; version one rejects them. Legacy observers receive version-one state
with unsupported slots cleared, preserving any supported cell/item/header slot.
The participant roster is independent. Other streams retain their old limits.

Groups carry the current saved grouping property, one member record and its
generation, a 64-character value fingerprint and at most eight additional record
references. Current read access and generation are checked for every input for
both publisher and observer, including on idle revalidation. A denied input
withdraws the entire precise state without withdrawing an otherwise readable
Base participant. Note groups require a Markdown member. Formula groups require the separately negotiated
[version-three extension](base-formula-presence.md); non-inventory file groups
remain unsupported. Group labels,
values and paths are not transmitted. Fingerprints are only client matching hints;
the server does not attest membership or treat a hash as an authorization proof.

Timeline ranges reuse the typed coordinate codec and carry the saved axis plus a
nullable group lane. The axis must exactly match the active saved view. Ordinary
axes require only the view descriptor; formula axes additionally check the saved
formula configuration, without evaluation or record-content reads. Inputs,
descriptor and axis are reused within a state check. No registry initialization,
comment capability, target-definition write or pointer-history storage occurs.

The whole canonical state is limited to 3,072 UTF-8 bytes within the existing
4 KiB ingress frame. Only negotiated version-two Bases snapshots raise the output
ceiling to 512 KiB, accommodating all 127 possible peers at the bounded state size.
Existing rate/coalescing/connection limits apply. Changed fingerprints, references,
axes, dates or lanes invalidate a pending authorized send; offsets are coalesced.
Legacy downgrade happens after authorization and before serialization.

Native member-backed group/lane capture, projection and navigation now have
client host evidence through table/cards/list and Time & Line adapters. Native
date-range capture, projection and Jump/Follow now also have real Time & Line
source/client interoperability evidence. Complete desktop/mobile acceptance and
additional group adapters remain unfinished.
Server-side automated checks cover read-only handling, legacy downgrade, denied dependencies,
idle revocation with preserved roster, obsolete axes/grouping, strict input and
full-room output budgets, and concurrent target changes. The actual client/service
fixture exchanges groups and lane ranges without roster churn. No migration or
production deployment is part of this increment.

## Multiple passive observations

`presence_multi_watch_preview: true` advertises an additive version-two extension
under the existing presence enable switch. Old clients keep their single `watch`
or `watch_embeds` slot unchanged. A new client may also send `observe` with a
fresh UUID `subscription_id` and a validated `location`, and optional image/Bases
state flags identical to `watch`. A note location observes embedded-image state;
text selections and identities still come from the existing managed awareness
connection. `unobserve` removes only the matching additional subscription.

Each connection allows eight additional observations plus its foreground watch.
An inaccessible initial observation receives an unavailable event for that
subscription without ending the foreground watch, publication, Follow or other
observations. Invalid authentication still ends the connection. Every delivery
and idle recheck validates the subscription's own entry/version and publisher
permissions. Revocation removes only that observation. Replacement/removal fences
in-flight authorization and writes by the exact subscription object.

The two existing roster/state workers service all subscriptions; no task or
connection is created per pane. Dirty bits are per subscription, so image motion
does not reauthorize unrelated rooms. A slow consumer retains only the latest
state. Cached output comparisons retain SHA-256 fingerprints, not roster payloads.
Per-message limits still apply, with a shared output allowance of 1 MiB/second
and a 2 MiB burst across roster, state, Follow and control frames. Exceeding the
observation or output budget closes with 4429. These are protective preview bounds,
not a measured capacity claim; small-group/mobile resource acceptance remains.

The plugin transport deduplicates scopes (including editor/Reading variants of
one note), isolates observation callbacks from foreground callbacks, rejects more
than eight scopes before mutation, ignores removed subscription IDs, clears local
observation state on disconnect, and obtains fresh IDs on reconnect. Ordinary
refreshes do not restart a denied observation; explicit Retry or reconnect does.

Validation includes 244 presence tests and 11 capability/policy tests; the new
checks cover mixed scope ownership, denial/revocation isolation, same-room
foreground/passive cleanup, capacity, delayed replacement, coalesced movement and
combined output limits. The actual plugin transport also passes disposable HTTP/
WebSocket tests with simultaneous foreground Bases and passive image/note scopes.
Native different-document pane wiring and acceptance are the next step; this
transport checkpoint alone does not claim that UI behavior is complete.
