# Formula-based Bases groups

November 7, 2026: a gated preview supports a bounded subset of formulas used to
group a saved Bases view. The same verified group can anchor a discussion, a
group-summary definition discussion, or a Time & Line lane/range. Comments stay
outside the source note and `.base` file. This is not general authorization to
read computed cells, aggregates or other files through a formula.

## Authority and compatibility

Enable `COLLABORATION_BASES_FORMULA_GROUPS_ENABLED` only after upgrading the
control plane and participating clients. It defaults off and also requires the
existing Bases registry, discussion, definition-target and group gates plus the
private source credential. The negotiated capability is
`discussion_base_formula_groups`. No production flags or services were changed.

The target catalog optionally includes `formula_group_ids`: eligible formula
group definitions on that catalog page. Eligibility means the reachable syntax
can be compiled; actual values, input types and permissions are verified during
capture. Existing target rows are unchanged and `computed_values` remains false.
Clients aggregate eligible IDs across pages and reject inconsistent discovery.

Capture reuses the note-source dependency/timezone request. The server compiles
the current synchronized Base's formula closure, observes the exact managed
Markdown snapshot through the private relay service, then rechecks current
definition, source lineage/path and authorization after that unlocked I/O. The
server derives a typed value and compares it with the proposed native value.
Client expressions, source bytes and claimed dependencies supply no authority.

Immutable `formula_note_v1` witnesses retain the source and every consulted
managed link reference, including inputs used to select a branch. Each thread
continues to authorize its exact witness for reads, replies, mutations, history,
pictures, mentions and receipts. Another readable record with the same calculated
value cannot unlock protected evidence. This also applies inside a group summary
or timeline range. Disabling creation preserves existing discussions and exact
accepted retries, subject to current access and lineage checks.

## Supported expression subset

The explicit lexer/parser and evaluator execute no JavaScript, Python `eval`,
plugin functions or filesystem calls. They support:

- Static note properties (`status`, `note.status`, `note["status name"]`), static
  transitive `formula.name` references, and inventory-backed `file.name`,
  `file.basename`, `file.fullname` and `file.ext`.
- Decimal number literals, strings, booleans, null, parentheses, unary `!`/`-`,
  arithmetic `+ - * / %`, comparisons and boolean `&&`/`||`.
- `if`, `min`, `max`, `number`; primitive `isTruthy`, `isEmpty`, `isType`, numeric
  `abs`/`ceil`/`floor`, and text `lower`, `contains`, `containsAll`, `containsAny`,
  `startsWith` and `endsWith`, where the operand types are supported.
- Direct passthrough of existing typed note-property values, preserving managed
  link identities. Rich-value transformations remain unavailable.

For example, `double = order * 2` and
`bucket = if(status == "Ready", formula.double + 1, 0)` produce the native numeric
group `5.6` for order `2.3`. Text `"5.6"` is a different group.

Unsupported syntax fails explicitly even in an unselected branch. This includes
dynamic property/file access, cross-file evaluation, ambient variables,
clock/random functions, regex, HTML/images and unknown plugin functions.
`file.path` and `file.folder` are excluded here because arbitrary calculations
on vault-local mount prefixes are not portable. Their direct inventory group
adapter remains available. Timestamps/size/dependency-bearing metadata,
duration/date arithmetic, scientific literal syntax, unary plus, fractional
number-to-text concatenation and unverified coercions also remain unavailable.

Native semantics are not assumed to be JavaScript semantics. Notable verified
rules: boolean operators return booleans; `number()` parses a numeric prefix;
null arithmetic stays null except text concatenation; `contains("")` is false;
boolean `.isEmpty()` is an error. Nonfinite/overflow outputs fail. String numeric
coercion uses an explicitly bounded whitespace/decimal subset. Unicode lower-case
samples match the tested host; exhaustive cross-version Unicode parity is not
claimed. Capture always requires an exact match to the native typed value.

## Work and storage limits

Each expression has at most 8,192 characters, 256 nodes and nesting depth 24.
A reachable graph has at most 64 formulas and 2,048 nodes; evaluation has a
4,096-step budget and bounded recursion. Derived text is at most 8,192 UTF-16
units and the witness has at most 256 managed references. Cycles are refused.
Repeated formulas and properties are memoized for one calculation. Frontmatter
is parsed once per immutable source observation, never cached across accounts
or snapshots; binding and expiry checks still surround evaluation. No formula
work runs for presence frames or scrolling.

The client retains exact native targets and source snapshots in the durable
offline queue. It obtains the authoritative witness before delivery and recovers
lost capture/send responses by receipt. Cache version 25 preserves existing
stores and fences older writers that cannot retain formula capture intents.
The capability is cached for offline use and cleared on downgrade. Unsupported
groups cannot silently become whole-view drafts, including command-palette focus
restoration in native Obsidian.

## Verification

The committed [native fixture](../apps/control-plane/tests/fixtures/native-base-formula-1.13.7.json)
records **131 formula observations** from a core-only disposable Obsidian 1.13.7
vault, including explicitly deferred/error cases. Its tests compare typed native
results or require explicit unavailability. The official
[syntax](https://obsidian.md/help/bases/syntax) and
[functions](https://obsidian.md/help/bases/functions) references guided discovery;
runtime observations determine the implemented subset.

- **212** final parser/evaluator/source/formula-API tests pass after native parity
  corrections. The preceding broader source/Bases/discussion run passed **656**
  tests. Formula authorization checks include exact-value/type mismatch, disabled
  retries, link dependencies, source revocation and definition changes during I/O.
- Actual Python/Rust/Yjs/client tests pass for both table and Time & Line formula
  groupings, including delete-only dependency waiting, lost capture/send responses,
  local cache reopening, retained read-only source, summaries/ranges, source
  access and relay restart.
- The disposable PostgreSQL suite passes with formula groups: concurrent exact
  captures, same-value witness isolation, revocation/lineage/definition changes
  during unlocked observation, range checks and migration guards. Its container
  was stopped and removed.
- The complete plugin run passes **3,301 tests / 251 suites**. Build/main types
  and the frozen test-type gate pass; changed-source lint has no errors (three
  existing sentence-case warnings in the native pane).
- Native desktop table and Time & Line numeric-lane creation, exact markers,
  saved-view isolation, full reload, table target navigation, supported keyboard
  capture and unsupported-formula refusal pass. Independent owner/reviewer API
  reads agree on both discussions; source note and Base hashes are unchanged.
  The disposable vault is closed and its services stopped.

Remaining work includes the unsupported formula families above, general computed
result/aggregate authorization, formula-group presence, wider native group-summary
and lane-range acceptance, different mounts/host versions, accessibility and
mobile/resource acceptance. This checkpoint does not complete those release gates.
