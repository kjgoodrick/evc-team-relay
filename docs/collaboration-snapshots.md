# Cold collaboration snapshots: capture and isolated staging

`scripts/collaboration_snapshot.py` provides a maintenance capture and a restore
staging tool for the single-host storage layout below. It stops explicitly
selected containers, captures all four stores after shutdown, verifies the
archives and writes a completion seal. Restore staging copies a verified set into
new, empty, network-isolated containers and reads every restored store back.

This is not yet the complete collaboration restore workflow. A separate
[offline generation-rotation command](collaboration-restore-rotation.md) now has
transaction and PostgreSQL CLI evidence. Its integration with this maintenance
lifecycle, application-level restore/rollback rehearsal, minimum-client enforcement and safe
client source activation remain required before a restored service admits users.
The tool never starts services, changes networks, overwrites existing stores,
removes containers or force-kills a process. Both successful capture and failed
capture leave services stopped where shutdown reached them. An operator must
review the result before any normal-service restart.

## Supported storage contract

The plan explicitly declares `single-postgres-minio-local-relay-v1`:

| Role label | Exactly one writable persistent mount | Captured data |
| --- | --- | --- |
| `postgres` | `/var/lib/postgresql/data` | Entire PostgreSQL cluster, including discussions, permissions, receipts, registries, restore generations and schema history. |
| `objects` | `/data` | Entire single-node MinIO store, including private comment pictures, `managed-assets/v1/`, legacy `web-assets/` and any relay checkpoints in that store. |
| `relay` | `/app/data` | Entire local relay store, including full document checkpoints if configured there. Include this mount even when object storage holds the checkpoints. |
| `control-plane` | `/app/app/static/uploads` | Local control-plane uploads. |

Optional `worker` and `ingress` containers join the same set and must have no
writable persistent mounts. Include every application process, standalone
retention/inbox/notification/webhook worker and scheduled writer that can affect
these stores. Extra persistent mounts, overlapping stores, and mounts shared with
any outside container are rejected, including read-only outside mounts. Read-only
configuration/secret mounts and transient container filesystems are not copied.

This contract assumes all durable collaboration state is in these four mounts.
External S3 services, additional relay nodes, distributed MinIO, external
PostgreSQL tablespaces/WAL directories, custom durable paths, external encryption
keys and host processes writing the volumes require a different coordinated
adapter. They are not made safe by adding labels. Symlinked/hardlinked storage and
special files are rejected. Provision credentials and any external encryption
material separately; raw stores themselves remain sensitive backup data.

The maintenance operator must also prevent orchestration, manual restarts and
new writers while the tool runs. Membership, configuration, starts and shared
mounts are rechecked throughout, but a local CLI cannot prevent another
administrator from creating an unlabelled remote writer or changing storage
directly. This is a maintenance workflow, not a distributed admission service.

## Explicit selection

All participating containers must be created with two labels:

```text
io.evc.snapshot.set=<one canonical UUID for this complete stack>
io.evc.snapshot.role=<role from the table, worker, or ingress>
```

Deploying these labels to an existing service requires its normal reviewed
deployment. This tool does not label, recreate or discover an arbitrary live
stack by its name. Current unlabelled deployments are rejected. Source code and
deployment files must still follow the repository's GitHub checkout rules.

With the intended stack running, prepare a read-only plan:

```sh
python scripts/collaboration_snapshot.py --engine docker plan \
  --set '<stack UUID>' --source-commit '<selected 40-character Git commit>' \
  --output /secure/backup-plan.json
```

The plan records full container IDs, observed immutable image IDs and configuration
fingerprints. It does not export raw environment variables, commands, secrets or
engine diagnostics. The source commit is an operator declaration, not build
provenance attested by the image. Deployment records must establish that mapping.
The plan and subsequent manifest identify the selected code/images; PostgreSQL's
schema history and per-share restore generations remain inside its captured data.

Review the plan and storage contract before live maintenance. Disposable fixture
operations require no additional user confirmation. Live interruptions retain
the deployment's separate authorization requirements.

## Capture

```sh
python scripts/collaboration_snapshot.py --engine docker capture \
  --plan /secure/backup-plan.json --output /secure/backup-unique-name
```

The output directory must not exist. It is created with mode `0700`; archive and
metadata files use `0600`. The sequence is:

1. Recheck the exact plan and complete labelled set before the first stop.
2. Stop ingress, standalone workers, control plane, relay, PostgreSQL, then MinIO.
3. Require no OOM/error or intervening restart. Storage, relay, workers and ingress
   must exit with status zero. The control plane also accepts Uvicorn's normal
   signal exits (130/143), but only with its final application-hook marker,
   successful lifespan shutdown and finished-server messages from this start.
   Missing/unsupported/failed lifespan completion is rejected even after a zero
   exit. Require the relay's final-checkpoint completion and normal shutdown
   messages too. A failed relay never produces a completed snapshot, even if the
   engine stop command succeeded.
4. Export each stopped store through the container engine. Check shutdown evidence
   and storage isolation before and after each export.
5. Verify tar structure, full archive SHA-256, and an order-independent file-tree
   digest covering content, numeric ownership and permissions. Recheck every
   container after final verification, then durably write `complete.json`.

