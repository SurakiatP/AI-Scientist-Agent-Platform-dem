#!/usr/bin/env python3
"""B5 live acceptance runner. Exit 0 PASS, 1 FAIL, 77 NOT RUN.

  uv run python backend/tests/live/run_b5_live.py --evidence-dir .local/live-evidence/<stamp>

Runs preflight, then each gate in order, stopping at the first failure. See README.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

GATES = [
    ("memory", "b5_memory_acceptance.py"),
    ("containment", "b5_containment_acceptance.py"),
    ("bootstrap_final", "b5_worker_bootstrap_final_acceptance.py"),
    ("native_adapter_run", "b5_native_adapter_run.py"),
    ("native_service", "b5_native_service_acceptance.py"),
    ("native_unknown", "b5_native_unknown_acceptance.py"),
    ("parent_verify_unknown", "b5_parent_verify_unknown.py"),
    ("native_committed", "b5_native_committed_acceptance.py"),
]
MATRIX = [
    "b5_matrix_restart.py committed", "b5_matrix_restart.py unknown",
    "b5_matrix_fence_stop.py generation-fence", "b5_matrix_fence_stop.py hung-stop",
    "b5_matrix_fence_stop.py race-completion", "b5_matrix_fence_stop.py race-cancel",
    "b5_matrix_checkpoint_recovery.py compressed", "b5_matrix_checkpoint_recovery.py todo_messages_carry_through",
    "b5_matrix_checkpoint_recovery.py workspace", "b5_matrix_checkpoint_recovery.py todo",
    "b5_matrix_checkpoint_faults.py missing", "b5_matrix_checkpoint_faults.py incompatible",
    "b5_matrix_checkpoint_faults.py corrupt", "b5_matrix_checkpoint_faults.py storage-fault",
    "b5_matrix_checkpoint_faults.py db-commit", "b5_matrix_checkpoint_faults.py upload",
    "b5_matrix_budget.py usage-ceiling-extension", "b5_matrix_budget.py owner-retry",
]
PATH_KEYS = {"private_dir", "server_source_hashes"}


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, **kw)


def preflight(cfg, root: Path) -> None:
    from b5_live_config import digest, not_run

    for tool in ("docker", "colima", "zsh"):
        if not shutil.which(tool):
            not_run(f"{tool} not on PATH")
    docker = ["env", "-u", "DOCKER_HOST", "docker", "--context", cfg.docker_context]
    checks = [
        (["docker", "context", "show"], cfg.docker_context, "docker context"),
        (docker + ["version", "--format", "{{.Server.Version}}"], cfg.engine_version, "engine version"),
        (docker + ["info", "--format", "{{.ID}}"], cfg.expected_engine_id, "engine id"),
    ]
    images = [cfg.worker_image, cfg.server_image, cfg.postgres_image, cfg.minio_image, *cfg.fixture_images.values()]
    checks += [(docker + ["image", "inspect", ref, "--format", "{{.Id}}"], digest(ref), f"image {ref.split('@')[0]}")
               for ref in images]
    for cmd, want, label in checks:
        try:
            out = run(cmd)
        except (OSError, subprocess.TimeoutExpired) as exc:
            not_run(f"{label}: {type(exc).__name__}")
        if out.returncode != 0 or out.stdout.strip() != want:
            not_run(f"{label} mismatch")
    for url in (cfg.database_url, cfg.s3_endpoint):
        parsed = urlparse(url)
        try:
            socket.create_connection((parsed.hostname, parsed.port), timeout=3).close()
        except (OSError, TypeError):
            not_run(f"cannot connect to {parsed.hostname}:{parsed.port}")
    dirty = run(["git", "-C", str(root), "status", "--porcelain", "--", "backend", "runtime"])
    if dirty.returncode != 0 or dirty.stdout.strip():
        not_run("backend/ or runtime/ is not committed and clean")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    evidence = args.evidence_dir.resolve()
    if evidence.exists():
        print(json.dumps({"status": "NOT RUN", "reason": "evidence dir already exists"}))
        return 77
    evidence.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.environ["B5_LIVE_EVIDENCE_DIR"] = str(evidence)
    os.environ["B5_LIVE_PYTHON"] = sys.executable
    from b5_live_config import CFG, NOT_RUN  # NOT RUN (77) propagates as SystemExit

    start = datetime.now(timezone.utc).isoformat()
    env = {k: v for k, v in os.environ.items() if k not in ("B5_CHECKPOINT_FAULT_IMAGE", "SCIENTIST_S3_ENDPOINT")}
    env["SCIENTIST_DATABASE_URL"] = CFG.database_url  # preflight-verified values only
    gates = [(name, [sys.executable, str(HERE / script)]) for name, script in GATES]
    gates.append(("matrix", ["zsh", str(HERE / "b5_matrix_run_serial.sh"), *MATRIX]))
    gates.append(("host_http", [sys.executable, str(HERE / "b5_host_http_acceptance.py")]))
    results, status = [], "PASS"
    try:
        preflight(CFG, CFG.root)
    except SystemExit:  # preflight printed its NOT RUN reason; still record the summary
        status, gates = "NOT RUN", []
    for name, cmd in gates:
        with open(evidence / f"{name}.log", "w") as out, open(evidence / f"{name}-stderr.log", "w") as err:
            rc = subprocess.run(cmd, cwd=CFG.root, env=env, stdout=out, stderr=err).returncode
        results.append({"name": name, "rc": rc, "log": f"{name}.log"})
        if rc == 0 and name == "matrix":
            text = (evidence / "matrix.log").read_text()
            if len(re.findall(r"^ACTOR \S+ rc=0\b", text, re.M)) != len(MATRIX) or "SERIAL DONE" not in text:
                results[-1]["rc"], rc = 1, 1
        if rc != 0:
            status = "NOT RUN" if rc == NOT_RUN else "FAIL"
            break
    head = run(["git", "-C", str(CFG.root), "rev-parse", "HEAD"]).stdout.strip()
    config = {k: v for k, v in vars(CFG).items() if isinstance(v, (str, dict)) and k not in PATH_KEYS}
    summary = {"status": status, "git_head": head, "start_utc": start, "end_utc": datetime.now(timezone.utc).isoformat(),
               "config": config, "gates": results}
    (evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"status": status, "summary": "summary.json"}))
    return {"PASS": 0, "FAIL": 1, "NOT RUN": NOT_RUN}[status]


if __name__ == "__main__":
    sys.exit(main())
