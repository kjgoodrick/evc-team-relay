# Typed Bases group values from verified source

Status: November 6, 2026. `base_group_values.derive_note_value` derives typed
values and their source references from a private source observation. A gated
capture endpoint, durable group authorization and client protocol now have
PostgreSQL and actual Python/Rust/Yjs integration evidence. The plugin now has
typed native controls, markers/navigation, durable offline capture intents and
authorized retained-source loading. Integrated native group-comment acceptance
remains pending. Inventory-backed file-property groups now have server/client
and actual-service evidence; remaining file properties and formulas are unfinished.

## Group capture and authorization

`POST .../bases/group-targets` accepts a saved view/grouping-definition identity,
managed source lineage, bounded snapshot dependency, timezone and proposed typed
value. It checks scope comment/source read access, releases its SQL transaction
for private source observation, then rechecks current authority and definition
before deriving and comparing the value. Only server-derived references become
evidence; no client source body or claimed dependency list is accepted.

The registry separates a group value from immutable source witnesses. Each
thread's anchor selects **one exact witness**. Every read/write/history/picture/
mention/receipt path checks every reference of that witness. A second accessible
record with the same value may share group identity, but cannot unlock a thread
created from protected source evidence. Never authorize a thread through any
alternative witness merely because its group ID matches.

Historical source proof remains usable after membership changes while the
original inputs retain their lineages and current read permission. New captures
require current grouping-definition semantics; semantic edit/reversion creates
a different group revision. Accepted exact retries preserve their result after
creation disablement, subject to current permission and lineage checks.

The first adapter supports managed note-property sources in one share, with a
bounded conservative link resolver. Ambiguous links, missing target lineage and
unsupported property adapters fail explicitly. It does not evaluate filters or
prove that a historical group is currently rendered. The inventory adapter below
adds a separate set of file properties. Formula groups, the remaining file
properties and aggregate dependency closure remain unfinished.

Migration `202611060006` adds group values, source witnesses and capture receipts;
downgrade refuses populated tables. The 34 focused tests cover duplicate capture,
source revocation during unlocked I/O, per-thread isolation, linked dependencies,
mentions/pictures, semantic revisions, request bounds and disabled retries.
The broader discussion/source/definition regression run passes 402 tests.
Actual PostgreSQL verifies ORM/migration parity, empty-table downgrade/upgrade,
concurrent exact capture, matching-value isolation, revocation/lineage/definition
changes during unlocked observation, populated downgrade refusal and cascading
fixture cleanup. Private source observations are controlled in this concurrency
fixture; the separate actual-service fixture uses the real source reader.

The plugin's actual HTTP/Rust/Yjs test verifies delete-only dependency waiting,
lost capture-response recovery, historical group access after a property change,
source access denial, fresh capture after relay restart and unchanged witness
identity. Verified anchors, drafts and queued comments survive a local database
reopen. New group selections can remain local pending intents while offline and
obtain a server witness during delivery. Cache version 15 preserves earlier work
while fencing workers that cannot retain inventory-backed capture intents.

The client validates and copies bounded typed values before asynchronous
credential lookup, binds capture responses to the requested identity/revision/
value, and negotiates explicit group support. Captures and group-create commands
allow at most 256 KiB: a legitimate bounded list can exceed 64 KiB when it contains
many managed references. Ordinary commands retain their 64 KiB limit. Both
transport and service limits retain accepted retries after creation disablement.

Enablement remains opt-in through `COLLABORATION_BASES_GROUPS_ENABLED`, existing
Bases definition/discussion gates and the private source credential. The relay
must have its private collaboration source/barrier support enabled. No production
flag or service changed. Upgrade clients before enabling group creation.

## Inventory-backed file properties

`name`, `basename`, `fullname`, `ext`, `path`, `folder` and `file` now use a
distinct inventory capture shape: the common identity/value fields plus
`source_kind: "file"` and `source_path_sha256`, with no snapshot dependency or
timezone. Note and file shapes are disjoint, including explicit null fields.
Legacy note request hashes are unchanged. Explicit
`discussion_base_file_group_properties` capability negotiation controls creation;
the client caches that capability for offline use and clears it on downgrade.

The server derives values from current authorized managed inventory without
reading a note, attachment or relay document. It rechecks source path, generation,
permissions and grouping definition before committing. Immutable
`file_inventory_v1` witnesses still bind each thread to its exact source; another
readable file with the same value cannot unlock protected history. Existing
threads and exact accepted retries survive renames subject to current access;
new captures with an old path digest are rejected without silently retargeting.

Path and folder values are share-relative on the wire. Native capture verifies
the displayed local path and removes only the known share mount; navigation
reapplies the receiving vault's mount. The share-root folder is an explicit empty
text value on the wire. `file.file` uses managed identity/generation. This works
for every supported file record type, without adding PDF or Canvas content
annotations. All comment metadata remains outside source files.

