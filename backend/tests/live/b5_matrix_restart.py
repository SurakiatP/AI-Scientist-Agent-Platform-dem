#!/usr/bin/env python3
"""Actor a: full isolated deployment restart (matrix case supervisor-reload, ADR-011 reconcile-and-replace).

  committed: barrier flow -> kill worker after commit/before delivery -> `docker restart` of the owned test
             PostgreSQL + MinIO -> fresh supervisor process recovers/claims/starts/completes. No adopt-live path.
  unknown:   counter fixture `fixture-unknown` -> unknown_outcome -> same restart -> fresh process recover;
             run must stay unknown_outcome, reservation held, zero new provider attempts.

Run strictly serially, parent-owned Docker window only:
  uv run python backend/tests/live/b5_matrix_restart.py committed
  uv run python backend/tests/live/b5_matrix_restart.py unknown
"""
from __future__ import annotations

import subprocess
import sys

import b5_matrix_common as c
from b5_native_fault_checks import assert_no_resend, assert_unknown_waiting


def _started(name: str) -> str:
    return subprocess.run(["docker", "--context", c.CTX, "inspect", "--format", "{{.State.StartedAt}}|{{.State.Pid}}", name],
                          capture_output=True, text=True, check=True).stdout.strip()


def _need(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError(message)


def committed() -> None:
    barrier, counter = c.fixture_ref("barrier"), c.fixture_ref("counter")
    with c.actor("supervisor-reload", barrier, counter) as h:
        h.guard_no_other_claimable()
        h.new_run()
        before = h.barrier_kill_fence(barrier)
        run0, ops0 = h.run_row(), h.ops()
        started0 = (_started(c.PG_CONTAINER), _started(c.MINIO_CONTAINER))
        baseline = c.SECURITY / "b5-matrix-supervisor-reload-baseline.json"
        c.write_json(baseline, {"schema_version": 1, "run_id": str(h.run_id),
                                "snapshot": {"run": run0, "checkpoint_revision": before["checkpoint_revision"],
                                             "checkpoint_id": before["checkpoint_id"], "operations": ops0}})
        h.stage = "restart_services"
        c.restart_services(h.run_id)
        restarted = all(a != b for a, b in zip((_started(c.PG_CONTAINER), _started(c.MINIO_CONTAINER)), started0))
        h.stage = "fresh_process_recovery"
        fresh = c.fresh_process(h, barrier, counter)
        h.stage = "verify"
        run1, ops1 = h.run_row(), h.ops()
        _need(fresh.get("state") == "completed" and fresh.get("exit_code") == 0 and run1["state"] == "completed",
              "fresh process did not complete the recovered run")
        _need(run1["generation"] == 2 and run1["revision"] == run0["revision"], "run generation/revision differs")
        same_ckpt = str(h.checkpoint_by_revision(before["checkpoint_revision"])["id"]) == before["checkpoint_id"]
        same_usage = (run1["usage_tokens"], run1["reserved_tokens"]) == (run0["usage_tokens"], run0["reserved_tokens"]) == (2, 0)
        _need(len(ops1) == 1 and ops1[0]["operation_id"] == before["operation_id"]
              and (ops1[0]["result"] or {}).get("ref") == before["ref"], "operation identity/result changed")
        _need(h.attempts() == 1, "provider attempt count changed across restart")
        _need(h.verify_checkpoint(h.latest_checkpoint()["manifest"]) == "verified", "final checkpoint bytes do not verify")
        cleanup = h.exact_cleanup()
        proof = {"services_restarted": restarted, "fresh_process_recovery": True, "same_run_revision": True,
                 "same_checkpoint_id": same_ckpt, "provider_attempts": 1, "usage_reservation_unchanged": same_usage,
                 "exact_cleanup": cleanup["status"] == "PASS"}
        _need(all(v is True for k, v in proof.items() if k != "provider_attempts"), "restart proof not fully true")
        c.emit("supervisor-reload", c.proof_doc("supervisor-reload", counter, proof, {
            "run_id": str(h.run_id), "baseline": str(baseline), "generations": [1, 2], "cleanup": cleanup}))


def unknown() -> None:
    counter = c.fixture_ref("counter")
    with c.actor("supervisor-reload-unknown", counter) as h:
        h.guard_no_other_claimable()
        h.new_run(model="fixture-unknown")
        h.configure(counter, "unknown")
        _, worker = h.claim_start()
        _need(h.wait(worker) != 0, "worker did not fail on the lost response")
        with c.session() as db:
            base = assert_unknown_waiting(db, h.run_id)
        started0 = (_started(c.PG_CONTAINER), _started(c.MINIO_CONTAINER))
        h.stage = "restart_services"
        c.restart_services(h.run_id)
        restarted = all(a != b for a, b in zip((_started(c.PG_CONTAINER), _started(c.MINIO_CONTAINER)), started0))
        h.stage = "fresh_process_recovery"
        fresh = c.fresh_process(h, counter, None)
        _need((fresh["state"], fresh["waiting_reason"]) == ("waiting_input", "unknown_outcome"),
              "fresh recovery did not preserve the owner decision state")
        with c.session() as db:
            current = assert_unknown_waiting(db, h.run_id)
            assert_no_resend(db, h.run_id, base)
        _need(h.attempts() == 1 and h.run_row()["generation"] == 1, "restart resent or started a replacement")
        cleanup = h.exact_cleanup()  # run intentionally stays waiting_input/unknown_outcome
        c.emit("supervisor-reload-unknown", {
            "schema_version": 1, "case": "supervisor-reload-unknown", "status": "PASS",
            "worker_image_digest": c.WORKER_IMAGE, "server_image_digest": c.SERVER_DIGEST,
            "fixture_image_digest": c.digest_of(counter),
            "evidence": {"run_id": str(h.run_id), "services_restarted": restarted, "provider_attempts": current["provider_attempts"],
                         "reserved_tokens": current["run"]["reserved_tokens"], "usage_tokens": current["run"]["usage_tokens"],
                         "generation": 1, "cleanup": cleanup}})


if __name__ == "__main__":
    {"committed": committed, "unknown": unknown}[sys.argv[1]]()
