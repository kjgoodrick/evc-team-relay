# Image discussion protocol preview

November 6, 2026. This is an opt-in extension to
[the discussion preview](discussion-preview.md), not a production rollout or
the complete image feature. Embedded-image scopes belong to managed Markdown
entries. A separate preview now supports managed image-file scopes through the
API, client session/outbox and native desktop image-file controls.
Uploaded pictures in messages also remain unfinished.

## Capability and compatibility

`COLLABORATION_IMAGE_ANCHORS_ENABLED=false` is the default. Enabling it also
requires the existing discussion/policy settings and working text-anchor/private
relay configuration. The capability `discussion_image_occurrences` and `image`
target are advertised only when the image and text-anchor checks are enabled.
Upgrade all participating clients before enabling image creation: previous
clients reject the new anchor union member in thread reads. Turning creation
off does not remove existing anchors or make older clients compatible.

The JSON-backed anchor table needs no migration. A disabled image flag rejects
new image targets with `409 image_anchors_disabled` before private network I/O.
Previously accepted identical retries still return their receipt after current
note authorization, and current clients can read existing anchors.

## Anchor

An image anchor has these fields:

| Field | Value |
| --- | --- |
| `version`, `type` | `1`, `"image"` |
| `occurrence` | Existing text-anchor format, bound to the enclosing Markdown entry/generation; nonempty range, no block structure. |
| `asset` (managed) | `{kind: "managed", entry_id: UUID, sha256: lowercase_hex_64}` |
| `asset` (external) | `{kind: "external", url: HTTP(S)_URL, version_evidence: "unknown"}` |
| `basis` | Positive integer `width`/`height` up to 100,000, `orientation: "display"`. |
| `geometry` | `{type: "whole"}`, `{type: "pin", x, y}`, or `{type: "region", x, y, width, height}`. |

Coordinates are finite numbers in [0,1]. Region dimensions must be positive and
fit inside the image. Booleans and numeric strings are not coordinates. External
URLs are limited to 2,048 characters and reject credentials, controls, ambiguous
slashes/backslashes, invalid ports and surrogate codepoints. No server-side URL
fetch, proxy, remote image hashing or content attestation is performed.

Image commands share the 256 KiB text-anchor envelope budget, including the
64 KiB decoded dependency bound. Whole-note commands retain the 64 KiB budget.
The private relay only receives the existing dependency and relative source
range, not URLs, image geometry or asset hashes. Its proof binds the complete
public command hash, preventing substitution between validation and commit.

## Authorization and evidence

The author needs comment permission on the note. A managed image must also be
a live `type: image` entry in the same share, with current read/write permission.
Both checks run before private relay I/O and again under the final share lock.
Inventory and permission writers use the same lock. No lock spans network I/O.
Unavailable, hidden, foreign-share and non-image assets return
`404 image_unavailable`; no path or content is exposed.

The observed hash may refer to an older image version when queued offline. It
is retained rather than silently replaced by today's inventory hash. The client
verifies newly captured local bytes and suppresses geometry when the current
asset differs. Historical hashes remain client evidence, not proof that the
server owns or can restore historical image bytes. Download authorization is
independent of anchor visibility; a thread reference never grants asset access.

## Validation and limits

Unit/API checks cover geometry/URL rejection, private relay payloads, receipts,
the independent flag, read-only commenters, old-hash preservation and revocation
between validation and commit. The real Python/Rust/plugin fixture covers three
geometries, read-back/retries, second-occurrence movement after shared insertion,
and invalid asset rejection:

```sh
python tests/integration/managed_stack.py --image-anchors --plugin <plugin-checkout>
```

This uses disposable SQLite/services and does not establish PostgreSQL lock
acceptance or attachment transfer. The native desktop fixture manually seeded
synthetic local image bytes/inventory. Upload/paste, private picture storage,
direct placement on native overlays, image presence, mobile, all-format geometry and resource
acceptance still require implementation and testing.

## Managed image-file scopes

`COLLABORATION_IMAGE_FILES_ENABLED=false` independently gates new direct-file
targets. With discussion/policy previews enabled, it advertises
`discussion_image_files` and the `image_file` target. Text-anchor configuration
and private relay barriers are not required. Upgrade participating clients before
enabling this union member; creation disable does not remove existing discussions.

An `image_file` anchor contains `version: 1`, `generation`, a managed `asset`,
`basis` and `geometry`. It has no Markdown occurrence or Yjs dependency. The
asset entry ID must equal the command's managed entry ID. External URLs cannot
be independent file scopes. The existing `anchor-context` endpoint allocates and
returns the server-owned generation with read permission. Creation requires that
generation and independent comment permission on the live image entry. The
stored scope kind is `image`; no database migration is required.

All geometry/basis/hash constraints are shared with embedded images. Older
client-observed hashes remain intact for offline work. These hashes are evidence,
not server attestation of historical bytes. Direct-file commands retain the
64 KiB envelope limit. Neither image URLs nor image content are fetched by the
server. Accepted retries, reads and replies remain available after the creation
flag is disabled, subject to current authorization. New creation returns
`409 image_files_disabled`; wrong file/anchor kinds return
`422 invalid_anchor_scope`; outdated lineage returns
`409 anchor_generation_mismatch`. Existing note and image scopes cannot be
reinterpreted after an inventory type change; explicit migration/recovery is
separate work.

The client parses these targets, binds reads and queued commands to the requested
file, persists the capability and generation, and queues them through the existing
account-isolated outbox. `DiscussionSession.watch(entryId, listener, "image")`
selects file context independently of note/text capability. The native client
now supplies image-file commands, target drafts, sidebar discussions, preview/
reveal and verified direct-file overlays. Creation uses a placement dialog;
direct manipulation, the full geometry/format matrix and mobile acceptance
remain unfinished. Desktop evidence includes owner and comment-only reviewer
creation, draft reload and original file-byte preservation.

API coverage includes whole/pin/region round trips, stable lineage, old-version
preservation, flag disable, retries, reader/commenter separation, permission,
session and deletion revocation, and kind/asset mismatches. The actual HTTP
fixture queues three image-file targets offline, replays them as a comment-only
reviewer, verifies receipts and read-back, and leaves note discussions empty:

```sh
python tests/integration/managed_stack.py --image-files --plugin <plugin-checkout>
```

This fixture uses disposable services and SQLite. It does not test native image
views, actual image-byte downloads, PostgreSQL concurrency or mobile behavior.
