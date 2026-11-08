# Formula group presence

November 7, 2026. This opt-in extension adds pointers and viewport navigation for
bounded same-record formula groups and formula lanes inside timeline ranges.
It uses the existing ephemeral location channel; it creates no discussion target,
source witness, registry identity or pointer history.

Enable `COLLABORATION_PRESENCE_FORMULA_GROUPS_ENABLED=true` alongside presence and
the Bases registry. The default is false. It is independent of discussion enable
switches, including formula-group comments. The capability is
`presence_base_targets_v3_preview`. Version-three clients request static input
plans through `GET /v1/collaboration/base-context?…&base_target_version=3`, and
negotiate `base_target_version: 3` on watches/observations. Older context requests
retain their exact response shape. Older observers receive the supported parts of
a state with formula-group positions removed; the roster remains independent.

## Matching and access

The server compiles the saved formula's complete static closure using the same
bounded syntax compiler as formula-group discussions. It returns property names,
not formula text or results. Ambient, cross-file, dynamic or unsupported expressions
are refused even in inactive branches. The selected source must be a readable
Markdown member of the share. The exact active grouping and registry revision
must still match, and every declared linked record must be readable at its current
generation by both the publisher and each observer, including idle revalidation.

Each honest client reads all static inputs from its existing native Bases member,
including inactive branches, and adds their linked-record references to the
bounded group target. An observer independently requires the same readable member,
input dependencies, local typed result and scoped fingerprint before drawing or
navigating. Changing an input invalidates pending/cached matching even when the
result is unchanged. No source download or formula evaluation is introduced on the
pointer path. A fingerprint remains a local matching hint, never a server proof of
membership or permission. This differs deliberately from persistent comments,
which require authoritative source witnesses.

Requiring inactive-branch dependencies is conservative: a constant-looking group
may be unavailable to a participant who cannot read an unused linked input. This
avoids client-side branch evaluation and ambiguous dependency tracking. Unsupported
or over-budget positions disappear; ordinary cell/item/header positions and the
otherwise-readable participant roster continue to work.

## Resource bounds

- Existing 3,072-byte state / 4 KiB ingress and coalescing/rate limits apply.
- The static formula map is at most 65,536 serialized characters; compiler limits
  also apply. A 32-entry LRU caches syntax/input plans, never records or access.
- At most 32 inputs per plan, 64 saved views and 16 KiB of encoded plans. The
  configuration is fetched once only for an opted-in context request, fenced by
  its exact hash/revision. Formula names and inputs retain protocol string bounds.
- Native matching retains the existing eight extra references, 256-value-node,
  8 KiB text, 128-entry cache and two concurrent hashing-job limits. Current source
  and input references are rechecked before asynchronous results can be used.
- One thousand unchanged movement captures in the regression fixture reuse hashes
  and metadata. This is a work-count assertion, not a device latency/battery claim.

## Verification

The focused server suite passes 262 tests, including bounded compiler plans,
capability independence, old-client downgrade, read-only context queries and
revoked-input handling. The disposable actual-service fixture
`tests/integration/managed_stack.py --base-formula-presence --plugin PATH` exercises
real client channels, static plans and local matching over sockets. Revoking access
to a linked input from an inactive branch withdraws precise presence while keeping
the readable Base participant. It leaves saved Base content unchanged.

Native Obsidian 1.13.7 and the Time & Line provider verify Table group movement,
saved-view isolation, Timeline formula-lane display, viewport capture, Jump/Follow,
reload and unsupported-group refusal. See the plugin's
[formula presence evidence](https://github.com/kjgoodrick/evc-team-relay-obsidian/blob/main/docs/base-formula-presence-preview.md)
for exact limits. No schema migration or production deployment is included.
