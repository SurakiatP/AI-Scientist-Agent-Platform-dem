#!/usr/bin/env python3
"""Actors f/g: checkpoint fault cases (synthetic; serial; owned engine).

  f (barrier fixture, workspace-seeded checkpoint, kill, tamper, recover):
    missing | incompatible | corrupt | storage-fault
  g (checkpoint-fault fixture image, real worker, injected capture fault on the second checkpoint):
    db-commit | upload        (image: pin `checkpoint_fault` or env B5_CHECKPOINT_FAULT_IMAGE=repo@sha256:...)
  uv run python backend/tests/live/b5_matrix_checkpoint_faults.py CASE
f cases leave the run waiting_input/checkpoint_integrity_unproven (no replacement, executors fenced, networks gone);
g cases leave it QUEUED on purpose (the matrix readback needs the run actionable); run the printed readback
command next, then `... b5_matrix_checkpoint_faults.py finalize RUN_ID` (queued -> canceled). Until then the claim
guard in every actor refuses to start. Proofs: <evidence-dir>/b5-matrix-checkpoint-CASE.json (+ -baseline.json).
"""
from __future__ import annotations

import os
import sys

import b5_matrix_common as c

F_CASES = {"missing": "checkpoint-missing", "incompatible": "checkpoint-incompatible",
           "corrupt": "checkpoint-corrupt", "storage-fault": "checkpoint-storage-fault"}
REASON = {"missing": "missing", "incompatible": "incompatible", "corrupt": "corrupt", "storage-fault": "storage_unavailable"}
EXPECTED_STORAGE = {"missing": "missing", "corrupt": "mismatch", "incompatible": "verified", "storage-fault": "verified"}


