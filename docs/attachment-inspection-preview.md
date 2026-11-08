# Read-only attachment inspection grants

`POST /shares/{share}/file-token` (also mounted under `/v1`) accepts the optional
strict boolean `read_only`. Omission or false preserves the existing grant
contract. True adds `read_only: true` to the response and signed file token;
upload-URL issuance and direct PUT content reject it with 403 for every role.
This does not grant general API access or change the user's permissions.

For managed shares the read grant captures the generation observed under the
inventory lock. Its signed `restore_guard: true` and explicit nullable
`restore_generation` distinguish the original generation from an unbound legacy
token. All managed reads reject a generation change and recheck current access
and login session. Storage I/O remains outside the lock; content is reauthorized
afterward. A headerless inspection request can observe the current generation,
but the client must compare it with its captured source before downloading.
Normal upload grants continue to retain the request's original generation.

The client requires explicit read-only support and never reuses normal cached
sync grants for inspection. This additive endpoint is available independently of
the unfinished native restore installation workflow; it does not activate a
restored cache or restart a service. See the plugin's
`docs/attachment-inspection-preview.md` for its transport limitations.

Managed/legacy grant tests and the actual client/HTTP/MinIO fixture cover allowed
reads, denied owner uploads, generation boundaries and unchanged ordinary sync.
