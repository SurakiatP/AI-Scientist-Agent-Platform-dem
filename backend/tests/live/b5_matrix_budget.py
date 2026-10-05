#!/usr/bin/env python3
"""Actor h: budget ceiling / stale-decision / replay across a deployment restart, plus owner-retry.

  uv run python backend/tests/live/b5_matrix_budget.py usage-ceiling-extension   (counter fixture)
  uv run python backend/tests/live/b5_matrix_budget.py owner-retry                (counter fixture, model fixture-unknown-once)

usage-ceiling-extension:
  A. token_limit=0, elapsed_limit_ms=1: claim refuses (token ceiling) -> waiting_input/budget_exhausted, no attempt.
  B. owner extension #1 (tokens 20000, elapsed 2 ms) -> recover/claim/start: the worker's first effect trips the
     ELAPSED ceiling -> waiting_input/budget_exhausted again (new decision id), zero provider attempts.
  C. restart owned PostgreSQL+MinIO; a FRESH process submits stale-decision, wrong-revision, lowering and
     idempotency-conflict extensions (all rejected), then extension #2 (tokens 30000, elapsed 60000), then replays it
     (no second row/event). A second fresh process recovers and the run completes.
  elapsed_extension_event_count = usage.updated events emitted by extension #2's own call (sequence watermark).
owner-retry: lost response -> unknown_outcome -> restart -> fresh recover (still unknown, no resend) ->
  POST /runs/{id}/decisions (REST, queued retry only) -> claim/start replacement worker ->
  exactly two provider attempts (lost + owner retry), original stays unknown, retry op committed, run completes.
"""
from __future__ import annotations

import json
import sys
from uuid import UUID

import b5_matrix_common as c
from scientist import broker, supervisor
from scientist.auth import DomainError
from scientist.contracts import Principal
from scientist.domain import extend_run_budget
from scientist.limits import effective_elapsed_ms


