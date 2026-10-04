# B5 live acceptance suite

Actual-container acceptance for the B5 worker/supervisor runtime. It is NOT part of `uv run pytest`
(`conftest.py` ignores this directory). Any missing prerequisite gives NOT RUN (exit 77), never PASS.

## Prerequisites

- macOS with zsh, git and uv; `uv sync` done.
- Colima profile running and the docker context set as current, with the configured engine version and engine ID.
- Every configured image already loaded with its exact ID. Building images is out of scope; `fixtures/*/Dockerfile`
  and `sitecustomize.py` are build provenance only, the suite never builds anything.
- Isolated `scientist-b5-postgres` / `scientist-b5-minio` services reachable at the configured loopback ports.
  They are created only by the owner-approved `b5_persistent_services.py`; the host port forward is an owner prerequisite.
- The private dir (mode 0700, containing only the 5 named files `broker_capability_key`, `database_url`, `master_key`,
  `s3_access_key`, `s3_secret_key`; the suite checks names only) and the repo must both be under a path Colima
  shares, because both are bind-mounted.
- The server source-hash file named in the config.
- `backend/` and `runtime/` committed and clean (the suite lives under `backend/`, so it must be committed too).
- The evidence dir and the private dir must be outside the repo or under `.local/` (the loader enforces this).
- The server source-hash file comes from the owner-held server build context of the configured server image
  (not tracked); without it the run is NOT RUN.

## Run

```
mkdir -p .local
cp backend/tests/live/b5_live.example.json .local/b5-live.json   # then set expected_engine_id and any real values
uv run python backend/tests/live/run_b5_live.py --evidence-dir .local/live-evidence/$(date -u +%Y%m%dT%H%M%SZ)
```

Config path: `$B5_LIVE_CONFIG`, default `.local/b5-live.json` (git-ignored). `b5_live.example.json` is the only key list.
Exit codes: 0 PASS, 1 FAIL, 77 NOT RUN. The run writes logs and `summary.json` into the evidence dir.
Running a single script directly is diagnostic only, never acceptance. Unit tests stay `uv run pytest`.
Per-run dispatch template and launch files are written under private_dir and removed by exact-label cleanup.

## Provenance (original under ignored `.local/` -> here, sha256 of the original)

| Original | New | sha256 |
|---|---|---|
| `b5_matrix_run_serial.sh` | `b5_matrix_run_serial.sh` | `d58a36eda57e0a7f2cba0f130dd5af3a4389c514c040570c674e66ad333bc246` |
| `b5_matrix_common.py` | `b5_matrix_common.py` | `ed5b7e32178fc00a9f29426eb0ac6825519d34f8520cd32fca2892530ef3814b` |
| `b5_matrix_budget.py` | `b5_matrix_budget.py` | `d0197323529df74b3dc8781995da611090c6359722aafdf6903cb77244a1c476` |
| `b5_matrix_fence_stop.py` | `b5_matrix_fence_stop.py` | `1656387b14b684ba684c5a058fa0e9a34ee2065583d3d2e4becd6a6f21cb4a2a` |
| `b5_matrix_restart.py` | `b5_matrix_restart.py` | `47e7f47c87a8a59e5efefe7b6b627914b3a48871d45c629cae461c6ef873b046` |
| `b5_matrix_checkpoint_faults.py` | `b5_matrix_checkpoint_faults.py` | `1e138d8c592bf608a03ba06f97cd226d57ff5e708af45639b3307937306152bf` |
| `b5_matrix_checkpoint_recovery.py` | `b5_matrix_checkpoint_recovery.py` | `37ce956b05cf7e5e509cae09e0e0526a56da45905f679cfe5ba28b5c547f16ac` |
| `b5_supervisor_matrix.py` | `b5_supervisor_matrix.py` | `0fa56c35bb38074386045364511ca6fbe21aa4695ca295a700beeebf878c9464` |
| `b5_native_fault_checks.py` | `b5_native_fault_checks.py` | `a7dc6b64820a4fea5eaae92d79709d456c99024a1e0c110bcacb16509858fe26` |
| `b5_persistent_services.py` | `b5_persistent_services.py` | `44bc4e6516d7003de6bd26b3691645ff4941cb334b15c595c435a0c6666a576e` |
| `b5_containment_acceptance.py` | `b5_containment_acceptance.py` | `d2f449ca2cddba128ea2edf60b62092c21d71bdb0d6231d8f8c845bc6283ff12` |
| `b5_memory_acceptance.py` | `b5_memory_acceptance.py` | `73e783a07a53b6d3881970321f3061be75db4cab53247bb1d40bfc1765815ed7` |
| `b5_native_adapter_run.py` | `b5_native_adapter_run.py` | `7fdd27c7dbe6c7c0a60c352a4c40d8c30c946fc21a5607a87bcb404fed00726b` |
| `b5_native_adapter_acceptance.py` | `b5_native_adapter_acceptance.py` | `c5f427a1f0ab5fab761545a01fa079ececa29b22569bd52e4a339b2180f8308f` |
| `b5_native_service_acceptance.py` | `b5_native_service_acceptance.py` | `8c05b668a5d63afc4409e6957b62948e9fe8cd9c289d6d34d86176fb0daa1b83` |
| `b5_native_unknown_acceptance.py` | `b5_native_unknown_acceptance.py` | `859625c862b79bc791d84e45dd018470695db44c16d0f761bdd018f37a3b9d2f` |
| `b5_native_committed_acceptance.py` | `b5_native_committed_acceptance.py` | `df452ffc553f3097f33dfa8df2b4d33c74ad27ce23c2443b850844136f6d8608` |
| `b5_parent_verify_unknown.py` | `b5_parent_verify_unknown.py` | `582262c5e49e33f07b05191abd4c6b272f5e26bf64655bbc1b985480d929f648` |
| `security/b5-worker-bootstrap-final-acceptance.py` | `b5_worker_bootstrap_final_acceptance.py` | `6a098be8788b0d2ae6626262d7e2c39a1be786b2888bd73e6d90244585a2b22f` |
| `b5-fixture-context/` | `fixtures/happy/` | Dockerfile sha256 `95e577283c72b6f8b98e295523bc769db8a0913fa80472d1a073bbbb7a7c6d74`; sitecustomize.py sha256 `e9e5158753e70ec56fd35492abdd94d86538cd891764ac5aacbecc81fd214a5e` |
| `b5-fault-fixture-context/` | `fixtures/counter/` | Dockerfile sha256 `95e577283c72b6f8b98e295523bc769db8a0913fa80472d1a073bbbb7a7c6d74`; sitecustomize.py sha256 `edfbcfe6a3050c4f9d5d667007417098492e7d5b20880017787cca9d7e3ae3e8` |
| `b5-after-result-fixture-context/` | `fixtures/barrier/` | Dockerfile sha256 `6642650a722ed193a8fbd8e35dc6db60f5db6972434a3278d8ea3ceff4f50f7c`; sitecustomize.py sha256 `f142d1973a6257ca5b665a7e4c73e9bf322cc5ed1e7d5c6bccc1f0bbe3c0ca0a` |
| `b5-checkpoint-fault-fixture-context/` | `fixtures/checkpoint_fault/` | Dockerfile sha256 `95e577283c72b6f8b98e295523bc769db8a0913fa80472d1a073bbbb7a7c6d74`; sitecustomize.py sha256 `1269fd7f9e586ba34a25bb1e15d358f4fe468218f8ac5600e21b5a15fa734d43` |
