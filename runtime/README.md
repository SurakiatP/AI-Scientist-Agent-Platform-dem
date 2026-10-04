# Worker image

The generated context preserves the selected catalog's pinned `LICENSE.md`.
The image installs it at
`/opt/scientist/notices/scientific-agent-skills-LICENSE.md`, separately from the
67 manifest-verified skill resources. Keep this notice when redistributing the
image or preparing a derived build.

Prepare an empty context from the pinned native and scientific-catalog Git
archives, rather than copying a working checkout. Python 3.13 or newer and RTK
are required. The script validates the native archive digest and all 67 selected
skill resources, rejects links/special files and records every source hash.
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
