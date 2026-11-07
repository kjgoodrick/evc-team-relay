# Shared timeline marker server preview

The control plane now provides stable shared marker identities, explicit saved-
view placements and marker discussion anchors. This is a **server preview**, not
a usable native marker feature yet. Keep
`COLLABORATION_BASES_TIMELINE_MARKERS_ENABLED=false` outside disposable tests until
the matching client, Time & Line renderer and native acceptance are complete.
The flag also requires existing timeline-range support. It does not enable the
full collaboration release.

## Identity and permissions

A marker belongs to one managed `.base` definition and may be explicitly placed
in several of that file's saved views. Its UUID survives label/color/coordinate
edits, placement changes, archive and restore. Threads remain scoped to their
original saved view even when the same marker appears in another view. Sharing a
marker across unrelated `.base` files is not implicit; their permissions and
identities are separate.

Only a member with edit permission on the managed `.base` may create, update,
place, archive or restore a shared marker. A commenter can annotate an existing
marker without changing the `.base`, task Markdown or local plugin settings. Every
metadata read and discussion access rechecks the current file/share/session
authority. Accepted management retries still require current editor authority.

The content includes a plain-text label, six-digit hex color, `point` or `range`
kind, and the existing version-one timeline coordinate value. Points require
identical, closed endpoints. Calendar dates, explicit-offset instants, historical
precision/BCE and exact Sequence decimals use the same codecs as range comments.
No pixel, array index, displayed label or dense row is a marker identity.

Each placement records the server-verified axis ID/revision/descriptor for its
saved view. Creation rejects unsupported axes, mismatched coordinate kinds or
timezones, stale configuration and forged descriptors. A marker never silently
changes from a calendar interval to an ordinal interval after a view-type change.
An editor must explicitly update its placement against the current axis.

## API and transaction contract

Under `/v1/collaboration/shares/{share}/entries/{entry}/bases/timeline-markers`:

- `POST /catalog` reads a bounded catalog, optionally scoped to `view_id` and/or
  including archived markers/placements. Continuing with `after` requires the
  catalog revision. A changed revision returns `timeline_marker_catalog_changed`.
- `GET /{marker_id}` reads one marker with its retained view placements. This
  supplies the complete state before an editor updates its associations.
- `POST /commands` accepts `marker.put` or `marker.archive`, an operation UUID,
  the definition/generation/registry revision, and an expected catalog revision.
  Put additionally specifies the expected marker revision (`0` for creation),
  content and the complete intended set of active placements. Omitting a previous
  placement archives that association. Putting an archived marker at its current
  revision restores the same identity; it does not recreate old IDs.

The authenticated share lock serializes marker metadata, configuration/inventory
changes and discussion creation. Catalog and marker comparisons prevent concurrent
edits from overwriting each other. Repeating an accepted operation returns its
original receipt; changing the intent under the same operation ID is rejected.
Two competing promotions using the same catalog revision cannot both succeed.
The client must reconcile the conflict and offer explicit matching; the server
does not deduplicate markers merely because their labels/dates happen to match.

Metadata, placement state, immutable revision snapshots, catalog revision and
receipt commit together. There is no network/object-store I/O inside this
transaction. Reads use an indexed, bounded page plus one batched placement query.
Initial limits are 50 markers per page, 256 retained markers per `.base`, 64 active
placements per put, 256 retained view associations per marker and 64 KiB per
command. These are explicit errors, never silent truncation or identity reuse.

## Automatic client refresh

Saved-view `GET .../changes?view_id=...&timeline_markers=true` responses include
an optional `timeline_markers` revision hint with protocol, entry/view/definition,
generation, registry and catalog identities. The usual discussion read and view
authorization apply before returning it. The hint contains no label, coordinate,
marker ID, or body; clients still use the authorized catalog to obtain placements.
Empty discussion pages report current revisions too, so marker-only activity
does not require creating a thread or adding synthetic discussion events.

Older clients do not request the field and receive the existing response shape.
Older servers ignore the query flag; new clients tolerate its absence. Creation
flag disablement does not hide existing marker metadata or prevent recovery.
This adds no migration, background worker or separate client poller. Active
clients use their existing five-second discussion refresh and fetch full marker
catalogs only when their verified revisions need refreshing. Hidden/unwatched
discussion scopes keep their existing suspension behavior.

## Discussion anchors and archival

The new `BaseAnchor.target` kind is `timeline_marker`. It carries marker ID and
revision, captured content, and captured axis ID/revision/descriptor. Creation
checks the immutable server snapshot and currently active marker/placement, then
reuses the range-axis validation. The caller cannot invent the label, range or
view association behind an otherwise valid marker UUID.

