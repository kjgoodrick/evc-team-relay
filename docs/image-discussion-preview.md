# Embedded-image discussion protocol preview

November 6, 2026. This is an opt-in extension to
[the discussion preview](discussion-preview.md), not a production rollout or
the complete image feature. Discussion scopes remain managed Markdown entries.
Directly opened image-file scopes and uploaded images in messages are unfinished.

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
inline overlays, image presence, mobile, all-format geometry and resource
acceptance still require implementation and testing.