def need(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError(message)


def workspace_intact(h: c.H, manifest: dict) -> bool:
    return all(c.sha(h.s3.get_object(Bucket=c.BUCKET, Key=ref.key)["Body"].read()) == ref.sha256
               for ref in c.CheckpointManifest.model_validate(manifest).workspace)


def f_case(case: str) -> None:
    barrier = c.fixture_ref("barrier")
    with c.actor(F_CASES[case], barrier, fast_s3=(case == "storage-fault")) as h:
        h.seed = c.seed_workspace  # a real workspace object makes "workspace preserved" meaningful
        h.guard_no_other_claimable()
        h.new_run()
        base = c.write_min_baseline(h, F_CASES[case])
        before = h.barrier_kill_fence(barrier)  # worker dead; old dispatch still to be fenced by recover
        manifest = h.latest_checkpoint()["manifest"]
        context_ref = c.CheckpointManifest.model_validate(manifest).context
        need(h.verify_checkpoint(manifest) == "verified", "checkpoint did not verify before tampering")
        evidence: dict = {}
        h.stage = f"inject_{case}"
        if case == "missing":
            h.s3.delete_object(Bucket=c.BUCKET, Key=context_ref.key)
        elif case == "corrupt":
            h.s3.put_object(Bucket=c.BUCKET, Key=context_ref.key, Body=b"corrupt checkpoint bytes", ContentType="application/octet-stream")
        try:
            if case == "incompatible":  # trusted-pin drift in the recovering process; stored bytes stay intact
                h.configure_storage(environment_digest="0" * 64)
            elif case == "storage-fault":
                c.container_state(c.MINIO_CONTAINER, "stop", h.run_id)
            h.stage = "recover"
            view = h.recover()
            if case == "storage-fault":
                evidence["status_during_fault"] = h.verify_checkpoint(manifest)
        finally:
            if case == "incompatible":
                h.configure_storage()
            elif case == "storage-fault":
                c.container_state(c.MINIO_CONTAINER, "start", h.run_id)
                c.wait_ready()
        h.stage = "verify"
        need((view.state, view.waiting_reason) == ("waiting_input", "checkpoint_integrity_unproven"),
             "checkpoint fault did not fail closed to checkpoint_integrity_unproven")
        run, executors = h.run_row(), h.executors()
        replacement = run["generation"] != 1 or any(e["generation"] != 1 for e in executors) or h.attempts() != before["attempts"]
        need(not replacement and len(h.ops()) == 1, "a replacement started or a provider request was resent")
        storage = h.verify_checkpoint(manifest)
        need(storage == EXPECTED_STORAGE[case], "MinIO read-back does not match the injected fault")
        if case == "storage-fault":
            evidence["status_after_fault_lifted"] = storage
            need(evidence["status_during_fault"] == "unavailable" and h.run_row()["state"] == "waiting_input",
                 "storage fault was not observed or the run auto-resumed")
        proof = {"checkpoint_rejected": True, "replacement_started": False, "reason": REASON[case]}
        if case == "storage-fault":
            # The product collapses an outage into checkpoint_integrity_unproven (known spec gap, parent-recorded);
            # only this fixture knows the cause, so the reason is attested by the actor, not by the product.
            proof["reason_fixture_attested"] = True
        if case == "corrupt":
            proof["workspace_preserved"] = workspace_intact(h, manifest)
            need(proof["workspace_preserved"], "workspace object changed")
        cleanup = h.exact_cleanup()
        c.emit(F_CASES[case], c.proof_doc(F_CASES[case], barrier, proof,
               {"run_id": str(h.run_id), "baseline": str(base), "storage_status": storage, "cleanup": cleanup, **evidence}))


def finalize(run_id: str) -> None:
    """Stop (queued -> canceled) a commit-fault run AFTER the parent's matrix readback, then remove private files.

    Until this runs the run stays queued, and every actor's claim guard refuses to start.
    """
    image = c.fixture_ref("checkpoint_fault", os.environ.get("B5_CHECKPOINT_FAULT_IMAGE"))
    c.require_clean_repo()
    h = c.H("finalize").setup(image).attach(c.UUID(run_id))
    h.configure(image, "finalize")
    with c.session() as db:
        state = c.supervisor.stop(db, h.run_id, 5).state
    if state != "canceled":
        raise RuntimeError("finalize stop did not cancel the run")
    cleanup = h.exact_cleanup()
    print(c.json.dumps({"status": "FINALIZED", "run_id": run_id, "state": state, "cleanup": cleanup}, sort_keys=True))


def g_case(case: str) -> None:
    kind = {"db-commit": "db_commit", "upload": "upload"}[case]
    image = c.fixture_ref("checkpoint_fault", os.environ.get("B5_CHECKPOINT_FAULT_IMAGE"))
    with c.actor(f"checkpoint-{case}-fault", image) as h:
        h.guard_no_other_claimable()
        h.new_run()
        base = c.write_min_baseline(h, f"checkpoint-{case}-fault")
        with c.session() as db:
            db.execute(c.text("CREATE TABLE IF NOT EXISTS b5_fixture_checkpoint_faults (run_id uuid NOT NULL, generation int NOT NULL, "
                              "kind text NOT NULL, consumed boolean NOT NULL DEFAULT false, PRIMARY KEY (run_id, generation))"))
            db.execute(c.text("INSERT INTO b5_fixture_checkpoint_faults (run_id, generation, kind) VALUES (:r, 1, :k)"),
                       {"r": h.run_id, "k": kind})
            db.commit()
        h.configure(image, "ckpt-fault")
        _, worker = h.claim_start()
        h.stage = "wait_for_faulted_worker"
        code = h.wait(worker)
        consumed = h.q("SELECT consumed FROM b5_fixture_checkpoint_faults WHERE run_id=:run")[0]["consumed"]
        need(code != 0 and consumed is True, "the injected checkpoint fault did not fail the worker")
        latest = h.latest_checkpoint()
        revision_unchanged = latest is not None and latest["revision"] == 1  # only the pre-fault checkpoint exists
        previous_ok = revision_unchanged and h.verify_checkpoint(latest["manifest"]) == "verified"
        orphans = h.orphan_count()  # fresh project: every unregistered object is a capture leftover
        h.stage = "recover"
        view = h.recover()
        run = h.run_row()
        replacement = run["generation"] != 1 or any(e["generation"] != 1 for e in h.executors())
        attempts = h.attempts()
        need(revision_unchanged and previous_ok and attempts == 1 and not replacement, "checkpoint fault left inconsistent durable state")
        need(orphans >= 1 if kind == "db_commit" else orphans == 0, "orphan object count does not match the fault kind")
        need(view.state == "queued", "run is not actionable (expected queued with a verified previous checkpoint)")
        cleanup = h.exact_cleanup(remove_files=False)  # keep the template so --finalize can reuse it
        c.emit(f"checkpoint-{case}-fault", c.proof_doc("checkpoint-commit-fault", image,
               {"fault_kind": kind, "checkpoint_revision": latest["revision"], "checkpoint_revision_unchanged": True,
                "previous_checkpoint_verified": True, "orphan_object_delta": orphans, "replacement_started": False,
                "run_actionable": True},
               {"run_id": str(h.run_id), "baseline": str(base), "worker_exit_code": code, "provider_attempts": attempts,
                "cleanup": cleanup}))


if __name__ == "__main__":
    case = sys.argv[1]
    if case == "finalize":
        finalize(sys.argv[2])
    elif case in F_CASES:
        f_case(case)
    elif case in {"db-commit", "upload"}:
        g_case(case)
    else:
        raise SystemExit("unknown case")