Both [Docker's stop timeout](https://docs.docker.com/reference/cli/docker/container/stop/)
and [Podman's stop time](https://docs.podman.io/en/latest/markdown/podman-stop.1.html)
support `-1` for indefinite graceful waiting. The tool uses that setting. Its own
`--timeout` bounds an engine CLI call (default 300 seconds); expiry fails the
capture and terminates only that CLI, without requesting a container kill. The
engine may still be draining the container. Inspect it before taking further
action. No failed operation automatically restarts or cleans up services.

The stores are copied only after all selected processes have exited. Taking the
database and object store offline also closes their public/presigned write paths.
Incomplete uploads can remain as unreachable storage data; committed references
and retained objects are captured together. This does not repair inconsistencies
already present before maintenance or establish external-store durability.

An incomplete output directory retains its plan, successful stop receipts and any
partial archives. It has no completion seal. Start a new capture with a new output
directory after investigating; do not manufacture a seal or treat successful
individual copies as a coherent snapshot. Store only completed snapshots under
the separately configured off-host encryption/retention policy.

## Offline verification

```sh
python scripts/collaboration_snapshot.py verify /secure/backup-unique-name
```

This command needs no container engine. It rejects missing/unexpected files,
symlinked archives, malformed or duplicate metadata, altered archives, incomplete
shutdown evidence, unsafe tar paths, links/special files, duplicate members,
sparse members, hidden trailing archives and truncated tar termination.

The completion seal is a checksum relationship, **not an authenticity signature**.
It proves neither that an untrusted party generated a truthful backup nor that
application semantics survived restore. Keep trusted backup provenance and access
controls. Verification does not decompress or extract arbitrary paths into the
host filesystem. Content is streamed in bounded chunks; path inventory memory
scales with entry count, with a one-million-entry limit per store. Capture and
staging deliberately perform full verification/readback passes; downtime and
temporary disk usage scale with stored bytes.

New captures use `evc-cold-snapshot-v2`, which binds each complete stop receipt,
including its observed exit status and timestamps, into the manifest checksum.
The reader still verifies and stages v1 archives under their original, narrower
shutdown-evidence contract; it does not retroactively claim v2 process evidence.
An old prepared capture plan must be regenerated before any services are stopped.
Staging always writes the current-format result, including when its input is v1.

## Isolated restore staging

Create a separate set of containers and empty volumes from the recorded image
identities, using a new set UUID, the same role counts and `--network=none`.
Containers must never have been started. Supply reviewed runtime configuration
and secrets separately. Source containers and stores must remain untouched.

```sh
python scripts/collaboration_snapshot.py --engine docker plan-stage \
  --snapshot /secure/backup-unique-name --set '<new target UUID>' \
  --output /secure/restore-plan.json

python scripts/collaboration_snapshot.py --engine docker stage \
  --snapshot /secure/backup-unique-name --plan /secure/restore-plan.json \
  --output /secure/restore-evidence-unique-name
```

Staging verifies the entire source snapshot, exact image identities and target
isolation, then proves **all** target mounts empty before the first copy. It
preserves archive ownership/permissions, re-exports each destination and compares
the file-tree digest. A changed target, content, owner or mode prevents the staged
receipt. Source verification repeats before completion. Neither source services
nor target services are started. The evidence directory retains empty probes,
readback archives and per-store verification, so plan its capacity accordingly.

On failure, the partial **new** target stores remain offline for inspection. A
retry refuses those nonempty stores; use a fresh target set after investigation.
The original service and backup remain available for rollback. `staged.json`
explicitly says that generation rotation and application rehearsal are still
required. It is not permission to attach production networks or start clients.

## Verification evidence and remaining release work

The fault suite covers stale plans, outside mounts, failed/forced/OOM shutdown,
missing relay completion, restarts during copying and final verification,
corruption/traversal/truncation, private metadata, non-forcing timeouts and staged
restore ownership/content/identity checks:

```sh
apps/control-plane/.venv/bin/pytest -q scripts/test_collaboration_snapshot.py
```

`scripts/test_collaboration_snapshot_engine.py` creates and removes only its own
labelled containers/volumes. Its synthetic processes write their final bytes on
TERM; successful capture/staging and failed-relay rejection exercise the actual
engine. Optional explicit cached PostgreSQL and MinIO images add reopening of
physical stores, byte-exact cross-store references and new writes after staging.
All fixture service connections remain inside network-isolated containers.
Existing container identities and start states are checked unchanged afterward.

November 8 verification passed **80 fault tests** and the actual Podman success,
failed-relay and populated-target cases. The final-format fixture combined real
PostgreSQL 16, MinIO and Uvicorn running a synthetic ASGI application. It verified
graceful exit status 143, reopened the staged physical stores, compared four binary
objects against database references and wrote new data. A failing lifespan hook
was rejected without a completion seal, including Uvicorn's automatic unsupported-
lifespan handling. Capture took about 30 seconds and staged copy/readback about
28 seconds for this tiny fixture; those are local engine and verification overhead
measurements, not production capacity or RTO promises.
All disposable fixtures were removed and pre-existing container IDs, running
states and start timestamps were unchanged. Changed-script lint and format checks
pass. The relay and application logic in this engine fixture remain synthetic;
do not conflate that check with full application acceptance. The real Uvicorn run
is server-runtime evidence, separate from the real control-plane hook tests and
the still-required integrated drill.

These checks do not yet prove application-level anchor/ACL/receipt preservation,
restored epoch rotation, obsolete-client rejection across a complete restored
stack, client reconciliation, a production rollback or a daily off-host schedule.
The real relay shutdown proof remains separately covered by
`scripts/test_relay_shutdown.py`; synthetic relay completion in the engine test
must not be described as real relay acceptance. Complete that integrated drill
before claiming the feature is ready for user testing or reopening restored data.
