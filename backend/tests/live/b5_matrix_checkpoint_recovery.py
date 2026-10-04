#!/usr/bin/env python3
"""Actor e: checkpoint recovery for one context class (compressed | todo_messages_carry_through | workspace | todo).

A seeded generation-one bootstrap context is checkpointed by the real worker, the worker is killed after the
result commits (barrier fixture), and a replacement continues from the verified checkpoint on the counter
fixture. Asserts the class fields survive restore, committed results are consumed and attempts do not change.
  uv run python backend/tests/live/b5_matrix_checkpoint_recovery.py CLASS
Writes <evidence-dir>/b5-matrix-checkpoint-recovery-CLASS.json (+ -baseline.json).
"""
from __future__ import annotations

import sys

import b5_matrix_common as c
from scientist.runtime_contracts import RuntimeContextV1


def need(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError(message)


def main(cls: str) -> None:
    barrier, counter = c.fixture_ref("barrier"), c.fixture_ref("counter")
    with c.actor(f"checkpoint-recovery-{cls}", barrier, counter) as h:
        h.seed = c.SEEDS[cls]
        h.guard_no_other_claimable()
        h.new_run()
        base = c.write_min_baseline(h, f"checkpoint-recovery-{cls}")
        before = h.barrier_kill_fence(barrier)
        seeded_fp = c.class_fingerprint(h.seed_context, h.seeded_messages)
        gen, code, state = h.resume_to_completion(counter)
        h.stage = "verify"
        need(code == 0 and state == "completed", "replacement worker did not complete from the checkpoint")
        restored = h.boot[gen]
        matched = c.class_fingerprint(restored, h.seeded_messages) == seeded_fp
        restored_ctx = RuntimeContextV1.model_validate_json(restored)
        users = [m for m in restored_ctx.messages if m.role == "user"]
        ops = h.ops()
        final_manifest = h.latest_checkpoint()["manifest"]
        with c.objects.open_verified(c.CheckpointManifest.model_validate(final_manifest).context) as source:
            final = RuntimeContextV1.model_validate_json(source.read())
        replayed = len(users) != 1 or users[0].content != c.QUESTION or sum(m.role == "user" for m in final.messages) != 1
        used = (len(ops) == 1 and ops[0]["operation_id"] == before["operation_id"]
                and (ops[0]["result"] or {}).get("ref") == before["ref"] and final.boundary == "final"
                and any(c.SYNTHESIS in (m.content or "") for m in final.messages if m.role == "assistant"))
        attempts_unchanged = h.attempts() == before["attempts"] == 1
        verified = h.verify_checkpoint(final_manifest) == "verified"
        need(matched and not replayed and used and attempts_unchanged and verified,
             "restored context/continuation assertions failed")
        cleanup = h.exact_cleanup()
        c.emit(f"checkpoint-recovery-{cls}", c.proof_doc("checkpoint-recovery", counter,
               {"continuation_used_committed_results": used, "original_prompt_replayed": replayed,
                "restored_workspace_verified": verified, "context_class": cls,
                "seeded_context_hash_matched": matched, "attempts_unchanged": attempts_unchanged},
               {"run_id": str(h.run_id), "seeded_fingerprint": seeded_fp, "baseline": str(base),
                "restored_generation": gen, "restored_boundary": restored_ctx.boundary,
                "restored_pending_assistant_present": restored_ctx.pending_assistant is not None,
                "note": "todo_messages_carry_through proves todo/messages carry-through only; no pending_assistant restore is claimed" if cls == "todo_messages_carry_through" else None,
                "cleanup": cleanup}))


if __name__ == "__main__":
    if sys.argv[1] not in c.SEEDS:
        raise SystemExit("context class must be one of: " + ", ".join(c.SEEDS))
    main(sys.argv[1])
