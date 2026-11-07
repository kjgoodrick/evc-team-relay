# Combined permission editor API preview

The server and TypeScript client now support one conditional transaction for
file and comment rules. This is the foundation for the settings permission
editor and its read-only reviewer preset. **The interface is not connected yet**;
this checkpoint does not make permission management a finished user feature.

The independent file and comment authorization models remain unchanged. A
reviewer needs file `read` plus comment `allowed: true`; this does not issue a
note-write or attachment-write grant. Writers may comment by default, and an
explicit comment denial can restrict that default. Comment rules never overcome
missing file read access, inactive accounts or lost share membership. Owner and
server-administrator authority still supersedes ordinary member rules.

## Contract

Both endpoints require the authenticated owner or server administrator of a
managed share and the existing `COLLABORATION_POLICY_ENABLED` gate. Capability
discovery advertises `features.permission_editor_v1` when this API is available.

- `GET /v1/collaboration/shares/{share_id}/permissions?actor_id={account_id}`
  returns version 1, share/actor/owner IDs, an opaque `revision`, the independent
  `comment_revision`, canonical `file_rules` and `comment_rules`, and current
  member display/eligibility hints.
- `PUT /v1/collaboration/shares/{share_id}/permissions` accepts version 1,
  `actor_id`, `expected_revision`, and both complete rule lists. It returns the
  same snapshot shape after saving. Both lists are limited to 1,000 rules.

The actor ID must match the authenticated account, even if a replacement token
belongs to another privileged account. UUIDs and exact canonical paths remain
the identities; email labels are display information. Root rules require
`recursive: true`. Duplicate keys and malformed paths are rejected before any
write. A rule key is `(user_id, path, recursive)` independently in each list.

New or modified rules require a current, active non-admin member. Owners/admins
cannot be restricted by ordinary rules. Existing rules for departed or inactive
members can be preserved unchanged or removed, allowing cleanup without silently
discarding other stored policies. They cannot be expanded through this endpoint.

## Concurrency and authorization

Reads hold a shared share-row lock while reading both rule sets. The revision is
a SHA-256 fingerprint of the share/owner identity, existing authorization epoch,
independent comment revision and both canonical rule lists. Saves hold the same
share's write lock, refresh ORM state and compare that fingerprint before writing.
This detects changes made through legacy file/comment endpoints and inventory
renames without adding a database column or changing those endpoints' contracts.

A successful change commits file rules, comment rules, authorization-epoch
invalidation and one administrative audit event together. Comment revisions
advance only when their rule set changes. Reordered/unchanged rule lists are a
no-op. Failure rolls back both lists, counters, epoch and audit. Current target
membership rows are held through commit for new/modified rules; removal can
complete before or after that grant, and always removes effective access.

The legacy unversioned file PUT remains available for older clients. It can
still replace file rules as an independent administrative operation; a combined
editor cannot silently overwrite its intervening result with an old snapshot.
Legacy comment PUTs remain separate and cannot erase file permissions.

Stale saves return HTTP 409 `permission_policy_changed`; newly ineligible target
members return 409 `permission_member_changed`. Account mismatches return 403
`permission_editor_account_changed`. These are messages inside the existing
`error` envelope, rather than raw exception diagnostics.

## Client boundary and pending interface

`PermissionPolicyClient` and the share client's `permissionEditor()` factory bind
an operation to a server, share, account and caller-provided lifecycle predicate.
They check that predicate before/after credential retrieval and after network
reads, validate the echoed identities and bounded response, and serialize the
proposed rules before awaiting credentials. Requests are marked sensitive,
have a 15-second deadline, abort supported transports and never retry themselves.

An interrupted save is unconfirmed, not a claim that the server rolled it back.
The caller must read the current policy and compare it with the retained
proposal. Matching rules establish that the desired state is present; they do
not identify which administrator wrote it. A differing state needs explicit
review before another conditional save. Timeout/account-change guards prevent
late credential resolution from dispatching a new write.

Remaining interface work includes presets and independent overrides, member/path
selection, inherited/effective access explanations, handling more-specific
exceptions, proposed-change review, conflict reconciliation, unsaved-state and
lost-response handling, owner/admin entry points, and native desktop/mobile
acceptance. The UI must preserve unrelated rules and must not describe a folder
reviewer preset as removing a more-specific write exception unless that exception
is explicitly included in the proposed changes.

## Verification

The new server tests cover HTTP authorization and account switches, reviewer
grants, explicit comment denial, hidden files, feature gates, legacy invalidation,
invalid/oversized rules, departed/inactive members, no-op saves and transaction
rollback. The related discussion/policy/Bases/timeline regression passes 374
tests. Existing test-environment/deprecation warnings remain.

The disposable PostgreSQL proof checks competing saves yielding one 200 and one
409, consistent reads against a real legacy writer, stale-save rejection after
that writer commits, rollback after both lists and audit are flushed, and target
membership protection through commit. The fixture uses a dedicated empty
loopback database in a disposable rootless container; it stops and removes that
container afterward and does not restart existing services.

The actual HTTP fixture exercises the real TypeScript client, grants a reviewer
read/comment without edit, creates a comment with that grant, loses a successful
save response, reconciles the current rules, rejects stale saves and rejects a
different account's token. The existing outbox/history scenarios also pass.
Client regression/build/type results and remaining release work are in the
[project status](../../../docs/Plans/obsidian-presence-comments-status.md).

No database migration, production deployment or live-vault installation is part
of this checkpoint. The UI and full release remain unfinished.