The [official property reference](https://obsidian.md/help/bases/syntax) distinguishes
file properties from note/formula properties. Local inspection of the installed
1.13.7 app source additionally verifies its name projection: `name` hides only an
exact lowercase `.md` extension; attachment names retain theirs. `basename` and
`fullname` are explicit alternatives. This is source evidence, not native runtime
acceptance. A downloaded 1.15.0 app bundle has the same projection, but its native
controls have not been validated or added to the supported surface-version gate.

Timestamps, sizes, tags, links, embeds, backlinks and property objects still need
their own source/dependency handling. The inventory contains no authoritative
creation/modified times or size; `synctime` must not be substituted. Unsupported
properties are refused rather than becoming empty or whole-view targets.

Validation includes source/type projection, disjoint request shapes, legacy
receipt hashes, exact retry after disablement, source permission intersection,
historical rename/lineage behavior and same-value witness isolation. Actual
PostgreSQL verifies concurrent exact file capture and changes to source path,
generation and access between authoritative reads. The Python/Rust/client fixture
creates an image-file group with no note CRDT and recovers both lost accepted
responses through a reopened outbox. The plugin regression passes 2,141 tests;
native/mobile/version acceptance remains pending. No production flags changed.

## Native evidence and interpretation

A disposable Obsidian **1.13.7** plugin recorded 49 synthetic frontmatter cases
through the actual `getFrontMatterInfo`, `parseYaml`, native property conversion
and the plugin's group-value adapter. The
[recorded fixture](../apps/control-plane/tests/fixtures/native-base-source-1.13.7.json)
includes inputs, native raw values, typed results, host version and capture time
zone. The plugin repository retains an identical copy and a version/path-gated
probe script. No source note or `.base` file changed; pre-probe hashes still match.

The observations changed two server assumptions:

- Frontmatter uses YAML 1.2 core scalar rules. `0123`, `0o17` and `0x1f` are
  numbers; `-0x1f` and `1_000` remain text. A separate bounded metadata resolver
  implements these rules without changing saved `.base` parsing.
- Property lookup prefers exact spelling, then the first lowercase match in
  source order. Unicode casefold is stronger and incorrectly equates `Straße`
  with `STRASSE`; it is no longer used for this lookup.

Native strings may become links, URLs, calendar dates or instants. Tag properties
become a specialized list subclass containing typed tags; the client adapter
now reads that bounded public ListValue interface and preserves tag identity.
`#one` as a tag remains distinct from ordinary text containing `#one`.

## Derivation contract

Inputs include a private `SourceObservation`, exact share/source identity and
generation, the snapshot dependency, grouping property, canonical source path,
capturing host timezone and a server-owned link resolver. The adapter checks
observation identity and expiry both before and after work. It accepts no
client-supplied source bytes or claimed dependency list.

The result contains the typed value, a versioned canonical fingerprint, source
observation/snapshot digests, property/timezone context and the set of managed
records actually referenced. That set always includes the source. Resolved
links add their managed entry/generation, preserve subpaths and ignore display
aliases. Unresolved links retain source identity and a canonical source-location
digest; moving their source cannot silently preserve a different relative link.
This is private derivation output, not a transferable permission receipt.

Scalar group identity excludes source membership and snapshot. Two authorized
records can independently establish the same typed group; changing members does
not require changing the group. Resolved file identities include generation so
replacement cannot silently reuse an old target. The durable registry must also
scope the value to its saved view and grouping-definition identity/revision.

The resolver must use current managed inventory and permissions. It must raise
for unreadable, ambiguous or unverifiable destinations; returning `None` is only
for a verified unresolved reference. Exceptions propagate. It must never make
network calls while the future registry transaction holds SQL locks. Inventory
resolution across mount paths and shares is still part of the command service.

## Types, bounds and open compatibility work

The note adapter handles empty, text, finite numbers, boolean, date, instant,
URL, tag, list, resolved-link and unresolved-link values. Lists retain order and
types; blank, empty, false, zero and singleton values remain distinct. A value
has at most 256 materialized nodes, depth eight and 8,192 UTF-16 text units.
Native output budgets apply after link display-alias removal and tag interpretation.

Calendar dates remain dates. Instants normalize to UTC with millisecond precision;
equivalent explicit offsets share identity. Floating timestamps use the capture
timezone with the host's earlier-fold/forward-gap interpretation. The adapter
also handles day rollover such as February 31 and skipped calendar days. The
timezone is derivation context, not an extra group-identity component.

Remaining cases reject explicitly and retain the need for recovery instead of
being treated as text or empty: YAML aliases, year zero/out-of-range calendar
conversion, unsafe integer literals, object values, nonfinite numbers and
unverified WHATWG URL forms. The URI adapter preserves spelling and proves a
bounded subset; Python's URL parser is not treated as a full WHATWG equivalent.
The fixture records native behavior for deferred cases too. Do not claim full
YAML/date/URL compatibility or silently replace these targets with view comments.

The remaining file-property projections, formula/duration evaluation and cross-file
dependency closure require separate adapters. Group summaries require membership/aggregate
authorization rather than the single-source semantics used here.

## Next integration

1. Complete integrated native controls, changed/unprovable-value recovery and
   desktop/mobile/version acceptance, including different local share mounts.
2. Complete remaining file properties, formula/duration and cross-file dependency
   adapters, then group summaries and Time & Line targets. This checkpoint does
   not reduce that scope.
3. Add provisional first-use offline identities. Existing verified-context
   offline intents already retain the exact evidence/value and obtain server
   authority before sending; repair must never silently replace a witness.

## Earlier typed-value checkpoint verification

The broader Python run passed **240 tests**, including existing registry and
definition behavior. The final source/value run passed **205 tests** after the
additional identity/expiry and URL guards. The native fixture supplies 41 matching
derived values, five native unavailable/error cases and three explicitly deferred
cases (aliases, year zero and unsafe integer literals). Other checks cover source
and share substitution, expiry during resolution, link-reference closure, source
relocation, typed identity, timezone interpretation and bounded work.

The plugin passed **2,033 tests / 191 suites**, preview build/main TypeScript,
changed-source lint, and the existing test-type gate. The existing 15 build
warnings and 206-error/45-signature test-type baseline remain. The reproducible
native probe builds separately and is never part of the release bundle.
No schema, relay runtime or production configuration changed in this increment.
