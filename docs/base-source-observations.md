# Private Bases source observations

Status: November 6, 2026. The relay source observer and private control-plane
client are implemented. **This does not enable group comments or establish
group-value authorization.** No public source endpoint or new advertised client
capability has been added. The existing private barrier remains disabled by
default; no deployment configuration changed.

## Purpose and authorization boundary

A client may propose a Bases group target, but it cannot prove which records
supplied a value by sending their paths, values or a dependency list. The next
stage needs values derived from server-observed inputs. Group identity remains
separate from its changing member list.

This helper supplies bounded text from an existing synchronized Markdown note.
It does not evaluate formulas, inspect views, authorize a user, create a durable
group witness, or return source text to a public caller. Source text stays in the
private server hop; the Python observation excludes it from its representation.

The future command service must authorize base and source access before I/O,
copy primitive identities and release SQL, perform the private call, then obtain
fresh share/session/entry guards and recheck permissions, lineage, definition
revision and observation expiry before consuming it. ORM lazy reads must not
reopen a transaction during the network call. Formula inputs and aggregate
membership still need server-derived dependency closure and authorization.

## Wire contract

`POST /internal/collaboration/source` shares the
[private barrier's](anchor-barrier-protocol.md) authentication, admission,
per-document lock, existing-document requirement, generation registration,
durable checkpoint and shutdown ownership. Document tokens cannot use it.

Send protocol version, request ID, document ID, share ID, generation and expiry,
plus `source_dependency`: a canonical padded base64 Yjs v1 snapshot, at most
64 KiB decoded. No anchor batch is allowed. Registration and text validation
reject a source dependency. Counts, truncated encodings, trailing bytes and
range overflows reject before loading a document or allocating decoded counts.

The observer verifies state-vector **and delete-set** inclusion. It reads current
source and the observed snapshot in one transaction, then waits for the ordered
durable checkpoint. Success contains the common response bindings and:

- `source_prefix`: at most 64 KiB UTF-8, ending at a Unicode boundary.
- `source_complete`: whether that prefix is the entire visible source.
- `source_prefix_sha256`: digest of the exact returned UTF-8 bytes.
- `observed_snapshot_sha256`: digest of the observed snapshot encoding.

This observes the current state containing the dependency, not a historical
reconstruction or frozen document. A future target command must compare its
proposed value with the derived value and retain the draft if they disagree.
Missing dependencies return 409; foreign generations/shares reject; checkpoint
failure or expiry returns no successful source observation. Responses use
`Cache-Control: no-store`.

## Resource bounds and remaining limits

The private client admits two source observations at once, disables redirects
and environment proxies, and shares a 12-second monotonic deadline across
registration and observation. It checks exact request digest/nonce, all identity
fields, checkpoint time, wall-clock expiry, source digest and UTF-8 byte bounds.
The source response stream is limited to 512 KiB to accommodate JSON escaping;
the existing anchor/registration response limit remains 64 KiB.

The pinned Yrs API cannot iterate only a visible text prefix. Before obtaining a
string, the observer caps visible document length at **2 Mi UTF-16 units**;
larger documents return 413 `source_document_too_large` (mapped privately to
`base_value_source_too_large`). Materialization can use roughly 6 MiB for text
before prefix truncation. This bounds visible-string allocation, not historical
CRDT fragmentation or total loaded-document memory. Performance on long-lived
documents and a bounded library prefix API remain release work. Do not advertise
this ceiling as completed large-note support or silently treat it as no metadata.

The Python metadata helper recognizes the host's opening/closing `---` fence
shape and never treats a truncated prefix boundary as EOF. It rejects malformed,
aliased, non-JSON, overly deep or oversized metadata, unsupported tags and
unsupported numbers instead of silently dropping them. Limits are 16 levels and
4,096 nodes. A complete note with no closing fence has no frontmatter, matching
the inspected host fence behavior. Native observations subsequently established
YAML 1.2 core numbers (including leading-zero decimal, octal and hex), exact-name
then lowercase property lookup, and typed note-property conversion. See the
[typed value adapter and remaining compatibility limits](base-group-values.md).
This is still a bounded subset and does not itself mint a group-value proof.

## Verification and next integration

Rust tests cover inserted and delete-only dependencies; UTF-8 truncation and the
document ceiling; malformed snapshot allocation counts; server-only authority;
mixed protocols; generation/share refusal; failed durable writes; and filesystem
eviction/reload. Existing text barrier cancellation/shutdown checks still run.

Python tests cover identity/expiry binding, response digests, canonical evidence,
bounded admission and release, JSON escape expansion, stream limits, private
failure mapping and safe metadata parsing. The opt-in actual HTTP fixture runs
the existing plugin text-comment proof, then uses the production Python source
client against its restarted Rust relay and refuses a foreign generation:

```sh
python tests/integration/managed_stack.py --plugin /path/to/plugin --base-sources
```

Checkpoint validation: **63 new Python tests**, **151 existing discussion/Bases
tests**, **4 core Rust source tests** and **12 relay barrier tests** passed.
The actual Python/Rust/Yjs fixture passed after correcting an older test's exact
capability expectation for already-existing Bases fields. It also verifies that
a document credential cannot use the source route. Relay build, changed Python
lint/format and whitespace checks passed. These are local automated/service
checks; no native/mobile, PostgreSQL migration or performance acceptance is
claimed for this increment. No schema migration or plugin runtime change was
needed.

Still required: the remaining native metadata cases; durable value identity and
verified input records; read/write/history/picture/receipt/mention authorization;
group wire anchors and client negotiation; cached offline capture and explicit
stale-value recovery; native sidebar and marker integration; group summaries and
formula dependencies. No group feature acceptance is claimed by these checks.
