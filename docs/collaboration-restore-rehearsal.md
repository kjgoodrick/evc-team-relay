# Disposable full-stack cold restore rehearsal

**November 8, 2026: passed.** The actual client completed both restore cycles and
rollback checks in 202.894 seconds. All owned containers, volumes and networks
were removed; the pre-existing container inventory and running/start/finish
state were unchanged. No live vault or production deployment changed.

The run used server/snapshot source `5d0510b13fcca6ba7db9963a62c5c12194f92256`,
control-plane application source `59bc24596c904aaa6a8df116a2be483098fa2ba3`, and
unchanged relay source `db57df6d30889fb9ad7cd5ec440b1d586cf8cc30`. Observed images:

| Role | Image ID |
| --- | --- |
| Control plane | `261667899a1a0720939642017d0c6ca979e6498cb13a475839ae8be28d90bd77` |
| Relay | `cdc5da5aa23e9fb477fdb8569e42731f30c95c3e39b55d147e18451f931113a4` |
| PostgreSQL | `6162f3765f7563b273a879894c5751d41285f922dd75ba0ba508a89e024e43ca` |
| MinIO | `919a89f4c86f1674c129eeb380226b3d05409cfd9f0ac09ecbe007e5313cc973` |

The control plane exited gracefully with status 143 in both captures, with all
required completion messages. Relay and stores exited zero. The capture-copy
phases took 29.496/33.939 seconds and stage-copy/readback phases 27.718/31.667
seconds for this tiny fixture; these are not production recovery-time estimates.
Local evidence is retained at `/private/tmp/evc-full-cold-drill-20261108-g`.

The rehearsal exposed and fixed two actual integration gaps: application logging
had suppressed required server lifecycle messages, and Podman rejected a valid
zero-entry uploads archive. The related logging/shutdown tests and all **83**
snapshot fault tests pass. Earlier failed fixture attempts are retained separately
and are not counted as successful restore evidence.

`apps/control-plane/tests/integration/collaboration_snapshot_stack.py` connects
the real plugin client to actual control-plane, Rust relay, PostgreSQL and MinIO
containers. It exercises the capture/staging tool and offline generation CLI in
one rehearsal. It is an opt-in integration test, not a deployment command.

The test creates its own containers, named volumes, isolated networks, loopback
ports, accounts, signing keys, picture bucket and client IndexedDB databases.
It never adopts or changes an existing service. Runtime resources are removed
in `finally`; the private evidence directory is retained. Run one rehearsal at a
time on an otherwise stable engine: the final check requires the pre-existing
container inventory and running/start/finish state to remain unchanged.

## What is exercised

- A real persisted Yjs document, a selected-text anchor and a deletion-only edit.
- Separate read/comment access and a hidden-note exception.
- A comment whose accepted response is lost, an ordinary reply, a reaction,
  editing history, and an image retained by an older message revision.
- Clean application/relay shutdown, all four physical stores, verified staging
  into empty never-started containers, and offline restore-generation rotation.
- Original CRDT history and anchor resolution, comment bodies/history, exact
  image and thumbnail bytes, receipts and permissions after restoration.
- A persisted original client retaining its offline edits while paused; rejected
  old command deliveries; read-only receipt lookup without replay.
- Current-generation document writes and comment delivery after the first restore.
- A second capture followed by restoration of the **original** backup into a
  third store set. This rehearses rollback: a different generation fences the
  first restore's cached socket grants and pending commands, while post-backup
  server changes are absent from the rolled-back data.

The second capture preserves the pre-rollback state as additional evidence. The
rehearsal never overwrites or reopens the original stores.

## Prerequisites and command

Use the control-plane virtual environment with its test dependencies, the
plugin's installed dependencies and supported Node executable on `PATH`, and
already-built local application images. No images are pulled. Record the image
build source commits and configuration separately; an image tag or the selected
`--source-commit` alone is not build attestation. The host application/migration
and snapshot-tool code must match the selected source commit.

From `apps/control-plane`:

```sh
.venv/bin/python tests/integration/collaboration_snapshot_stack.py \
  --engine podman \
  --postgres-image '<cached PostgreSQL 16 image digest>' \
  --minio-image '<cached MinIO image digest>' \
  --control-plane-image '<locally built control-plane image>' \
  --relay-image '<locally built Rust relay image>' \
  --source-commit '<full server source commit>' \
  --plugin '/absolute/path/to/evc-team-relay-obsidian' \
  --output '/absolute/path/to/new-private-evidence-directory'
```

On this Mac, run Podman outside the process/filesystem sandbox as required by
the repository instructions. The public relay verification-key configuration is
copied into each owned fixture container; the VM does not need host bind mounts.
Synthetic private credentials are supplied through an explicit private env file;
the application image's production secret-reading entrypoint is overridden.

Inspect `client.log`, `capture-1/`, `capture-2/`, both `stage-*` directories,
`rotation-*` plans/results, and `transition-*` records. A successful run writes
`result.json` and confirms cleanup and unchanged pre-existing containers. Failed
attempts retain partial evidence; do not infer success from a completed capture
alone. Raw archives and fixture credentials remain private in the output folder.

## Limits

After verified staging, this fixture removes only its never-started placeholder
containers, preserves their named volumes, and creates replacement runtimes on a
new fixture network. The `transition-*` record maps both container identities and
volume names. Only PostgreSQL starts for generation rotation; the application and
relay start afterward. This explicit fixture transition does not implement a
production admission barrier, network cutover, deployment rollback or scheduled
off-host backups. The public key config and synthetic secrets are reprovisioned,
not recovered from the store archives.

The test uses TypeScript clients and fake IndexedDB, not native Obsidian or a
physical mobile device. Fresh-generation document peers are separately created;
automatic source-cache activation and native-file replacement remain separate
implementation work. This small fixture does not establish production restore
time, large-vault resource bounds, minimum-client compatibility, external S3,
multiple relay nodes, or continuous-backup guarantees.
