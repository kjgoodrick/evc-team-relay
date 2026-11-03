# Obsidian permissions and Pocket ID preview

Status: experimental fork, 2026-11-03. Use a disposable vault for the first pilot.
No paid service is required. Server administration and hosting remain your responsibility.

Use both forks together:

- Server: <https://github.com/kjgoodrick/evc-team-relay>
- Plugin: <https://github.com/kjgoodrick/evc-team-relay-obsidian>
- Upstream bases: server `ec81be7743e775bf70da528f8d5b5c3472f5e0eb`, plugin `99a1f843425414384f5db91fe962650529858688`.

## What is implemented

New private folder shares created by the fork plugin use a server-owned file inventory. Each member receives only authorized paths, IDs, metadata and activity markers. The complete folder Yjs document is never replicated to members. Markdown and Canvas content retain EVC's per-document Yjs collaboration, local persistence, and Rust relay.

The owner can open **Shares → select share → Manage permissions** in the plugin settings or the share-management modal. Add a member first, then assign rules:

| Member | Path relative to share | Include descendants | Access |
| --- | --- | --- | --- |
| Bob | empty | on | Read only |
| Bob | Projects | on | Read and write |
| Bob | Projects/Budget.md | off | Hidden |

Members have no file access by default. The longest matching path wins; an exact rule wins a tie with a recursive rule. A more specific grant can override a broader hidden rule. Owners and server administrators retain full access. Membership/invitation roles do not grant file access in managed shares; the path rules determine access.

The backend checks permissions for inventory, content grants, attachment access, create, delete, rename and move. A folder operation requires access to every affected child. Explicit exceptions follow renamed objects. Conflicting structural updates are refused, with local edits retained. Stable document IDs cannot be replaced or reused after deletion. Case-insensitive path collisions are rejected for desktop/mobile compatibility.

Live document connections pass through an authorization gateway. It rechecks each frame and checks idle sessions every second; membership removal, user disablement, app-session revocation, and write downgrades close the connection. Clients never receive the private relay CWT. A read-only client also encounters the Rust relay's protocol-level write checks. Attachment requests recheck path policy and managed-session validity.

Revocation is not deletion. Already downloaded files may remain on the user's device. Explicit deletion markers are filtered by current permissions; only matching previously tracked identities are candidates for local deletion, retaining EVC's conflict-copy checks. There is no remote erasure guarantee. This is server-enforced sharing, not end-to-end encryption from the server administrator.

## Structural conflict recovery (0.2.1)

A rejected file operation no longer blocks the entire inventory. On HTTP 403/409 the plugin fetches the latest **authorized** inventory, compares stable file identities, and retries independent operations whose server state is unchanged. Already completed operations are acknowledged; a deletion requires an explicit visible tombstone, because a missing entry can instead mean revoked access. Renames and folder/child changes stay together. Divergent changes enter a local, persisted decision queue; unrelated operations continue syncing. The local disputed layout is retained across polls and app restarts.

Open **Shares → select share → Review file conflicts**. This is available to members as well as owners; a notice also links to the queue when new conflicts arrive. Each conflict shows local paths and the currently visible server paths:

- **Use server layout** saves a verified backup, moves affected local originals into the recovery folder, and reconciles the current server layout. Local per-document history is retained, so surviving document identities can still merge their text edits.
- **Keep my local layout** saves a verified backup and retries the operation with fresh server preconditions. It does not override permissions, identity rules, destination collisions, or access to folder children. New server children absent from the offline operation are protected: adopt the server layout and review them before moving/deleting the folder. Deleted identities cannot be resurrected; recover their content as a new file at an allowed path.

If the server changed since the displayed decision, the user must review the updated paths. A failed backup, failed archive, or new local edit retains the conflict. Originals are moved, never permanently deleted by recovery. After a partial move, the completed backup and any moved originals remain; retrying creates another distinct backup. Do not clear IndexedDB or reinstall to dismiss a conflict.

Recovery bundles live at the vault root in **`Relay Recovery (local only)/<timestamp>-<random ID>/`**:

| Entry | Preserved data |
| --- | --- |
| `files/<original vault path>` | Original disk bytes, including binary attachments |
| `documents/<ID>.txt` | Readable Markdown or Canvas editing state |
| `documents/<ID>.unsaved.txt` | A pending editor/disk buffer, when present |
| `documents/<ID>.yjs` | Local CRDT editing history |
| `inventory.yjs`, `recovery.json` | Inventory history and the reviewed decision |
| `originals/<original vault path>` | Local originals moved out of the way by a server-layout decision |

