# Saved-view timeline range protocol preview

The control plane can persist range discussions for the Time & Line Timeline and
Sequence views. The client can discover an axis, retain its proof offline, capture
an explicit interval and deliver it through the ordinary comment queue. This is
protocol/session implementation: native range selection, projection, visible
markers, navigation and mobile acceptance remain unfinished. Keep
`COLLABORATION_BASES_TIMELINE_RANGES_ENABLED=false` outside disposable development
fixtures until the matching UI and release acceptance are complete.

## Identity and authorization

`POST /v1/collaboration/shares/{share}/entries/{entry}/bases/timeline-axis` accepts
`view_id` and optional `expected_revision`. A reader receives the managed file,
definition, saved view, generation and registry revision, plus one axis identity,
revision, fingerprint and descriptor. The server derives this from synchronized
`.base` bytes; it never accepts a client description as proof of configuration.

The descriptor records the renderer, start/end property namespaces, explicit
configured timezone or null for device-local display, and semantic version one.
Sequence coordinates are original property values, independent of dense packing.
Formula axes conservatively include every formula definition in their revision
fingerprint without evaluating any expression. No record values are returned.

An axis uses the existing `BaseTargetDefinition` registry with kind `axis` and
name `primary`. No database migration or new table is needed. Semantic changes,
removal/reappearance and observed change-and-revert invalidate new captures;
presentation changes such as zoom, sorting, filters and dense layout do not.
There is at most one axis per active saved view (at most 64 views). Its separate
budget preserves the existing formula/group/summary catalog limits. The old
`target-definitions` endpoint filters axes out, preserving strict older decoders.

A `BaseAnchor.target` of type `timeline_range` contains:

- `axis_definition_id`, `axis_definition_revision`, and the captured `axis`;
- a versioned `range` using the coordinate contract below;
- optional `lane`, containing the complete existing group target, typed value,
  definition revision, group ID and exact source witness ID.

Creation checks current file/view access, comment permission, synchronized
configuration, generation, registry proof, axis revision and descriptor. An
optional lane receives the same recursive validation and exact source-access
checks as a standalone group discussion. A different readable member with the
same label/value cannot replace the retained witness. Global intervals have
view-level authority; they disclose no computed record values or membership.

Existing discussions retain their original axis and coordinates after edits,
view changes or creation-feature disablement. Every read, reply, receipt retry,
picture access and inbox authorization continues through the existing Bases
thread authorization. Lane access is rechecked on these paths. The new feature
flag gates creation/discovery, not access to already accepted discussion history.

## Coordinate contract, version one

Both implementations use the same checked-in fixture file,
`tests/fixtures/timeline-ranges-v1.json` in the server and
`__tests__/fixtures/timeline-ranges-v1.json` in the plugin. Decoders reject missing,
unknown and coerced fields. Bound inclusivity is explicit.

| Kind | Stored meaning and bounds |
| --- | --- |
| `calendar` | Strict Gregorian `YYYY-MM-DD`, years 1–9999, and an explicit timezone. A closed endpoint includes the entire day; an open endpoint excludes that day. |
| `instant` | Explicit-offset timestamps with 1–3 fractional digits or whole seconds, normalized to milliseconds. Preserve capture offsets; compare and key by UTC instant. Reject unknown `-00:00`, absent offsets, invalid dates/times, leap seconds and UTC overflow. Equal endpoints require both bounds closed. |
| `historical` | Astronomical years −1,000,000,000 through +1,000,000,000, including year zero, with explicit year/month/day precision and proleptic Gregorian calendar. Arithmetic never passes through JavaScript Date or Python datetime. Bounds include/exclude entire stated periods, including mixed-precision endpoints. |
| `ordinal` | Exact decimal strings: at most 15 integer digits and nine fractional digits, unit `property-value`. No binary float conversion for identity/order. Normalize negative zero and trailing fractional zeros. Never persist pixels or dense row/rank coordinates. |

Timezone names have bounded portable syntax. Persisted decoding does not depend
on the host's timezone database, which may differ across server/desktop/mobile
versions. Authoring separately requires local support; the server also verifies
support when accepting a new calendar/instant range. Unknown stored zones must
remain readable with explicit unavailable projection, never a guessed local zone.
Configured zones must match the range; a view without a configured zone requires
an explicit supported zone in each calendar/instant capture. Exact instants always
carry their capture offsets, including both occurrences of a repeated DST hour.

These are semantic coordinates. Renderer precision/geometry is a separate
contract: an exact ordinal interval cannot be silently rounded into a different
interval when the renderer uses JavaScript numbers. Geometry must disclose or
refuse unsupported precision. Historical precision and open/closed boundaries
must be preserved when drawing, hit testing and navigating.

## Client persistence and recovery

The client strictly decodes axis responses and range anchors, negotiates the new
`timeline_range` target, labels stored ranges, and retains them in drafts and the
normal outbox. An axis selection carries its authoritative fingerprint/revision;
creation rechecks it rather than comparing incidental JSON property order.

`readTimelineAxis` uses account/scope-isolated durable preferences, verification
tickets and generation checks. Network failure may use an already verified
context. Authoritative denial invalidates that context; it cannot fall back to
old proof. Capture rechecks permission and identity after asynchronous work.
Registry disagreement requires reselection while retaining draft text.

Cache version 17 preserves all existing stores and fences older workers that do
not understand range/axis/lane targets. No comments or IDs are inserted into task
Markdown or `.base` files. Ordinary queued range creation retains existing
operation IDs and receipts across lost responses and cache reopen.

## Verification and remaining work

Shared fixtures exercise leap dates, early CE dates, explicit DST overlap offsets,
BCE/year zero, whole-period and mixed-precision bounds, large adjacent ordinals,
invalid schemas and unavailable timezone data. API tests exercise persistence,
view isolation, separate comment permission, revision changes/reverts, descriptor
forgery, feature disablement, legacy catalog capacity and exact lane access.

The real Python/Rust/client fixture runs both `sabidurian-timeline` and
`sabidurian-sequence` with `tests/integration/managed_stack.py --base-groups`.
Each variant captures a lane-bound range while offline using a verified axis,
reopens its queued state, loses an accepted response, retries the same operation,
and verifies the original target after relay restart. It also retains the prior
note/file group capture and source-permission coverage. These are disposable
localhost services and synthetic accounts, not native Obsidian acceptance.

Required next work: Time & Line range-selection/axis-conversion API, explicit
precision/offset controls, rendering/markers, reveal and dirty-inspector guards,
new unverified offline lane capture composition, keyboard/touch behavior and
native multiwindow/mobile acceptance. PostgreSQL-specific range concurrency and
upgrade acceptance also remain; the HTTP fixture uses SQLite. Range support does
not complete shared milestones, timeline presence or the wider collaboration
release.
