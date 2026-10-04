#!/usr/bin/env python3
"""Actors b/c/d: generation-fence, hung-stop, race-completion, race-cancel (synthetic; serial; owned engine).

  uv run python backend/tests/live/b5_matrix_fence_stop.py generation-fence|hung-stop|race-completion|race-cancel
Each case writes <evidence-dir>/b5-matrix-<case>.json (+ -baseline.json) for b5_supervisor_matrix.py.
"""
from __future__ import annotations

import sys
import time

import b5_matrix_common as c
from scientist.auth import DomainError
from scientist.contracts import OperationRequest
from scientist import broker, supervisor


def need(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError(message)


def _probe(h: c.H, capability: str, generation: int, name: str) -> bool:
    request = OperationRequest(run_id=h.run_id, generation=generation, operation_id=name, kind="llm", reserve_tokens=10,
        payload={"model": "fixture", "provider_id": str(h.provider_id), "recipient": "https://research.example",
                 "messages": [{"role": "user", "content": "stale probe"}], "max_tokens": 1})
    with c.session() as db:
        try:
            broker.execute(db, capability, request)
        except DomainError as exc:
            db.rollback()
            return exc.status == 403
    return False  # any accepted effect is a failure


def _late_effects(h: c.H, ops_before: int, attempts_before: int) -> int:
    return (h.attempts() - attempts_before) + (len(h.ops()) - ops_before) + h.event_count("run.state", "completed")


def generation_fence() -> None:
    barrier, counter = c.fixture_ref("barrier"), c.fixture_ref("counter")
    with c.actor("generation-fence", barrier, counter) as h:
        h.guard_no_other_claimable()
        h.new_run()
        h.barrier_kill_fence(barrier)
        with c.session() as db:  # still generation one: mint the capability that must go stale
            stale = broker.issue_capability(db, h.run_id, 1, 300)
        need(h.recover().state == "queued", "generation one was not fenced")
        h.release_barrier()
        h.configure(counter, "fence")
        claim = h.claim_one()
        need(claim == (h.run_id, 2), "run was not claimed at generation two")
        with c.session() as db:
            current = broker.issue_capability(db, h.run_id, 2, 300)
        baseline = c.write_baseline(h, "generation-fence")
        ops0, attempts0 = len(h.ops()), h.attempts()
        h.stage = "stale_probes"
        denied = [_probe(h, stale, 1, "fence-old-old"), _probe(h, stale, 2, "fence-old-new"), _probe(h, current, 1, "fence-new-old")]
        late = _late_effects(h, ops0, attempts0) + len([r for r in h.ops() if r["operation_id"].startswith("fence-")])
        need(all(denied) and late == 0, "a stale-generation effect was accepted or had a durable side effect")
        with c.session() as db:
            need(supervisor.stop(db, h.run_id, 5).state == "canceled", "stop did not cancel the run")
        cleanup = h.exact_cleanup()
        c.emit("generation-fence", c.proof_doc("generation-fence", counter,
               {"stale_capability_denied": True, "late_effect_count": late, "old_generation": 1},
               {"run_id": str(h.run_id), "probes_denied": len(denied), "baseline": str(baseline), "cleanup": cleanup}))


def hung_stop() -> None:
    counter = c.fixture_ref("counter")
    with c.actor("hung-stop", counter) as h:
        h.guard_no_other_claimable()
        h.new_run(model="fixture-stall-db")
        base = c.write_min_baseline(h, "hung-stop")
        h.configure(counter, "hung")
        h.claim_start()
        h.stage = "wait_for_hung_attempt"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not (h.attempts() == 1 and any(o["state"] == "reserved" for o in h.ops())):
            time.sleep(0.1)
        need(h.attempts() == 1, "stalled provider attempt was not observed")
        h.stage = "stop"
        hung_before = h.ops()
        with c.session() as db:
            stopped = supervisor.stop(db, h.run_id, 5)
        stop_at = h.db_clock()
        need(stopped.state == "canceled", "stop did not reach canceled")
        executors = h.executors()
        need(executors and all(e["state"] == "inactive" and e["proof"] for e in executors), "no exact inactive proof")
        ops1 = h.ops()
        state = ops1[0]["state"]  # read from the DB: product converts proven-fenced reserved -> unknown on stop
        # The stall is released only after stop, so the fenced in-flight operation must be unknown with its reservation held.
        need(len(ops1) == 1 and state == "unknown", f"hung operation has unexpected final state {state}")
        need([(o["operation_id"], o["reserve_tokens"], o["usage_tokens"]) for o in ops1]
             == [(o["operation_id"], o["reserve_tokens"], o["usage_tokens"]) for o in hung_before], "hung operation reservation or usage changed")
        with c.session() as db:  # release the stall AFTER stop: a surviving executor would now commit an effect
            db.execute(c.text("CREATE TABLE IF NOT EXISTS b5_fixture_stall_release (run_id uuid PRIMARY KEY, released_at timestamptz NOT NULL DEFAULT now())"))
            db.execute(c.text("INSERT INTO b5_fixture_stall_release (run_id) VALUES (:r) ON CONFLICT DO NOTHING"), {"r": h.run_id})
            db.commit()
        time.sleep(3)
        ops2 = h.ops()
        late = (h.q("SELECT count(*) AS n FROM b5_fixture_provider_attempts WHERE run_id=:run AND attempted_at > :t", t=stop_at)[0]["n"]
                + (len(ops2) - 1) + int(ops2[0]["state"] != state))
        need(late == 0 and h.attempts() == 1, "an effect or ledger change appeared after stop")
        ops = ops2
        cleanup = h.exact_cleanup()
        c.emit("hung-stop", c.proof_doc("hung-stop", counter,
               {"stop_completed": True, "late_effect_count": late, "operation_state": ops[0]["state"]},
               {"run_id": str(h.run_id), "baseline": str(base), "cleanup": cleanup}))


def _barrier_run(name: str):
    barrier = c.fixture_ref("barrier")
    return barrier, c.actor(name, barrier)


def race(case: str) -> None:
    barrier, ctx = _barrier_run(case)
    with ctx as h:
        h.guard_no_other_claimable()
        h.new_run()
        base = c.write_min_baseline(h, case)
        h.configure(barrier, "race")
        _, worker = h.claim_start()
        h.stage = "wait_barrier"
        h.wait_barrier()
        if case == "race-cancel":
            h.stage = "stop_while_barrier_holds"
            with c.session() as db:
                need(supervisor.stop(db, h.run_id, 5).state == "canceled", "stop did not cancel")
            h.release_barrier()  # the held dispatcher is gone; release must produce nothing
            time.sleep(3)
            winner = "cancel"
        else:
            h.stage = "release_then_complete"
            h.release_barrier()
            need(h.wait(worker) == 0, "worker did not finish after release")
            need(h.recover().state == "completed", "completion did not win")
            with c.session() as db:
                need(supervisor.stop(db, h.run_id, 5).state == "completed", "stop overwrote completion")
            winner = "completion"
        expected = {"cancel": ("canceled", 0), "completion": ("completed", 1)}[winner]
        run = h.run_row()
        completed_events = h.event_count("run.state", "completed")
        late = (h.attempts() - 1) + (len(h.ops()) - 1)
        need((run["state"], completed_events) == expected and late == 0, "race outcome/events/effects mismatch")
        cleanup = h.exact_cleanup()
        c.emit(case, c.proof_doc(case, barrier, {"winner": winner, "completion_event_count": completed_events,
               "late_effect_count": late}, {"run_id": str(h.run_id), "baseline": str(base), "cleanup": cleanup}))


if __name__ == "__main__":
    case = sys.argv[1]
    if case not in {"generation-fence", "hung-stop", "race-completion", "race-cancel"}:
        raise SystemExit("unknown case")
    {"generation-fence": generation_fence, "hung-stop": hung_stop}.get(case, lambda: race(case))()
