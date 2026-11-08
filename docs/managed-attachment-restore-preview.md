# Managed attachment revisions and restore fencing

This development checkpoint protects managed attachment bytes and their scoped
credentials. It does not enable production restores or complete the coordinated
backup/restore workflow. Ordinary comment pictures use their separate private
picture protocol; this contract concerns source images and other synced binaries.

## Storage and compatibility

New managed uploads use
`managed-assets/v1/{share UUID}/{entry UUID}/{lowercase SHA-256}` in the existing
asset bucket. The server verifies the exact scoped byte length and SHA-256 before
writing. Streaming request reads stop at the scoped length or 25 MiB limit.
Two revisions have separate keys; a delayed upload of the same digest can only
write identical verified bytes. No SQL pointer migration is needed because the
managed inventory already records the digest.

Managed HEAD, download-URL and content routes prefer the hash key. A missing
hash key may fall back to `web-assets/{share}/{entry}`, but the mutable legacy
object must pass a bounded read and checksum before availability or bytes are
returned. Storage failures and corrupt hash-key bytes never fall back. The Bases
configuration and saved-view readers use the same key selection while retaining
their smaller byte limits and existing error contracts. Unmanaged web assets keep
their original route and storage behavior.

This deliberately retains old revisions. Garbage collection must account for
current inventory, retained historical references and backup manifests before
removing them; this checkpoint has no automatic revision purge. A coherent backup
must capture both asset key layouts as well as the private comment-picture bucket,
database and complete CRDT state. Old binaries and privileged storage writers are
outside this new writer's checksum guarantee and still need compatibility gates.

## Credential and transaction boundaries

Managed file-token responses advertise `restore_guard: true` and the current
nullable `restore_generation`. The mint request's optional
`X-Relay-Restore-Generation` is copied into the credential, never replaced with
the latest generation. Missing binding belongs to the original lineage. Read
minting can remain available after restore without a binding, but such a token
cannot upload to the restored share. A provided stale binding is rejected.

Minting refreshes the locked share and current user/session. Upload-URL requests
and actual PUTs independently recheck the token, file access and original restore
binding. PUT rechecks before body intake, after body/hash validation, and after
object storage. SQL locks do not span request body or MinIO I/O; blocking checks
and storage writes run off the async event loop. A restore during object storage
can leave an unreferenced verified revision, but cannot overwrite the restored
revision and cannot return a successful stale acceptance.

Object reads reauthorize after I/O and reject a restore that overtook the read.
Guarded Bases bytes already stored in SQL retain their existing atomic read path.
Session revocation, permission changes and a restore remain distinct checks.

## Client behavior and costs

The client captures file/share/provider/account and managed-inventory context
before asynchronous work. Upload bytes and their hash come from one read. It checks
the final PUT response and acknowledges only that captured hash/mtime; it cannot
mistake a later local cache hash for uploaded content. Downloads check HTTP status
and checksum before returning bytes. Local stat/context checks before replacement
retain files changed while downloading, but do not supply an atomic filesystem
compare-and-swap or a verified pre-write byte backup.

File credentials use a bounded 256-entry memory cache, separate from persisted
document grants and their refresh queue. Keys distinguish server/provider session,
file identity, digest, MIME type, length and restore binding. Identical concurrent
requests coalesce. Session/account changes, cache clearing and teardown fence late
completions. Routine JWT refresh in the same session retains its namespace.

New-object HEAD requests need only a storage stat. Legacy HEAD/download-URL
verification reads and hashes the legacy object, so it costs more I/O until a
verified hash-addressed revision exists. Upload hashing uses the existing captured
read; downloads add one bounded client checksum. The short database checks add
round trips but avoid holding the share lock over potentially slow image transfer.

## Evidence and remaining work

- `tests/test_managed_file_token.py`: actual routes, scoped checksum/length,
  original/restored credentials, read-only callers, request-body and storage races,
  session/permission revocation, legacy reads and restored read rejection.
- `tests/test_managed_assets.py`: bounded reads, distinct keys, cleanup and
  missing-only fallback.
- `tests/integration/managed_attachment_client.py`: real TypeScript BlobClient,
  credential cache/provider and inventory transport over loopback HTTP with real
  MinIO. Verifies two readable revisions, legacy reads, stale/wrong-byte rejection
  and the exact final object set. Uses only disposable resources.
- `tests/integration/managed_attachment_postgres.py`: actual row-lock waiting
  across restore; body/storage race checks prove the lock is released, stale
  success is rejected and restored bytes remain intact.

Native/mobile acceptance, source byte recovery, original-work reconciliation,
safe resume, document writer/socket fencing, older-client exclusion, coordinated
capture/restore and rollback rehearsal remain required. No production generation
is rotated by this code.