def need(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError(message)


def extend_fresh(run_id: UUID, identity: UUID, decision: UUID, consumed: UUID) -> dict:
    """Runs in a NEW process after the restart: stale/replay/lowering checks then the real extension."""
    owner = Principal(identity=identity, kind="owner")
    out: dict = {}
    with c.session() as db:
        row = db.execute(c.text("SELECT revision, token_limit, elapsed_limit_ms FROM runs WHERE id=:r"), {"r": run_id}).one()

        def attempt(label, rev, dec, key, tokens, elapsed):
            try:
                extend_run_budget(db, owner, run_id, rev, dec, key, tokens, elapsed)
                out[label] = "ok"
            except DomainError as exc:
                db.rollback()
                out[label] = f"{exc.code}:{exc.status}"
        attempt("stale_decision", row.revision, consumed, "stale-1", 30_000, 600_000)  # decision 1 was already consumed
        attempt("wrong_revision", row.revision + 1, decision, "wrong-rev-1", 30_000, 600_000)
        attempt("lowered_limits", row.revision, decision, "lower-1", row.token_limit - 1, row.elapsed_limit_ms)
        mark = db.execute(c.text("SELECT COALESCE(MAX(sequence), 0) FROM events WHERE run_id=:r"), {"r": run_id}).scalar_one()
        attempt("apply", row.revision, decision, "ext-2", 30_000, 600_000)
        events_after_apply = db.execute(c.text("SELECT count(*) FROM events WHERE run_id=:r AND kind='usage.updated' AND sequence > :m"),
                                        {"r": run_id, "m": mark}).scalar_one()
        attempt("replay_same", row.revision, decision, "ext-2", 30_000, 600_000)
        attempt("replay_conflict", row.revision, decision, "ext-2", 40_000, 600_000)
        out["usage_events_after_apply"] = events_after_apply
        out["usage_events_after_replay"] = db.execute(c.text(
            "SELECT count(*) FROM events WHERE run_id=:r AND kind='usage.updated' AND sequence > :m"), {"r": run_id, "m": mark}).scalar_one()
        out["extension_rows"] = db.execute(c.text("SELECT count(*) FROM run_budget_extensions WHERE run_id=:r"), {"r": run_id}).scalar_one()
    return out


def _last_mapping(h) -> dict:
    """Last operation mapping journaled in the latest checkpoint's context object (read-only)."""
    ref = c.CheckpointManifest.model_validate(h.latest_checkpoint()["manifest"]).context
    ctx = json.loads(h.s3.get_object(Bucket=c.BUCKET, Key=ref.key)["Body"].read())
    need(ctx.get("operation_mappings"), "latest checkpoint journals no operation mapping")
    return ctx["operation_mappings"][-1]


def usage_ceiling_extension() -> None:
    counter = c.fixture_ref("counter")
    with c.actor("usage-ceiling-extension", counter) as h:
        h.guard_no_other_claimable()
        h.new_run(token_limit=0, elapsed_limit_ms=1)
        h.configure(counter, "budget")
        h.stage = "A_token_ceiling"
        need(h.claim_one() is None, "claim did not refuse the token ceiling")
        run = h.run_row()
        need((run["state"], run["waiting_reason"], run["generation"]) == ("waiting_input", "budget_exhausted", 0)
             and run["budget_decision_id"] is not None and h.attempts() == 0 and not h.ops(), "token ceiling wait is wrong")
        decision1 = run["budget_decision_id"]
        h.stage = "B_extension_one_then_elapsed_ceiling"
        from scientist.model_payload import llm_input_reserve  # lazy: fails clearly if R8 has not landed
        # Small grant so ADR-012 clamping really happens. Pinned Hermes also sends the reviewed todo_list tool schema,
        # which the adapter/broker count in the input reserve (independently measured ~1303 tokens on bd0affe5). The
        # margin only sizes the grant; the post-hoc assertion below recomputes the exact allowance from the journaled body.
        TOOL_SCHEMA_MARGIN = 1600
        est_input = llm_input_reserve([{"role": "system", "content": c.SYSTEM_PROMPT}, {"role": "user", "content": c.QUESTION}])
        ext1_tokens = est_input + TOOL_SCHEMA_MARGIN
        with c.session() as db:
            extend_run_budget(db, h.owner, h.run_id, run["revision"], decision1, "ext-1", ext1_tokens, 2)
        need(h.recover().state == "queued", "extension did not let recovery queue the run")
        _, worker = h.claim_start()
        code = h.wait(worker)
        need(code == 0, f"generation-1 worker did not exit 0 after the broker recorded the budget wait (exit={code})")
        pre = h.run_row()  # read-only, BEFORE any recovery: the broker itself must have enforced the ceiling
        need((pre["state"], pre["waiting_reason"], pre["generation"]) == ("waiting_input", "budget_exhausted", 1)
             and pre["budget_decision_id"] not in (None, decision1) and h.event_count("decision.required") == 2
             and h.latest_checkpoint() is not None and not h.ops() and h.attempts() == 0,
             f"elapsed ceiling not enforced by the broker while the worker was live (exit={code}, run={pre['state']}/{pre['waiting_reason']})")
        mapping = _last_mapping(h)
        payload = mapping["request"]["payload"]
        from scientist.limits import remaining_tokens
        # same inputs as RuntimeAdapter: snapshot (token_limit - usage - reserved, from the run row), this generation's
        # reservations (0, fresh generation), and llm_input_reserve(messages, **controls) over the journaled body
        fixed = {"provider_id", "model", "recipient", "credential_id", "max_output_tokens", "messages", "timeout_seconds"}
        controls = {k: v for k, v in payload.items() if k not in fixed}
        allowance = remaining_tokens(pre) - 0 - llm_input_reserve(payload["messages"], **controls)
        need(pre["token_limit"] == ext1_tokens, f"run token_limit {pre['token_limit']} != ext-1 grant {ext1_tokens}")
        need(1 <= allowance < 2048 and payload["max_output_tokens"] == allowance,
             f"ADR-012 clamp not proven: max_output_tokens={payload['max_output_tokens']} allowance={allowance} "
             f"(ext-1={ext1_tokens}, estimated input reserve={est_input})")
        h.recover()  # fence the exited worker/dispatch; elapsed interval settles
        run = h.run_row()
        need((run["state"], run["waiting_reason"]) == ("waiting_input", "budget_exhausted")
             and run["budget_decision_id"] not in (None, decision1) and h.attempts() == 0, "elapsed ceiling wait is wrong")
        with c.session() as db:
            before_ms = effective_elapsed_ms(db, h.run_id)
        persisted = run["elapsed_active_since"] is None and run["elapsed_used_ms"] >= run["elapsed_limit_ms"]
        baseline = c.write_baseline(h, "usage-ceiling-extension")
        decision2 = run["budget_decision_id"]
        h.stage = "C_restart_and_fresh_extension"
        c.restart_services(h.run_id)
        run_restart = h.run_row()
        persisted = persisted and (run_restart["elapsed_used_ms"], run_restart["state"], run_restart["budget_decision_id"]) == (
            run["elapsed_used_ms"], "waiting_input", decision2)
        proc = c.subprocess.run([sys.executable, __file__, "extend-fresh", str(h.run_id), str(h.owner.identity), str(decision2), str(decision1)],
                                cwd=c.ROOT, capture_output=True, text=True, timeout=120, check=False)
        need(proc.returncode == 0, "fresh extension process failed")
        out = json.loads([ln for ln in proc.stdout.splitlines() if ln.startswith("{")][-1])
        need(out["stale_decision"].startswith("revision_conflict") and out["wrong_revision"].startswith("revision_conflict")
             and out["lowered_limits"].startswith("forbidden") and out["apply"] == "ok" and out["replay_same"] == "ok"
             and out["replay_conflict"].startswith("idempotency_conflict"), f"stale/replay outcomes differ: {out}")
        need(out["usage_events_after_apply"] == out["usage_events_after_replay"] == 1 and out["extension_rows"] == 2,
             "extension was not applied exactly once")
        fresh = c.fresh_process(h, counter, counter)
        run = h.run_row()
        replayed = h.q("SELECT payload_hash FROM operations WHERE run_id=:run")
        need(len(replayed) == 1 and replayed[0]["payload_hash"] == mapping["payload_hash"],
             "the replayed operation payload differs from the journaled request")
        need(fresh.get("state") == "completed" and fresh.get("exit_code") == 0 and run["state"] == "completed",
             f"run did not complete after the extension: fresh={fresh} run_state={run['state']}/{run['waiting_reason']}")
        need(run["usage_tokens"] == 2 and run["reserved_tokens"] == 0 and h.attempts() == 1, "usage/attempts after extension are wrong")
        with c.session() as db:
            after_ms = effective_elapsed_ms(db, h.run_id)
        cleanup = h.exact_cleanup()
        c.emit("usage-ceiling-extension", c.proof_doc("usage-ceiling-extension", counter, {
            "token_ceiling_enforced": True, "elapsed_ceiling_enforced": True, "active_elapsed_before_ms": before_ms,
            "active_elapsed_after_ms": after_ms, "elapsed_time_ledger_persisted": persisted,
            "elapsed_extension_event_count": out["usage_events_after_apply"], "extension_decision_id": str(decision2),
            "extension_applied_once": True}, {"run_id": str(h.run_id), "baseline": str(baseline), "stale_replay": out, "cleanup": cleanup}))


def owner_retry() -> None:
    from b5_native_fault_checks import assert_no_resend, assert_unknown_waiting
    counter = c.fixture_ref("counter")
    with c.actor("owner-retry", counter) as h:
        h.guard_no_other_claimable()
        h.new_run(model="fixture-unknown-once")
        h.configure(counter, "retry")
        _, worker = h.claim_start()
        need(h.wait(worker) != 0, "worker did not fail on the lost response")
        with c.session() as db:
            base = assert_unknown_waiting(db, h.run_id)
        c.restart_services(h.run_id)
        fresh = c.fresh_process(h, counter, None)
        need((fresh["state"], fresh["waiting_reason"]) == ("waiting_input", "unknown_outcome"), "restart lost the unknown_outcome wait")
        with c.session() as db:
            assert_no_resend(db, h.run_id, base)
        h.stage = "owner_retry"
        h.configure(counter, "retry-owner")
        view = h.rest_decide("retry", "owner-retry-1")  # REST host only queues; the supervisor/worker path dispatches
        need(view["state"] == "queued", "owner retry did not queue the replacement")
        gen, worker = h.claim_start()
        code = h.wait(worker)
        need(gen == 2 and code == 0 and h.recover().state == "completed", "replacement worker did not complete")
        ops = h.ops()
        by_id = {o["operation_id"]: o for o in ops}
        retry = [o for o in ops if o["operation_id"].startswith("retry-")]
        need(len(ops) == 2 and by_id[base["operation_id"]]["state"] == "unknown" and len(retry) == 1
             and retry[0]["state"] == "committed" and h.attempts() == 2, "owner retry did not produce exactly one extra attempt")
        run = h.run_row()
        need(run["usage_tokens"] == 2, "usage after the owner retry is wrong")
        cleanup = h.exact_cleanup()
        c.emit("owner-retry", {"schema_version": 1, "case": "owner-retry", "status": "PASS", "worker_image_digest": c.WORKER_IMAGE,
               "server_image_digest": c.SERVER_DIGEST, "fixture_image_digest": c.digest_of(counter),
               "evidence": {"run_id": str(h.run_id), "provider_attempts": 2, "operations": [(o["state"]) for o in ops],
                            "reserved_tokens_after": run["reserved_tokens"], "generation": gen, "cleanup": cleanup,
                         "repo": dict(c.REPO)}})


if __name__ == "__main__":
    if sys.argv[1] == "extend-fresh":
        print(json.dumps(extend_fresh(UUID(sys.argv[2]), UUID(sys.argv[3]), UUID(sys.argv[4]), UUID(sys.argv[5])), sort_keys=True, default=str))
    else:
        {"usage-ceiling-extension": usage_ceiling_extension, "owner-retry": owner_retry}[sys.argv[1]]()