The recovery directory and its descendants are excluded from managed share adoption and live editor binding, including a share of the whole vault. They remain on that device; keep your own backup of them. A `BACKUP-COMPLETE.txt` marker means the initial bundle was fully written and checked. It does **not** mean the later decision or all archive moves succeeded. Recovery does not automatically merge competing binary contents or infer which of two different moves a person intended.

The server also distinguishes a stale delete from a completed delete: if the same identity was moved elsewhere, deleting its old path now returns a conflict. Exact retries of the completed move or delete remain idempotent. Local persistence drains accepted edits before closing, including edits queued immediately before document teardown.

## Pocket ID configuration

Create a **confidential** OIDC client in Pocket ID: this fork's backend holds the secret and exchanges the authorization code. Register exactly:

```text
https://YOUR-CONTROL-PLANE/v1/auth/native/callback
```

Choose the allowed Pocket ID groups/users. Use the issuer exactly as reported by discovery; configure the client ID and secret only on the backend. Pocket ID's [app setup guide](https://pocket-id.org/docs/setup/connect-an-app) explains these fields. The plugin never needs the client secret.

Add these to the private server environment, alongside the normal database, signing-key, object-store, and bootstrap credentials:

```dotenv
CONTROL_PLANE_PUBLIC_URL=https://YOUR-CONTROL-PLANE
MANAGED_RELAY_URL=ws://relay-server:8080
# Must exactly match [server].url in relay.toml, including its scheme:
RELAY_AUDIENCE=https://YOUR-RELAY-HOST
OAUTH_ENABLED=true
OAUTH_PROVIDER_NAME=pocket-id
OAUTH_ISSUER_URL=https://YOUR-POCKET-ID
OAUTH_CLIENT_ID=REPLACE_ME
OAUTH_CLIENT_SECRET=REPLACE_ME
OAUTH_SCOPES=openid profile email
OAUTH_AUTO_REGISTER=true
OAUTH_DEFAULT_ROLE=user
# Required by the existing OAuth configuration validation; generate a random value.
OAUTH_STATE_SECRET=REPLACE_WITH_A_RANDOM_SECRET
BILLING_ENABLED=false
```

The OIDC client must support `client_secret_post`. Discovery supplies the endpoints; no Casdoor-specific endpoint paths are used by native sign-in. HTTPS is required except loopback test servers. The public URL is the control-plane origin, without `/v1`; proxies must forward `/v1/managed/socket/*` WebSocket upgrades to the control plane. Keep the relay's internal management interfaces private. The upstream Caddy configuration already supports WebSocket upgrades; redact token query parameters in any additional proxy/access logs.

Optional trusted administrator-group mapping uses `OAUTH_ADMIN_GROUPS`; request the `groups` scope if using it. Treat this as server-administrator access, not folder membership. Avoid granting that group to ordinary collaborators.

In Obsidian, add the control-plane URL, choose Login, and choose the SSO provider. The system browser handles the passkey login. After the browser confirms success, return to Obsidian manually. A verifier held by the initiating plugin binds the one-time completion. The flow validates discovery issuer, ID-token signature/audience/expiry, nonce, PKCE, and userinfo subject. Application credentials are returned to the plugin, never placed in a browser redirect URL.

Accounts are keyed by provider and subject. An existing local account with the same email is deliberately **not** automatically linked. For initial setup use a different recovery/bootstrap email from the intended SSO account. The preview has no account-linking UI. Existing local-password login remains available. The legacy web-admin OAuth flow is unchanged; use the bootstrap login for web administration. Logging out of Pocket ID alone does not revoke an existing Relay session.

## Build and install

The server's existing [source deployment configuration](../infra/docker-compose.yml) builds its control-plane, migration and relay images from this checkout. Build from the fork; upstream published control-plane images do not include these endpoints or migrations. Run Alembic through revision `202611030002` before starting the fork control plane. Back up the database and relay/object storage together before any upgrade. Do not use a schema downgrade as a rollback plan once managed shares contain data.

The changes are additive: existing shares stay in legacy mode. There is no automatic migration of an existing share to managed mode, and managed shares cannot be switched to public publishing. Legacy agent keys and web publishing are unavailable for managed shares. Create a new private folder share with the matching plugin and pilot copied data there.

In the plugin checkout, with Node 24 and npm:

```sh
npm ci --ignore-scripts
npm run build:preview
```

This generates version `0.2.1` in `dist/`. Copy `main.js`, `manifest.json`, and `styles.css` to a disposable vault's `.obsidian/plugins/team-relay-sync/`. Enable the plugin there. The ID is retained for compatibility, so it replaces the upstream plugin in that vault; the two must not be installed together. Keep automatic upstream updates off for the pilot. New fork installations start with no hosted server presets.

