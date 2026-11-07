# Typed Bases group values from verified source

Status: November 6, 2026. `base_group_values.derive_note_value` derives typed
values and their source references from a private source observation. A gated
capture endpoint, durable group authorization and client protocol now have
PostgreSQL and actual Python/Rust/Yjs integration evidence. Native group capture,
sidebar/marker integration and new offline capture intents remain unfinished.
Group comments are **not ready for native testing**.

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
prove that a historical group is currently rendered. Formula/file-property
groups and aggregate dependency closure remain unfinished.

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
reopen. This does not yet implement capturing a previously unverified group while
offline. Cache version 13 preserves earlier work and fences older protocol workers.

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

File-property projection, formula/duration evaluation and cross-file dependency
closure remain separate adapters. Group summaries require membership/aggregate
authorization rather than the single-source semantics used here.

## Next integration

1. Wire native capture, explicit mixed-type choices,
   sidebar, markers and offline drafts/outbox. Changed/unprovable values need
   actionable retained-draft recovery. Existing group identity survives members
   leaving and reappearing.
2. Add durable capture intents for groups first selected while offline; preserve
   the exact dependency/value and obtain server evidence before sending a thread.
   Never silently replace a thread's selected witness during repair/reattachment.
3. Complete the deferred value adapters and native/mobile acceptance, then group
   summaries and Time & Line targets. This checkpoint does not reduce that scope.

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
