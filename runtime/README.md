# Worker image

The generated context preserves the selected catalog's pinned `LICENSE.md`.
The image installs it at
`/opt/scientist/notices/scientific-agent-skills-LICENSE.md`, separately from the
manifest-verified catalog resources. Keep this notice when redistributing the
image or preparing a derived build.

Prepare an empty context from the pinned native and scientific-catalog Git
archives, rather than copying a working checkout. Python 3.13 or newer and RTK
are required. The script validates the native archive digest and every selected
catalog resource, rejects links/special files and records every source hash.
It never overwrites an existing destination.

```sh
rtk proxy uv run python runtime/prepare_build.py \
  --hermes-source .local/vendor/hermes \
  --catalog-source .local/vendor/scientific-agent-skills \
  --output .local/worker-build-context
rtk proxy docker --context colima-scientist-platform-test build --pull=false \
  -t scientist-runtime-candidate:test .local/worker-build-context
```

The Dockerfile pins the reviewed dependency prerequisite by digest. That image
must already exist on the owned engine; it contains the hash-locked dependencies
in `requirements.lock` and Python 3.14.7. Its reproducible provisioning and the
production storage/network composition belong to the deployment tranche.
Do not substitute an unreviewed base or build against the shared Docker context.
See [dependency evidence](../docs/security/runtime-dependencies.md).

The final image must receive its own scan and SPDX inventory. The supervisor
supplies isolated tmpfs mounts, limits, bootstrap, firewall and readiness gate;
running this image directly does not establish containment. Native gateway
endpoints are never started. B5 acceptance remains required before dependent
workflow/protocol features are enabled.

## Trusted environment preparation

`profiles/worker-base.json` describes the existing Python 3.14.7 resource-measurement
environment. It pins the runtime/catalog revisions, the existing dependency lock
and the exact full-catalog manifest bytes. All 177 instruction sets are inert
files; their presence does not enable their tools or certify useful results.
CPU and document profiles need separately reviewed locks, images and fixtures.

Research Setup requests record owner/project-bound preparation jobs. Reads do
not build, capture credentials or install packages. The host configures
`profile_preparation.configure_builder(callback, evidence_key=existing_host_key)`;
private controllers configure only `configure_evidence_key(existing_host_key)`.
The domain-separated HMAC authenticates accepted evidence stored in PostgreSQL.
Never expose either configuration function or the key through an API or worker.

The callback receives a predefined `Profile` and durable job UUID. It must enforce
the declared limits and verify actual source/image hashes, compatibility checks,
dated Trivy coverage with zero HIGH/CRITICAL, SPDX inventory, approved licenses
and containment before returning the strict `BuildProof` summary. A matching
immutable prebuilt image may be rechecked; identical builds need not run again.
Checks/scans must be fresh and the scanner database unexpired. Evidence-file
hashes retain the full underlying reports for independent review. Import success
alone cannot establish readiness or scientific validation.

The host serially calls `process_next_job(db)`. Its committed claim precedes
physical work. A callback may use `record_stage(db, job_id, stage)` for bounded
progress; it cannot set ready through that helper. `BuildFailure` records a
known failed preparation; uncertain exceptions remain unknown. Started or
unknown jobs block a second launch until exact reconciliation and owner action.
No configured callback/key means blocked preparation, never a pretend ready state.

For a new environment, the operator uses the existing context and owned engine:

```sh
rtk proxy uv run python tools/skills/build_manifest.py \
  --catalog-source .local/vendor/scientific-agent-skills --check
rtk proxy uv run python runtime/prepare_build.py \
  --hermes-source .local/vendor/hermes \
  --catalog-source .local/vendor/scientific-agent-skills \
  --output .local/x0-worker-build-context
rtk proxy docker --context colima-scientist-platform-test build --pull=false \
  --network=none -t scientist-runtime-x0:candidate .local/x0-worker-build-context
rtk proxy docker --context colima-scientist-platform-test image inspect \
  scientist-runtime-x0:candidate --format '{{.Id}}'
```

Verify the owned engine identity first. The context must be new; use its
`source-hashes.json` for exact source accounting. Run the established dated
Trivy/Syft procedures on the returned immutable image ID, serialize scans sharing
a cache, and run compatibility/containment through the existing supervisor.
Neither this command example nor synthetic module tests constitute accepted
image evidence. The application must use the exact independently reviewed proof;
never invent a proof or substitute an unreviewed base when digest resolution fails.