For a Pi deployment in Kyle's services repository, follow the parent `AGENTS.md`: fetch the selected commits directly from GitHub on the Pi, use a clean checkout, keep secrets/runtime data outside it, and record the commit. This implementation has **not** deployed or restarted any production service.

## Verification and reproduction

Local results on 2026-11-03:

- Backend: 179 focused regression tests passed across native OIDC, managed permissions, existing OAuth, relay-token issuance, attachment tokens and refresh sessions.
- Real-stack integration: passed with actual FastAPI, SQLAlchemy and the compiled Rust relay. Covered filtered metadata, concurrent edits, offline merge, persisted client state, relay restart, competing offline moves, independent-operation progress, a backed-up local-layout decision, text convergence after recovery, read-only rejection, live downgrade, and direct-relay bypass rejection.
- OIDC: real HTTP discovery, signed JWT/JWKS verification, PKCE, nonce and userinfo round trip passed against a disposable provider with Pocket ID's documented endpoint layout. This was **not** a sign-in against a real Pocket ID installation.
- Rust: 16 document-connection tests passed; the relay built with the existing lockfile.
- Plugin: 132 suites / 1,367 tests passed (the opt-in real-stack case runs separately). TypeScript, Svelte typechecking, the test-type regression gate, and the production preview bundle build passed. Tests cover persisted conflicts, both decisions, live edits during requests/backup/archive, failed/partial disk operations, new server children, hidden-versus-deleted entries, binary bytes, readable editor buffers, and IndexedDB restart. The test-type gate retains its upstream frozen baseline; it is not a claim that all legacy test files are free of type errors.
- Desktop: the earlier 0.2.0 preview in Obsidian 1.13.7 loaded the plugin in a disposable vault, signed into the disposable backend, loaded/saved permission rules, and connected a managed folder. A separate Yjs client received the exact text entered in Obsidian; its reply appeared live in the editor. The 0.2.1 conflict dialog has automated DOM interaction coverage; a new native desktop check was blocked by the locked Mac and remains pending.
- PostgreSQL: both new migrations render successfully as PostgreSQL SQL. Runtime tests used SQLite; PostgreSQL contention and migration execution still require a deployment rehearsal.
- `npm audit --omit=dev`: zero reported runtime advisories at this check. The inherited development dependency graph still has advisories; the preview does not claim a complete dependency/security audit.

Reproduce the backend tests:

```sh
cd apps/control-plane
uv sync --locked --extra test --extra dev
uv run pytest tests/test_managed_shares.py tests/test_native_oauth.py tests/test_native_oidc_roundtrip.py tests/test_oauth.py tests/test_relay_token_endpoint.py tests/test_file_token.py tests/test_refresh_token.py -q
```

Build `apps/relay-server` with `cargo build --locked -p relay`. With the plugin checkout installed alongside the server checkout:

```sh
cd apps/control-plane
uv run python tests/integration/managed_stack.py --plugin ../../../evc-team-relay-obsidian
```

The harness uses temporary databases, keys, files and loopback ports, then cleans up its processes. `--serve-seconds 900` holds a disposable stack open for desktop testing and prints the temporary test login. Never expose this fixture server externally. In the plugin checkout, `npm test -- --runInBand` runs the regular suite; the real-stack test is skipped unless the harness supplies its fixture.

## Remaining pilot gates

This is a working preview, not yet a production reliability claim:

1. Test the actual Pocket ID passkey/browser round trip on macOS, iOS and Android, including cancellation, app suspension, timeout and session refresh. Killing the plugin during sign-in requires starting a new flow.
2. Test real mobile file creation, background/foreground recovery, offline app termination, rename/delete conflicts, and attachments/Canvas. Automated coverage is strongest for Markdown and the authorization boundary; mobile read-only UX is not yet qualified.
3. Rehearse the migrations and concurrent structural operations on PostgreSQL. Test realistic vault sizes and memory/CPU on the chosen host. Inventory is polled at one-second intervals; document edits use live WebSockets. The gateway currently makes database checks per frame and on an idle timer.
4. Exercise the new conflict dialog in a disposable Obsidian vault on desktop and mobile, including real filesystem rename failures, open editors, and app termination during an archive. Automated recovery and real-relay tests pass, but these native device checks remain pilot gates.
5. Validate recovery from backups before putting valuable data in managed shares. Do not mix this pilot with another tool writing the same shared folder.

The fork preserves the upstream Apache-2.0 server and MIT plugin licensing and notices.