Editing or archiving a marker does not rewrite accepted anchors. A thread retains
the marker context at capture; future UI can show that context alongside the
current marker position. New stale captures require reselection. Existing thread
reads, replies, exact receipt retries, picture access and inbox authorization use
the same Bases authorization path and remain possible after archival or creation-
feature disablement, while current file permissions still apply.

Migration `202611070001` adds catalog, marker, placement, immutable revision and
receipt tables. Empty downgrade/upgrade is supported; populated downgrade refuses
to discard marker identities/history/receipts. These tables belong in the coherent
database backup/restore boundary. This guard is not a backup or rolling-upgrade
procedure.

## Verification and remaining implementation

The revision-hint increment passes **549 tests across the 13 collaboration
regression modules**, including **18 marker cases**. The opt-in hint advances
without thread events, remains readable with creation disabled, rejects a wrong
view, and leaves legacy response shapes unchanged. All six actual HTTP/client
tests pass, including catalog-version delivery and verified-cache reuse through
the real session/transport. Changed-source lint/format and whitespace checks pass.
This is collaboration regression coverage, not a complete control-plane suite.
The server adds no schema or transaction mutation in this increment; the earlier
PostgreSQL mutation/migration evidence below remains at its recorded checkpoint.

API coverage includes all four point/range coordinate kinds, editor versus
commenter permissions, exact retry, compare-and-swap conflicts, separate saved-
view threads, detached/archived history, restoration, stale axes, feature disablement,
pagination, limits, file revocation, forged targets and injected precommit rollback.

The existing disposable `base_discussions_postgres.py` runner now also invokes
`timeline_markers_postgres.py`. It requires an empty `127.0.0.1` database named
`collaboration_test`. PostgreSQL 16.15 checks verify ORM/migration agreement,
concurrent duplicate promotion, competing operations against one catalog revision,
comment creation waiting behind archive, marker management waiting behind editor
revocation, historical receipts and the populated downgrade guard. Existing Bases,
group and range checks pass in the same run. No production service or vault is used.

The November 7 checkpoint passed **398 server regression tests**, including **17
marker cases**, plus the complete PostgreSQL Bases/group/range/marker runner.
Changed-source lint, formatting and whitespace checks also pass. The disposable
database and synthetic accounts were removed after verification.

The matching client now decodes exact marker anchors, reads revision-pinned saved-
view catalogs and retains verified metadata in its account-isolated cache.
Marker captures use its existing durable drafts/outbox; historical status retains
the captured label/position. The disposable `base_content_client.py` runner now
also checks the actual client against HTTP for calendar and exact-decimal markers,
including offline capture after reopen, a lost accepted response, edits/archive
before exact retry, historical replies and stale new-capture rejection. The test
fixture explicitly enables both timeline flags; production defaults are unchanged.

Client `86f2153` connects a shared-marker picker to verified capture, explicit
refresh and guarded position inspection. Existing marker discussions render at
their captured coordinates and navigate using the original saved axis, with
marker identities/revisions kept separate from ordinary ranges. The actual
Time & Line source interoperates in both load orders; native acceptance is still
pending. No server runtime or feature defaults changed in that increment.

The client now also provides online creation/edit/archive/restore and explicit
recovery controls, current-marker overlays independent of discussions, and
automatic refresh driven by the opt-in revision hint. Current marker labels and
captured discussion positions remain distinct. Native acceptance is still pending.

The client now offers a read-only captured-versus-current comparison with explicit
fresh/cached status and separate guarded navigation. Its unit/sidebar and actual
Time & Line source interoperability checks pass; native acceptance remains. See
the [client comparison record](../../evc-team-relay-obsidian/docs/timeline-marker-client-preview.md).

The client also uses the existing base-wide summary catalog and selected-view
axis endpoint to add explicit saved-view placements. Its actual HTTP fixture
checks Timeline and Sequence additions, preservation of original placements and
captured discussion targets, and unchanged `.base` bytes. No server protocol,
schema or default changed. See the
[client placement record](../../evc-team-relay-obsidian/docs/timeline-marker-client-preview.md).

Remaining work includes local marker promotion/matching;
direct point picking; native comparison/placement acceptance and overlap
polish; broader recovery; server-restart/client and native desktop/mobile/
accessibility/resource acceptance.
Management uses online guarded commands; queued comments must reuse verified
cached marker/axis evidence. Do not enable this preview before those paths exist.
