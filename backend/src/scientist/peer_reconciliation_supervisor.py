"""Host-only scheduling and fencing for bounded, known-ID peer GetTask reads."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from scientist import broker, peer_receipt_runtime, supervisor
from scientist.auth import DomainError
from scientist.contracts import ObjectRef, PlanSpec
from scientist.peer_reconciliation_config import PeerReconciliationTarget


_CLAIM_LOCK = "scientist.supervisor.claim"
_RECOVERY_LEASE_GRACE_MS = 5_000


def tick(db: Session, *, max_active: int, startup: bool = False) -> int:
    """Reap or fence prior reads, then launch eligible known-task reads.

    The returned count is the number of new private dispatch executors started.
    Every attempt reservation and generation/executor record commits before
    `supervisor.start_peer_reconciliation` can touch the owned engine.
    """
    if type(max_active) is not int or not 1 <= max_active <= supervisor._MAX_ACTIVE:
        raise ValueError("max_active must be between one and three")
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:name))"), {"name": _CLAIM_LOCK})
    _reap_attempts(db, startup=startup)
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:name))"), {"name": _CLAIM_LOCK})

    launched = 0
    excluded: set[tuple[object, str]] = set()
    while _active_count(db) < max_active:
        candidate = _next_candidate(db, excluded)
        if candidate is None:
            break
        run_id = candidate["run_id"]
        operation_id = candidate["operation_id"]
        try:
            if not _prove_prior_quiescence(db, run_id, operation_id):
                db.rollback()
                excluded.add((run_id, operation_id))
                continue
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:name))"), {"name": _CLAIM_LOCK})
            run = db.execute(text("""
                SELECT state, waiting_reason, generation, cancel_requested, revision,
                       project_id
                FROM runs WHERE id=:run FOR UPDATE SKIP LOCKED
            """), {"run": run_id}).mappings().one_or_none()
            if (
                run is None or run["state"] != "waiting_input"
                or run["waiting_reason"] != "unknown_outcome"
                or run["cancel_requested"] or _has_unresolved_attempt(db, run_id)
                or _active_count(db) >= max_active
            ):
                db.rollback()
                excluded.add((run_id, operation_id))
                continue

            plan = _approved_plan(db, run_id, run["revision"], run["project_id"])
            receipt = db.execute(text("""
                SELECT release_id FROM peer_outbound_receipts
                WHERE run_id=:run AND operation_id=:operation FOR UPDATE
            """), {"run": run_id, "operation": operation_id}).scalar_one_or_none()
            release = next((item for item in plan.peer_releases if item.release_id == receipt), None)
            if release is None or not release.allow_get_task:
                db.rollback()
                excluded.add((run_id, operation_id))
                continue
            broker.validate_peer_reconciliation(
                db, run_id, run["project_id"], plan, release.release_id,
            )

            attempt = peer_receipt_runtime.consume_reconciliation_attempt(db, run_id, operation_id)
            generation = int(run["generation"]) + 1
            changed = db.execute(text("""
                UPDATE runs
                SET generation=:generation,
                    lease_expires_at=clock_timestamp() + (:lease_ms * interval '1 millisecond')
                WHERE id=:run AND state='waiting_input'
                  AND waiting_reason='unknown_outcome' AND cancel_requested=false
                  AND generation=:previous
            """), {
                "generation": generation,
                "lease_ms": release.timeout_ms + _RECOVERY_LEASE_GRACE_MS,
                "run": run_id, "previous": run["generation"],
            }).rowcount
            if changed != 1:
                raise DomainError("revision_conflict", 409)

            executor_id, incarnation = uuid4(), uuid4()
            db.execute(text("""
                INSERT INTO runtime_executors
                    (id, run_id, generation, kind, operation_id,
                     process_incarnation, state, peer_reconciliation_attempt)
                VALUES (:id, :run, :generation, 'dispatch', :operation,
                        :incarnation, 'starting', :attempt)
            """), {
                "id": executor_id, "run": run_id, "generation": generation,
                "operation": operation_id, "incarnation": incarnation,
                "attempt": attempt,
            })
            db.commit()
            target = PeerReconciliationTarget(operation_id=operation_id, attempt=attempt)
            supervisor.start_peer_reconciliation(
                db, run_id, generation, executor_id, incarnation, target
            )
            launched += 1
        except DomainError:
            db.rollback()
            excluded.add((run_id, operation_id))
            continue
        except Exception:
            db.rollback()
            raise
    return launched


def _active_count(db: Session) -> int:
    return int(db.execute(text("""
        SELECT count(*) FROM runs r
        WHERE r.state IN ('running','recovering','stopping')
           OR EXISTS (
               SELECT 1 FROM runtime_executors e
               WHERE e.run_id=r.id AND e.peer_reconciliation_attempt IS NOT NULL
                 AND e.state <> 'inactive'
           )
    """)).scalar_one())


def _next_candidate(db: Session, excluded: set[tuple[object, str]] | None = None):
    for row in db.execute(text("""
        SELECT r.id AS run_id, o.operation_id, o.generation AS operation_generation,
               r.revision, r.project_id, r.generation,
               receipt.release_id, receipt.reconciliation_attempts, o.result
        FROM runs r
        JOIN operations o ON o.run_id=r.id AND o.kind='peer' AND o.state='unknown'
        JOIN peer_outbound_receipts receipt
          ON receipt.run_id=o.run_id AND receipt.operation_id=o.operation_id
        WHERE r.state='waiting_input' AND r.waiting_reason='unknown_outcome'
          AND r.cancel_requested=false AND receipt.remote_task_id IS NOT NULL
        ORDER BY r.id, o.operation_id
    """)).mappings():
        if excluded and (row["run_id"], row["operation_id"]) in excluded:
            continue
        if _stored_terminal_task(row["result"], row["project_id"]):
            continue
        if _has_unresolved_attempt(db, row["run_id"]):
            continue
        try:
            plan = _approved_plan(db, row["run_id"], row["revision"], row["project_id"])
        except DomainError:
            continue
        release = next((item for item in plan.peer_releases if item.release_id == row["release_id"]), None)
        if (
            release is None or not release.allow_get_task
            or row["reconciliation_attempts"] >= release.reconciliation_limit
        ):
            continue
        return row
    return None


def _stored_terminal_task(result, project_id) -> bool:
    if not isinstance(result, dict) or result.get("peer_task_terminal") is not True:
        return False
    try:
        reference = ObjectRef.model_validate(result.get("ref"))
    except Exception:
        return False
    return reference.project_id == project_id


def _approved_plan(db: Session, run_id, revision: int, project_id) -> PlanSpec:
    row = db.execute(text("""
        SELECT p.plan FROM plan_revisions p JOIN runs r
          ON r.id=p.run_id AND r.revision=p.revision AND r.project_id=p.project_id
        JOIN approvals a ON a.run_id=p.run_id AND a.revision=p.revision
          AND a.project_id=p.project_id AND a.plan_digest=r.plan_digest
        WHERE p.run_id=:run AND p.revision=:revision AND p.project_id=:project
    """), {"run": run_id, "revision": revision, "project": project_id}).scalar_one_or_none()
    if row is None:
        raise DomainError("peer_release_unapproved", 409)
    return PlanSpec.model_validate(row)


def _has_unresolved_attempt(db: Session, run_id) -> bool:
    return db.execute(text("""
        SELECT 1 FROM runtime_executors
        WHERE run_id=:run AND peer_reconciliation_attempt IS NOT NULL
          AND state <> 'inactive'
        LIMIT 1
    """), {"run": run_id}).scalar_one_or_none() is not None


def _prove_prior_quiescence(db: Session, run_id, operation_id: str) -> bool:
    """Fence the original generation and every earlier read before a new send."""
    operation = db.execute(text("""
        SELECT generation FROM operations WHERE run_id=:run AND operation_id=:operation
          AND kind='peer' AND state='unknown' FOR UPDATE
    """), {"run": run_id, "operation": operation_id}).one_or_none()
    if operation is None:
        return False
    binding = db.execute(text("""
        SELECT generation, executor_id FROM operation_executors
        WHERE run_id=:run AND operation_id=:operation
    """), {"run": run_id, "operation": operation_id}).one_or_none()
    if binding is None or binding.generation != operation.generation:
        return False

    generations = db.execute(text("""
        SELECT DISTINCT generation FROM runtime_executors
        WHERE run_id=:run AND (
            generation=:original OR
            peer_reconciliation_attempt IS NOT NULL
        ) ORDER BY generation
    """), {"run": run_id, "original": operation.generation}).scalars().all()
    if operation.generation not in generations:
        return False
    for generation in generations:
        if not supervisor._fence_generation(db, run_id, generation, 0):
            return False
        rows = db.execute(text("""
            SELECT * FROM runtime_executors
            WHERE run_id=:run AND generation=:generation
            ORDER BY id
        """), {"run": run_id, "generation": generation}).mappings().all()
        if any(row["state"] != "inactive" or not supervisor._inactive_executor_proven(row) for row in rows):
            return False
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('scientist.supervisor.claim'))"))
    current = db.execute(text("""
        SELECT state, waiting_reason, cancel_requested FROM runs
        WHERE id=:run FOR UPDATE
    """), {"run": run_id}).one_or_none()
    return bool(
        current is not None and current.state == "waiting_input"
        and current.waiting_reason == "unknown_outcome" and not current.cancel_requested
    )


def _reap_attempts(db: Session, *, startup: bool) -> None:
    rows = db.execute(text("""
        SELECT r.state AS run_state, r.generation AS run_generation,
               r.cancel_requested, r.lease_expires_at,
               e.*
        FROM runtime_executors e JOIN runs r ON r.id=e.run_id
        WHERE e.peer_reconciliation_attempt IS NOT NULL AND e.state <> 'inactive'
        ORDER BY r.id, e.generation, e.peer_reconciliation_attempt
        FOR UPDATE OF r, e
    """)).mappings().all()
    if not rows:
        return
    cfg = supervisor._require_config()
    for row in rows:
        expired = row["lease_expires_at"] is not None and row["lease_expires_at"] <= datetime.now(timezone.utc)
        must_stop = (
            startup or row["cancel_requested"] or expired
            or row["run_state"] in {"completed", "failed", "canceled", "rejected"}
            or row["run_generation"] != row["generation"]
        )
        if (
            not must_stop and row["state"] in {"starting", "active"}
            and not row["peer_reconciliation_started"]
        ):
            continue
        ref = None
        try:
            ref = cfg.dispatch.find(
                db, row["run_id"], row["generation"], row["id"],
                row["operation_id"], row["process_incarnation"],
                peer_reconciliation=PeerReconciliationTarget(
                    operation_id=row["operation_id"],
                    attempt=row["peer_reconciliation_attempt"],
                ),
            )
        except Exception:
            _unknown(db, row["id"])
            continue

        if ref is None:
            if _prove_exact_absence(db, cfg.dispatch, row):
                _clear_recovery_lease(db, row, canceled=row["cancel_requested"])
            else:
                _unknown(db, row["id"])
            continue
        if not supervisor._matches_executor(row, ref):
            _unknown(db, row["id"])
            continue

        if must_stop:
            try:
                cfg.dispatch.stop(db, ref, 0)
            except Exception:
                pass
        if supervisor._dispatch_operation_is_inactive(db, cfg.dispatch, ref, row["operation_id"]):
            proof = {
                "source": "owned-engine-exact-container",
                "engine_id": ref.engine_id, "container_id": ref.container_id,
                "executor_id": str(ref.executor_id),
                "process_incarnation": str(ref.process_incarnation),
                "peer_reconciliation_attempt": row["peer_reconciliation_attempt"],
                "stopped_at": datetime.now(timezone.utc).isoformat(),
            }
            supervisor._record_dispatch_inactive(
                db, cfg.dispatch, ref.executor_id, ref, [], proof
            )
            _clear_recovery_lease(db, row, canceled=row["cancel_requested"])
        elif must_stop:
            _unknown(db, row["id"])


def _prove_exact_absence(db: Session, dispatch, row) -> bool:
    """A complete owned-label lookup may prove no launch object exists."""
    if row["container_id"] is not None or row["engine_id"] is not None:
        try:
            if not row["engine_id"] or dispatch.engine_id() != row["engine_id"]:
                return False
        except Exception:
            return False
    if row["container_id"] is not None:
        proof = {
            "source": "owned-engine-exact-container",
            "engine_id": row["engine_id"], "container_id": row["container_id"],
            "executor_id": str(row["id"]),
            "process_incarnation": str(row["process_incarnation"]),
            "peer_reconciliation_attempt": row["peer_reconciliation_attempt"],
            "stopped_at": datetime.now(timezone.utc).isoformat(),
        }
    elif row["engine_id"] is not None:
        proof = {
            "source": "owned-engine-exact-dispatch-absence",
            "engine_id": row["engine_id"], "executor_id": str(row["id"]),
            "process_incarnation": str(row["process_incarnation"]),
            "peer_reconciliation_attempt": row["peer_reconciliation_attempt"],
            "stopped_at": datetime.now(timezone.utc).isoformat(),
        }
    else:
        proof = {
            "source": "dispatch-launch-not-attempted",
            "run_id": str(row["run_id"]), "generation": row["generation"],
            "executor_id": str(row["id"]),
            "process_incarnation": str(row["process_incarnation"]),
        }
    changed = db.execute(text("""
        UPDATE runtime_executors
        SET state='inactive', proof=CAST(:proof AS jsonb), updated_at=now()
        WHERE id=:id AND run_id=:run AND generation=:generation
          AND peer_reconciliation_attempt=:attempt AND state <> 'inactive'
          AND container_id IS NOT DISTINCT FROM :container
          AND engine_id IS NOT DISTINCT FROM :engine
    """), {
        "proof": json.dumps(proof, sort_keys=True), "id": row["id"],
        "run": row["run_id"], "generation": row["generation"],
        "attempt": row["peer_reconciliation_attempt"],
        "container": row["container_id"], "engine": row["engine_id"],
    }).rowcount
    return changed == 1


def _unknown(db: Session, executor_id) -> None:
    db.execute(text("""
        UPDATE runtime_executors SET state='unknown', updated_at=now()
        WHERE id=:id AND state <> 'inactive'
    """), {"id": executor_id})


def _clear_recovery_lease(db: Session, row, *, canceled: bool) -> None:
    current = db.execute(text("""
        SELECT state, generation, revision, cancel_requested
        FROM runs WHERE id=:run FOR UPDATE
    """), {"run": row["run_id"]}).mappings().one_or_none()
    if current is None or current["generation"] != row["generation"]:
        return
    if canceled and current["state"] == "waiting_input" and current["cancel_requested"]:
        db.execute(text("""
            UPDATE runs SET state='canceled', waiting_reason=NULL, lease_expires_at=NULL
            WHERE id=:run AND generation=:generation AND cancel_requested=true
              AND state='waiting_input'
        """), {"run": row["run_id"], "generation": row["generation"]})
        supervisor._event(db, row["run_id"], current["revision"], "run.state", {"state": "canceled"})
        supervisor._issue_unknown_decisions(db, row["run_id"], current["revision"], canceled=True)
    else:
        db.execute(text("""
            UPDATE runs SET lease_expires_at=NULL
            WHERE id=:run AND generation=:generation AND state='waiting_input'
        """), {"run": row["run_id"], "generation": row["generation"]})
    db.commit()
