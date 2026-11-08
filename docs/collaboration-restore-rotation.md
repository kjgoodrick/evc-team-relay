# Offline restore-generation rotation

After [verified restore staging](collaboration-snapshots.md), the maintenance
command can give all restored shares a new lineage and reset their discussion
sync cursors in one database transaction. Existing queued commands keep their
old lineage and are rejected by the server's restore guards. Original comments,
permissions, receipt identities, thread IDs, document generations, event history
and sequence numbers remain intact.

This is an administrative operation on an **isolated restored PostgreSQL copy**.
It is not an HTTP endpoint and never runs at application startup. It neither
starts services nor makes a restored deployment ready for clients. Safe client
source activation and the complete application/rollback rehearsal remain open.
Do not use it to reset a live service's cursors or to repair a single note.

## Preconditions and scope

Keep clients, relay processes, control-plane workers and scheduled writers
disconnected. The database may be started only within the isolated maintenance
environment. Provision its credentials and reviewed configuration separately.
The CLI does not alter networks, discover default credentials or launch a server.

Migration `202611080001` adds `collaboration_restores`, a durable operation
receipt table independent of ordinary audit-log retention. The database's Alembic
heads must exactly match the maintenance build. An older staged backup needs its
reviewed offline schema migration before rotation planning. The tool does not
perform that migration automatically or infer that arbitrary version jumps are
safe. Captured source/image provenance still comes from the snapshot/deployment
records; staging hashes are references to those records, not attestation of
currently running code or a SQL instance's physical origin.

The credential file must be a private regular file, containing an explicit
`postgresql+psycopg://…` URL. No `DATABASE_URL` fallback is used. The administrative
role needs normal table update/lock privileges and visibility into every database
session (`pg_read_all_stats` membership or superuser). The CLI refuses other
connected client sessions and unresolved prepared transactions before rotation,
and checks for connected clients again before commit. These are additional
checks; only deployment isolation prevents new clients from arriving. A table
lock does not establish a network admission barrier.

## Prepare and inspect a plan

Run from the control-plane environment that contains the selected build:

```sh
python -m app.maintenance.collaboration_restore \
  --database-url-file /run/secrets/restore-database-url \
  plan --staging-evidence /secure/restore-evidence \
  --operator 'Operator or maintenance-run identifier' \
  --output /secure/rotation-plan.json
```

Planning is read-only. It validates the source-manifest/target-plan hashes in the
staging receipt, requires distinct source/target identities, reads the complete
share inventory and generates a new version-4 UUID. The private plan records
schema heads, the old restore generations, discussion cursor epochs and sequence
numbers, along with the staged backup/target references and operator label.
Review this concrete plan before applying it to any non-disposable environment.
The bounded initial implementation supports up to 10,000 shares per plan.

## Apply exactly that plan

```sh
python -m app.maintenance.collaboration_restore \
  --database-url-file /run/secrets/restore-database-url \
  apply --staging-evidence /secure/restore-evidence \
  --plan /secure/rotation-plan.json --output /secure/rotation-result.json
```

The CLI reserves a new private result file before changing the database.
PostgreSQL table locks serialize rotation and prevent new-share phantoms while
the transaction runs. The current share inventory must match the reviewed plan
exactly. A competing plan, changed generation/cursor/sequence, new/deleted share,
schema mismatch or existing unreceipted generation rejects the operation.

Every share receives the plan's new restore generation, including direct-document
shares and previously restored shares. Each discussion cursor receives a distinct,
deterministic UUID under that generation. Sequence counters are preserved. The
same transaction writes per-share administrative audit entries and one durable
receipt containing the exact plan fingerprint and previous-inventory digest.
Failure rolls back the entire transition. Lock waits are limited to five seconds
and SQL statements to 30 seconds; failure is reported without raw driver values
or credentials.

No server cache is refreshed and no socket is disconnected by this command: the
application and relay must already be stopped. The clients' existing restore
guards, receipt inspection and local-work preservation remain responsible for
handling their old queues after a separately validated activation.

## Lost results and retries

```sh
python -m app.maintenance.collaboration_restore \
  --database-url-file /run/secrets/restore-database-url \
  receipt --plan /secure/rotation-plan.json
```

Receipt inspection is read-only and does not require exclusive access. It reports
`recorded` only for an internally consistent receipt matching the exact plan;
`unknown` is not proof that a prior process cannot still commit. It does not claim
the generation remains current after a later restore.

If the process or local result write failed after SQL commit, retry the **same
plan** with a new result filename while the database remains isolated. A matching
receipt makes this read-only and reports `already-applied`. If a later restore
superseded it, the retry reports `superseded` and never reinstates the older
generation or rotates newly added shares. Altered plan/provenance fields or an
inconsistent receipt are rejected. Do not generate a second plan merely because
the first result file was lost.

Once this migration has recorded a restore, its downgrade refuses to delete the
receipt table. The earlier generation-column migration separately refuses to
remove an activated guard. A real rollback must preserve these protections or
restore another isolated complete snapshot and establish a fresh generation;
it must not simply erase guard metadata.

## Evidence and remaining work

The unit/transaction suite exercises original and previously restored shares,
empty inventories, stale plans, rollback, strict plan/evidence parsing,
idempotence, inconsistent receipts and late retries. An application test preserves
an existing discussion and accepted receipt across actual maintenance rotation,
rejects old create/withdraw requests and accepts a new generation-bound comment.

`tests/integration/collaboration_restore_admin_postgres.py` creates its own
PostgreSQL fixture. It checks fresh migrations and unused-receipt downgrade/
upgrade, actual CLI planning/apply/receipt, connected-client and insufficient
session-visibility rejection, lost-result retry, a real competing table-lock wait,
late superseded retry and populated-receipt downgrade refusal. Its staging metadata
is synthetic; this test is not the full physical snapshot/application drill.

November 8 verification passed **2,752 control-plane tests** (18 skipped) and the
actual PostgreSQL migration/CLI/concurrency drill. The final application shutdown
marker and v1/v2 staging-format refinements were checked afterward with **56
focused tests** and **15 shutdown/evidence tests**, including actual snapshot-tool
metadata interoperability. Full control-plane lint/format checks and snapshot
script checks pass. No live database or deployment was changed.

Still required: integrate these steps under one verified maintenance lifecycle,
rehearse restoration of real application data and CRDT anchors with the private
objects, confirm obsolete clients across that full stack, complete client source
activation/reconciliation and exercise production-equivalent rollback. Nothing
in a successful rotation receipt authorizes opening the restored service to users.
