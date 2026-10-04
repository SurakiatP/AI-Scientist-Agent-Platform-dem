#!/usr/bin/env python3
"""Read-back B5 supervisor/recovery acceptance checks; this file runs only from the live suite.

The script never builds images, starts Docker services or runs actors. Actors (backend/tests/live/b5_matrix_*.py) produce
sanitized fixture proofs; this script independently verifies durable PostgreSQL/MinIO state against them.
Per ADR-011 supervisor restart is reconcile-and-replace (no adopt-live path): `supervisor-reload` consumes the
restart actor's proof and checks the run was completed by a replacement with preserved identity, usage and
checkpoint. `--server-container` is optional and only used to cross-check the pinned server image when supplied
(`capture-baseline`, the legacy live-worker snapshot, still requires it).

  rtk proxy uv run python backend/tests/live/b5_supervisor_matrix.py CASE \
    --run-id RUN_UUID --baseline <evidence-dir>/b5-matrix-CASE-baseline.json \
    --fixture-proof <evidence-dir>/b5-matrix-CASE.json \
    --server-image-digest sha256:... --fixture-image-digest sha256:...

Fixture proof contract is deliberately small and contains assertions only:
  {"schema_version":1,"case":"...","status":"PASS",
   "worker_image_digest":"sha256:...","server_image_digest":"sha256:...",
   "fixture_image_digest":"sha256:...","proof":{...}}
The accepted keys for ``proof`` are enumerated per case below. Do not include
tokens, request bodies, provider responses, object keys, or exception text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent))
from b5_live_config import CFG  # noqa: E402

ROOT = CFG.root
PRIVATE = CFG.private_dir
WORKER_IMAGE = CFG.worker_image_id
DEFAULT_DB_URL = CFG.database_url
DEFAULT_BUCKET = "scientist-b5"
DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
CONTAINER_RE = re.compile(r"^[a-f0-9]{64}$")

CASES = (
    "capture-baseline",
    "supervisor-reload",
    "generation-fence",
    "hung-stop",
    "race-completion",
    "race-cancel",
    "checkpoint-recovery",
    "checkpoint-missing",
    "checkpoint-incompatible",
    "checkpoint-corrupt",
    "checkpoint-storage-fault",
    "usage-ceiling-extension",
    "checkpoint-commit-fault",
)

PROOF_KEYS: dict[str, set[str]] = {
    "generation-fence": {"stale_capability_denied", "late_effect_count", "old_generation"},
    "hung-stop": {"stop_completed", "late_effect_count", "operation_state"},
    "race-completion": {"winner", "completion_event_count", "late_effect_count"},
    "race-cancel": {"winner", "completion_event_count", "late_effect_count"},
    "supervisor-reload": {"services_restarted", "fresh_process_recovery", "same_run_revision", "same_checkpoint_id", "provider_attempts", "usage_reservation_unchanged", "exact_cleanup"},
    "checkpoint-recovery": {"continuation_used_committed_results", "original_prompt_replayed", "restored_workspace_verified", "context_class", "seeded_context_hash_matched", "attempts_unchanged"},
    "checkpoint-commit-fault": {"fault_kind", "checkpoint_revision", "checkpoint_revision_unchanged", "previous_checkpoint_verified", "orphan_object_delta", "replacement_started", "run_actionable"},
    "checkpoint-missing": {"checkpoint_rejected", "replacement_started", "reason"},
    "checkpoint-incompatible": {"checkpoint_rejected", "replacement_started", "reason"},
    "checkpoint-corrupt": {"checkpoint_rejected", "replacement_started", "workspace_preserved", "reason"},
    "checkpoint-storage-fault": {"checkpoint_rejected", "replacement_started", "reason", "reason_fixture_attested"},
    "usage-ceiling-extension": {"token_ceiling_enforced", "elapsed_ceiling_enforced", "active_elapsed_before_ms", "active_elapsed_after_ms", "elapsed_time_ledger_persisted", "elapsed_extension_event_count", "extension_decision_id", "extension_applied_once"},
}


class AcceptanceError(RuntimeError):
    """Safe failure message suitable for local acceptance output."""


def _safe_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise AcceptanceError(f"{label} is not an immutable sha256 digest")
    return value


def _json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"cannot read {path.name}: {type(exc).__name__}") from None
    if not isinstance(value, dict):
        raise AcceptanceError(f"{path.name} must contain a JSON object")
    return value


def _db_url() -> str:
    # Never print the URL; it may contain credentials in operator environments.
    return os.environ.get("SCIENTIST_DATABASE_URL", DEFAULT_DB_URL)


def _s3_client():
    try:
        import boto3
    except ImportError:
        raise AcceptanceError("boto3 is unavailable in the selected Python environment") from None
    try:
        access = (PRIVATE / "s3_access_key").read_text(encoding="utf-8").strip()
        secret = (PRIVATE / "s3_secret_key").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AcceptanceError(f"private MinIO credentials unavailable: {type(exc).__name__}") from None
    if not access or not secret:
        raise AcceptanceError("private MinIO credentials are empty")
    return boto3.client(
        "s3", endpoint_url=os.environ.get("SCIENTIST_S3_ENDPOINT", CFG.s3_endpoint),
        region_name="us-east-1", aws_access_key_id=access, aws_secret_access_key=secret,
    )


def _read_run(db, run_id: UUID) -> dict[str, Any]:
    row = db.execute(text("""
        SELECT id, project_id, state, waiting_reason, error_code, generation,
               revision, usage_tokens, reserved_tokens, token_limit,
               elapsed_limit_ms, elapsed_used_ms, cancel_requested, plan_digest, lease_expires_at
        FROM runs WHERE id=:run
    """), {"run": run_id}).mappings().one_or_none()
    if row is None:
        raise AcceptanceError("run_id does not exist in the configured PostgreSQL database")
    return {key: (str(value) if isinstance(value, UUID) else value)
            for key, value in row.items()}


def _read_executors(db, run_id: UUID) -> list[dict[str, Any]]:
    rows = db.execute(text("""
        SELECT id, generation, kind, operation_id, process_incarnation,
               container_id, engine_id, state, proof
        FROM runtime_executors WHERE run_id=:run ORDER BY generation, kind, id
    """), {"run": run_id}).mappings().all()
    return [{
        "id": str(row["id"]), "generation": row["generation"], "kind": row["kind"],
        "operation_id": row["operation_id"],
        "process_incarnation": str(row["process_incarnation"]),
        "container_id": row["container_id"], "engine_id": row["engine_id"],
        "state": row["state"], "has_inactive_proof": bool(row["proof"]),
    } for row in rows]


def _read_checkpoint_refs(db, run_id: UUID) -> tuple[int, list[dict[str, Any]]]:
    row = db.execute(text("""
        SELECT revision, manifest FROM checkpoints WHERE run_id=:run
        ORDER BY revision DESC LIMIT 1
    """), {"run": run_id}).mappings().one_or_none()
    if row is None:
        return 0, []
    manifest = row["manifest"]
    if isinstance(manifest, str):
        manifest = json.loads(manifest)
    if not isinstance(manifest, dict):
        raise AcceptanceError("latest checkpoint manifest is not an object")
    refs = [manifest.get("context"), *(manifest.get("workspace") or [])]
    sanitized = []
    for ref in refs:
        if not isinstance(ref, dict):
            raise AcceptanceError("checkpoint contains a malformed object reference")
        key, digest, size = ref.get("key"), ref.get("sha256"), ref.get("size")
        if not isinstance(key, str) or not key or type(size) is not int or size < 0:
            raise AcceptanceError("checkpoint object reference lacks a valid key/size")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise AcceptanceError("checkpoint object reference has an invalid hash")
        sanitized.append({"key_sha256": hashlib.sha256(key.encode()).hexdigest(),
                          "sha256": digest, "size": size})
    return int(row["revision"]), sanitized


def _verify_checkpoint_objects(s3, db, run_id: UUID, *, tolerate_fault: bool = False) -> dict[str, Any]:
    revision, refs = _read_checkpoint_refs(db, run_id)
    if not refs:
        raise AcceptanceError("no committed checkpoint object references exist")
    verified = 0
    for ref in refs:
        # Resolve the key only in memory. Do not include object keys or bytes in evidence.
        row = db.execute(text("""
            SELECT manifest FROM checkpoints WHERE run_id=:run AND revision=:revision
        """), {"run": run_id, "revision": revision}).mappings().one()
        manifest = row["manifest"]
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        candidates = [manifest.get("context"), *(manifest.get("workspace") or [])]
        object_ref = next((item for item in candidates
                           if hashlib.sha256(item["key"].encode()).hexdigest() == ref["key_sha256"]), None)
        if object_ref is None:
            raise AcceptanceError("checkpoint key-hash mapping changed while verifying")
        try:
            body = s3.get_object(Bucket=DEFAULT_BUCKET, Key=object_ref["key"])["Body"].read(ref["size"] + 1)
        except Exception as exc:
            response = getattr(exc, "response", {})
            code = str(response.get("Error", {}).get("Code", "")) if isinstance(response, dict) else ""
            status = "missing" if code in {"404", "NoSuchKey", "NotFound"} else "unavailable"
            if tolerate_fault:
                return {"checkpoint_revision": revision, "object_count": len(refs),
                        "objects_verified": verified, "status": status,
                        "failure_type": type(exc).__name__}
            raise AcceptanceError(f"MinIO object read failed: {type(exc).__name__}") from None
        if len(body) != ref["size"] or hashlib.sha256(body).hexdigest() != ref["sha256"]:
            if tolerate_fault:
                return {"checkpoint_revision": revision, "object_count": len(refs),
                        "objects_verified": verified, "status": "mismatch",
                        "failure_type": "IntegrityMismatch"}
            raise AcceptanceError("MinIO checkpoint object length or digest mismatch")
        verified += 1
    return {"checkpoint_revision": revision, "object_count": len(refs),
            "objects_verified": verified, "status": "verified"}


def _snapshot(db, run_id: UUID, s3, *, docker_context: str,
              tolerate_storage_fault: bool = False) -> dict[str, Any]:
    run = _read_run(db, run_id)
    executors = _read_executors(db, run_id)
    for executor in executors:
        if executor["state"] == "active":
            _assert_live_executor(docker_context, run_id, executor)
    operations = db.execute(text("""
        SELECT operation_id, generation, kind, state, reserve_tokens, usage_tokens,
               CASE WHEN result IS NULL THEN false ELSE true END AS has_result
        FROM operations WHERE run_id=:run ORDER BY created_at, operation_id
    """), {"run": run_id}).mappings().all()
    event_counts = db.execute(text("""
        SELECT payload->>'state' AS state, count(*) AS count FROM events
        WHERE run_id=:run AND kind='run.state' GROUP BY payload->>'state'
    """), {"run": run_id}).all()
    extensions = db.execute(text("""
        SELECT token_limit_before, token_limit_after, elapsed_limit_before_ms, elapsed_limit_after_ms
        FROM run_budget_extensions WHERE run_id=:run ORDER BY created_at, id
    """), {"run": run_id}).mappings().all()
    revision, refs = _read_checkpoint_refs(db, run_id)
    storage = _verify_checkpoint_objects(s3, db, run_id, tolerate_fault=tolerate_storage_fault) if refs else {
        "checkpoint_revision": 0, "object_count": 0, "objects_verified": 0, "status": "absent",
    }
    orphans = None
    try:  # independent recount: objects under the project prefix with no stored_objects row (count only, no keys)
        registered = {r[0] for r in db.execute(text("SELECT key FROM stored_objects WHERE project_id=:p"), {"p": run["project_id"]})}
        present: set[str] = set()
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=DEFAULT_BUCKET, Prefix=f"{run['project_id']}/"):
            present.update(item["Key"] for item in page.get("Contents", []))
        orphans = len(present - registered)
    except Exception:
        orphans = None
    return {
        "orphan_object_count": orphans,
        "run": run, "executors": executors,
        "operations": [{key: row[key] for key in row.keys()} for row in operations],
        "state_event_counts": {str(row.state): int(row.count) for row in event_counts},
        "checkpoint_revision": revision, "checkpoint_refs": refs,
        "storage": storage, "budget_extensions": [dict(row) for row in extensions],
    }


def _docker(context: str, *args: str, timeout: int = 45) -> str:
    try:
        result = subprocess.run(
            ["docker", "--context", context, *args], check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcceptanceError(f"owned Docker query failed: {type(exc).__name__}") from None
    if result.returncode:
        raise AcceptanceError("owned Docker query failed; output suppressed")
    return result.stdout.decode("utf-8", "replace").strip()


def _inspect_container(context: str, container_id: str) -> dict[str, Any]:
    if not CONTAINER_RE.fullmatch(container_id):
        raise AcceptanceError("container id is not canonical")
    raw = _docker(context, "inspect", "--format",
                  '{{.Id}}|{{.Image}}|{{.State.Running}}|{{.State.Pid}}|{{.RestartCount}}|{{.State.StartedAt}}|{{index .Config.Labels "scientist.platform/test"}}|{{index .Config.Labels "scientist.platform/role"}}',
                  container_id)
    fields = raw.split("|")
    if len(fields) != 8 or fields[0] != container_id:
        raise AcceptanceError("Docker returned unexpected isolated-service identity")
    if fields[6] != "true" or fields[7] != "server":
        raise AcceptanceError("server container lacks required isolated B5 fixture labels")
    return {"id": fields[0], "image_id": fields[1], "running": fields[2] == "true",
            "pid": int(fields[3]), "restart_count": int(fields[4]), "started_at": fields[5]}


def _assert_live_executor(context: str, run_id: UUID, executor: dict[str, Any]) -> None:
    container_id = executor.get("container_id")
    if not isinstance(container_id, str) or not CONTAINER_RE.fullmatch(container_id):
        raise AcceptanceError("active executor has no canonical container identity")
    raw = _docker(context, "inspect", "--format",
                  '{{.Id}}|{{.Image}}|{{.State.Running}}|{{index .Config.Labels "scientist.platform/run"}}|{{index .Config.Labels "scientist.platform/generation"}}|{{index .Config.Labels "scientist.platform/executor"}}|{{index .Config.Labels "scientist.platform/kind"}}|{{index .Config.Labels "scientist.platform/incarnation"}}',
                  container_id)
    fields = raw.split("|")
    expected = (container_id, str(run_id), str(executor["generation"]), executor["id"],
                str(executor["kind"]), executor["process_incarnation"])
    if (len(fields) != 8 or fields[0] != expected[0] or fields[2] != "true"
            or tuple(fields[3:]) != expected[1:]):
        raise AcceptanceError("live Docker executor does not match durable run/generation identity")
    if executor["kind"] == "worker" and fields[1] != WORKER_IMAGE:
        raise AcceptanceError("live worker image does not match accepted immutable digest")


def _fixture_proof(path: Path, case: str, server_digest: str, fixture_digest: str) -> dict[str, Any]:
    data = _json_file(path)
    if data.get("schema_version") != 1 or data.get("case") != case or data.get("status") != "PASS":
        raise AcceptanceError("fixture proof schema/case/status does not match")
    if _safe_digest(data.get("worker_image_digest"), "fixture worker image") != WORKER_IMAGE:
        raise AcceptanceError("fixture proof is for a different worker image")
    if _safe_digest(data.get("server_image_digest"), "fixture server image") != server_digest:
        raise AcceptanceError("fixture proof is for a different server image")
    if _safe_digest(data.get("fixture_image_digest"), "fault fixture image") != fixture_digest:
        raise AcceptanceError("fixture proof is for a different fault fixture image")
    proof = data.get("proof")
    if not isinstance(proof, dict) or set(proof) != PROOF_KEYS[case]:
        raise AcceptanceError("fixture proof has an unexpected field set")
    return proof


def _assert_case(case: str, before: dict[str, Any], after: dict[str, Any], proof: dict[str, Any] | None) -> None:
    run = after["run"]
    active = [row for row in after["executors"] if row["state"] == "active"]
    assert proof is not None
    if case == "supervisor-reload":
        if proof["provider_attempts"] != 1 or not all(proof[key] is True for key in PROOF_KEYS[case] - {"provider_attempts"}):
            raise AcceptanceError("restart proof is incomplete")
        prior = before["run"]
        if (run["state"] != "completed" or run["revision"] != prior["revision"] or run["generation"] <= prior["generation"]
                or (run["usage_tokens"], run["reserved_tokens"]) != (prior["usage_tokens"], prior["reserved_tokens"])):
            raise AcceptanceError("restart did not preserve run identity/usage or complete via a replacement generation")
        if after["checkpoint_revision"] < before["checkpoint_revision"] or active:
            raise AcceptanceError("checkpoint regressed or an executor remains active after the restart")
        old = {x["operation_id"]: (x["generation"], x["state"], x["reserve_tokens"], x["usage_tokens"]) for x in before["operations"]}
        new = {x["operation_id"]: (x["generation"], x["state"], x["reserve_tokens"], x["usage_tokens"]) for x in after["operations"]}
        if old != new:
            raise AcceptanceError("restart changed the durable operation ledger")
    elif case == "generation-fence":
        if proof["stale_capability_denied"] is not True or proof["late_effect_count"] != 0:
            raise AcceptanceError("old-generation capability was not proven denied without effects")
        if type(proof["old_generation"]) is not int or proof["old_generation"] >= run["generation"]:
            raise AcceptanceError("fencing proof does not identify an older generation")
        before_ops = [(x["operation_id"], x["generation"], x["state"], x["reserve_tokens"], x["usage_tokens"])
                      for x in before["operations"]]
        after_ops = [(x["operation_id"], x["generation"], x["state"], x["reserve_tokens"], x["usage_tokens"])
                     for x in after["operations"]]
        if before_ops != after_ops:
            raise AcceptanceError("stale capability attempt changed the durable operation ledger")
    elif case == "hung-stop":
        if proof["stop_completed"] is not True or proof["late_effect_count"] != 0:
            raise AcceptanceError("hung operation stop or post-stop fence failed")
        if active or not all(row["has_inactive_proof"] for row in after["executors"]):
            raise AcceptanceError("hung-stop lacks exact inactive executor proofs")
        if proof["operation_state"] != "unknown":
            raise AcceptanceError("fenced hung operation is not unknown")
        if proof["operation_state"] not in {item["state"] for item in after["operations"]}:
            raise AcceptanceError("hung-stop proof state is absent from the durable operation ledger")
    elif case in {"race-completion", "race-cancel"}:
        winner = "completion" if case == "race-completion" else "cancel"
        expected_state = "completed" if winner == "completion" else "canceled"
        if proof["winner"] != winner or run["state"] != expected_state:
            raise AcceptanceError("database state does not match forced race winner")
        expected_events = 1 if winner == "completion" else 0
        database_completion_events = after["state_event_counts"].get("completed", 0)
        if (proof["completion_event_count"] != expected_events
                or database_completion_events != expected_events
                or proof["late_effect_count"] != 0 or active):
            raise AcceptanceError("race event/effect/executor evidence is inconsistent")
    elif case == "checkpoint-recovery":
        if proof["continuation_used_committed_results"] is not True or proof["original_prompt_replayed"] is not False:
            raise AcceptanceError("recovery did not continue after the committed operation boundary")
        if proof["restored_workspace_verified"] is not True or after["storage"]["objects_verified"] == 0:
            raise AcceptanceError("workspace or actual MinIO checkpoint bytes were not verified")
        if proof["seeded_context_hash_matched"] is not True or proof["attempts_unchanged"] is not True:
            raise AcceptanceError("restored context hash differs from the seeded one, or provider attempts changed")
        if proof["context_class"] not in {"compressed", "todo_messages_carry_through", "workspace", "todo"}:
            raise AcceptanceError("recovery proof does not identify a required context case")
    elif case == "checkpoint-commit-fault":
        orphans = proof["orphan_object_delta"]
        if proof["fault_kind"] not in {"db_commit", "upload"} or type(orphans) is not int:
            raise AcceptanceError("commit-fault proof has an invalid kind or orphan count")
        if (proof["checkpoint_revision_unchanged"] is not True or proof["previous_checkpoint_verified"] is not True
                or proof["replacement_started"] is not False or proof["run_actionable"] is not True):
            raise AcceptanceError("checkpoint commit fault left the run unsafe")
        if proof["checkpoint_revision"] != after["checkpoint_revision"] or after["storage"].get("status") != "verified":
            raise AcceptanceError("durable checkpoint revision/bytes differ from the proof")
        recount = after["orphan_object_count"]
        if recount is None or recount != orphans:
            raise AcceptanceError("independent MinIO-minus-registry orphan recount differs from the proof")
        if (orphans < 1) if proof["fault_kind"] == "db_commit" else (orphans != 0):
            raise AcceptanceError("orphan object count does not match the fault kind")
        if run["state"] not in {"queued", "waiting_input"} or active:
            raise AcceptanceError("run is not actionable (leave it queued until this readback; finalize afterwards)")
        if run["generation"] != 1 or any(row["generation"] != 1 for row in after["executors"]):
            raise AcceptanceError("a replacement generation or executor exists after the checkpoint fault")
    elif case.startswith("checkpoint-"):
        if proof["checkpoint_rejected"] is not True or proof["replacement_started"] is not False:
            raise AcceptanceError("invalid checkpoint was not rejected before replacement execution")
        if run["state"] not in {"waiting_input", "failed"} or active:
            raise AcceptanceError("checkpoint fault did not leave an actionable state without active executor")
        if run["generation"] != 1 or any(row["generation"] != 1 for row in after["executors"]):
            raise AcceptanceError("a replacement generation or executor exists after the checkpoint fault")
        if case == "checkpoint-storage-fault":
            if proof["reason_fixture_attested"] is not True:
                raise AcceptanceError("storage-fault reason must be marked fixture-attested")
            # Known spec gap (recorded by the parent): the product collapses a storage outage into
            # checkpoint_integrity_unproven; only the fixture knows the outage was the cause.
        if proof["reason"] not in {"missing", "incompatible", "corrupt", "storage_unavailable"}:
            raise AcceptanceError("checkpoint fault reason is not in the accepted error vocabulary")
        # Product fails closed to one reason for every verification failure. The storage fault is lifted
        # before readback, so bytes must verify again; pin drift never touches stored bytes.
        if run["waiting_reason"] != "checkpoint_integrity_unproven":
            raise AcceptanceError("checkpoint fault did not fail closed to checkpoint_integrity_unproven")
        expected_storage = {"checkpoint-missing": "missing", "checkpoint-corrupt": "mismatch",
                            "checkpoint-incompatible": "verified", "checkpoint-storage-fault": "verified"}.get(case)
        if expected_storage and after["storage"].get("status") != expected_storage:
            raise AcceptanceError("actual MinIO state does not match the injected checkpoint fault")
        if case == "checkpoint-corrupt" and proof["workspace_preserved"] is not True:
            raise AcceptanceError("corrupt checkpoint overwrote the prior workspace")
    elif case == "usage-ceiling-extension":
        if run["usage_tokens"] + run["reserved_tokens"] > run["token_limit"]:
            raise AcceptanceError("durable token ledger exceeds approved ceiling")
        if (proof["token_ceiling_enforced"] is not True or proof["elapsed_ceiling_enforced"] is not True
                or proof["extension_applied_once"] is not True
                or proof["elapsed_time_ledger_persisted"] is not True
                or proof["elapsed_extension_event_count"] != 1):
            raise AcceptanceError("ceiling/extension fixture proof is incomplete")
        if (type(proof["active_elapsed_before_ms"]) is not int
                or type(proof["active_elapsed_after_ms"]) is not int
                or proof["active_elapsed_after_ms"] < proof["active_elapsed_before_ms"]):
            raise AcceptanceError("active elapsed-time evidence is invalid")
        before_run = before["run"]
        if (before_run["state"] != "waiting_input" or before_run["waiting_reason"] != "budget_exhausted"
                or run["token_limit"] <= before_run["token_limit"]
                or run["elapsed_limit_ms"] <= before_run["elapsed_limit_ms"]
                or run["usage_tokens"] < before_run["usage_tokens"]):
            raise AcceptanceError("approved extension did not persist a larger ceiling and prior usage")
        if proof["active_elapsed_before_ms"] < before_run["elapsed_limit_ms"] or before_run["elapsed_used_ms"] < before_run["elapsed_limit_ms"]:
            raise AcceptanceError("time ceiling was not reached before approved extension (baseline elapsed_used_ms)")
        if before_run["usage_tokens"] + before_run["reserved_tokens"] >= before_run["token_limit"]:
            raise AcceptanceError("baseline tokens are not below the limit, so the wait was not the elapsed ceiling")
        if len(after.get("budget_extensions") or []) != 2:
            raise AcceptanceError("expected exactly two durable extension rows")
        extensions = after.get("budget_extensions") or []
        if (not extensions or extensions[-1]["token_limit_after"] != run["token_limit"]
                or extensions[-1]["elapsed_limit_after_ms"] != run["elapsed_limit_ms"]):
            raise AcceptanceError("durable extension record does not match the persisted ceiling")
        try:
            UUID(str(proof["extension_decision_id"]))
        except ValueError:
            raise AcceptanceError("approved extension decision id is invalid") from None


def _write_output(path: Path, text_: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        archive = path.parent / "b5-matrix-proof-archive-20261004"
        archive.mkdir(parents=True, exist_ok=True)
        old = path.read_bytes()
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        (archive / f"{path.stem}-{hashlib.sha256(old).hexdigest()[:12]}-{stamp}{path.suffix}").write_bytes(old)
    path.write_text(text_, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=CASES)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--fixture-proof", type=Path)
    parser.add_argument("--server-image-digest")
    parser.add_argument("--fixture-image-digest")
    parser.add_argument("--docker-context", default=CFG.docker_context)
    parser.add_argument("--server-container", help="optional isolated server container to cross-check against the image pin")
    parser.add_argument("--output", type=Path, default=CFG.evidence / "b5_supervisor_matrix_result.json",
                        help="per-case result file under the evidence dir (previous file archived first)")
    args = parser.parse_args()

    try:
        run_id = UUID(args.run_id)
        server_digest = _safe_digest(args.server_image_digest, "server image")
        if args.server_container:
            server_before = _inspect_container(args.docker_context, args.server_container)
            if not server_before["running"] or server_before["image_id"] != server_digest:
                raise AcceptanceError("isolated server container does not match supplied immutable image pin")
        if args.case == "capture-baseline":
            if not args.server_container:
                raise AcceptanceError("capture-baseline requires --server-container")
            engine = create_engine(_db_url(), pool_pre_ping=True)
            s3 = _s3_client()
            with engine.connect() as db:
                snapshot = _snapshot(db, run_id, s3, docker_context=args.docker_context)
            if snapshot["run"]["state"] != "running" or not any(
                    row["state"] == "active" and row["kind"] == "worker"
                    for row in snapshot["executors"]):
                raise AcceptanceError("baseline requires a running run with an actual active worker")
            if snapshot["checkpoint_revision"] < 1:
                raise AcceptanceError("baseline requires an actual committed checkpoint")
            baseline = {"schema_version": 1, "run_id": str(run_id), "snapshot": snapshot}
            baseline["server_image_digest"] = server_digest
            if args.fixture_image_digest:
                baseline["fixture_image_digest"] = _safe_digest(args.fixture_image_digest, "fixture image")
            _write_output(args.output, json.dumps(baseline, sort_keys=True, indent=2, default=str) + "\n")
            print(json.dumps({"status": "PASS", "case": args.case, "run_id": str(run_id),
                              "output": str(args.output)}, sort_keys=True))
            engine.dispose()
            return 0
        if not args.fixture_proof or not args.fixture_image_digest:
            raise AcceptanceError("every proof-driven case requires --fixture-proof and --fixture-image-digest")
        fixture_digest = _safe_digest(args.fixture_image_digest, "fault fixture image")
        proof = _fixture_proof(args.fixture_proof, args.case, server_digest, fixture_digest)
        supplied_before = _json_file(args.baseline)
        if supplied_before.get("run_id") != str(run_id):
            raise AcceptanceError("baseline run_id does not match requested run")
        baseline = supplied_before.get("snapshot")
        if not isinstance(baseline, dict):
            raise AcceptanceError("baseline must contain a snapshot object")

        engine = create_engine(_db_url(), pool_pre_ping=True)
        s3 = _s3_client()
        with engine.connect() as db:
            after = _snapshot(db, run_id, s3, docker_context=args.docker_context,
                              tolerate_storage_fault=args.case.startswith("checkpoint-"))
        if args.server_container:
            server_after = _inspect_container(args.docker_context, args.server_container)
            if not server_after["running"] or server_after["image_id"] != server_digest:
                raise AcceptanceError("isolated server image changed or did not remain running")
        _assert_case(args.case, baseline, after, proof)

        report = {
            "schema_version": 1, "status": "PASS", "case": args.case,
            "evidence_class": "fixture-attested-live; PostgreSQL-and-MinIO-verified",
            "run_id": str(run_id), "worker_image_digest": WORKER_IMAGE,
            "server_image_digest": server_digest, "fixture_image_digest": fixture_digest,
            "snapshot": after, "fixture_proof_fields": sorted(proof),
        }
        _write_output(args.output, json.dumps(report, sort_keys=True, indent=2, default=str) + "\n")
        print(json.dumps({"status": "PASS", "case": args.case, "evidence_class": report["evidence_class"],
                          "run_id": str(run_id), "output": str(args.output)}, sort_keys=True))
        engine.dispose()
        return 0
    except AcceptanceError as exc:
        print(json.dumps({"status": "BLOCKED", "case": args.case, "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    except Exception as exc:
        # Keep SQLAlchemy, boto, and subprocess details out of reports/terminal output.
        print(json.dumps({"status": "ERROR", "case": args.case, "error_type": type(exc).__name__}, sort_keys=True), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
