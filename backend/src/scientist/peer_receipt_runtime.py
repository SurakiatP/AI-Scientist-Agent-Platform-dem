"""Durable runtime operations for outbound peer receipts."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from scientist import db, domain
from scientist.auth import DomainError
from scientist.contracts import PlanSpec


def prepare_submission(run_id: UUID, operation_id: str, release_id: UUID) -> bool:
    """Commit the approved peer receipt before any network request is made.

    A repeated call for the same operation and release is idempotent and returns
    False. A conflicting release remains a domain conflict.
    """
    release_id = UUID(str(release_id))
    with db.session() as session:
        # The domain function also takes this lock. Taking it here first makes
        # the receipt lookup below part of the same serialized run transaction.
        session.execute(
            text("SELECT id FROM runs WHERE id=:run FOR UPDATE"), {"run": run_id}
        ).scalar_one_or_none()
        prior = session.execute(
            text(
                "SELECT release_id FROM peer_outbound_receipts "
                "WHERE run_id=:run AND operation_id=:operation FOR UPDATE"
            ),
            {"run": run_id, "operation": operation_id},
        ).scalar_one_or_none()
        # Revalidate through the domain even on replay; it owns approved-release
        # conflict checks and the remaining dispatchability rules.
        domain.prepare_peer_receipt(session, run_id, operation_id, release_id)
        session.commit()
        return prior is None


def record_remote_identity(
    run_id: UUID,
    operation_id: str,
    task_id: str | None,
    context_id: str | None,
) -> None:
    """Persist a returned peer identity before subsequent transport work."""
    for value in (task_id, context_id):
        if value is not None and (not value or value != value.strip()):
            raise DomainError("peer_identity_invalid", 400)
    with db.session() as session:
        domain.record_peer_remote_identity(session, run_id, operation_id, task_id, context_id)
        session.commit()


def mark_unknown(run_id: UUID, operation_id: str, reason: str) -> None:
    """Commit the unknown outcome without storing or logging diagnostic text."""
    del reason
    with db.session() as session:
        domain.mark_peer_receipt_unknown(session, run_id, operation_id)
        session.commit()


def consume_reconciliation_attempt(
    session: Session, run_id: UUID, operation_id: str
) -> int:
    """Consume one caller-transaction attempt for an approved known task ID.

    The caller owns the transaction and must commit or roll it back. Row locks
    serialize concurrent callers, including those racing for the final slot.
    """
    run = session.execute(
        text("SELECT id, state, cancel_requested, revision, project_id, plan_digest "
             "FROM runs WHERE id=:run FOR UPDATE"),
        {"run": run_id},
    ).mappings().one_or_none()
    if run is None:
        raise DomainError("not_found", 404)
    if run["state"] in {"completed", "failed", "canceled", "rejected"} or run["cancel_requested"]:
        raise DomainError("peer_reconciliation_unavailable", 409)

    operation = session.execute(
        text("SELECT kind FROM operations WHERE run_id=:run AND operation_id=:operation FOR UPDATE"),
        {"run": run_id, "operation": operation_id},
    ).mappings().one_or_none()
    if operation is None or operation["kind"] != "peer":
        raise DomainError("peer_operation_missing", 409)

    receipt = session.execute(
        text("""SELECT release_id, peer_id, message_id, remote_task_id,
                         reconciliation_attempts
                  FROM peer_outbound_receipts
                  WHERE run_id=:run AND operation_id=:operation FOR UPDATE"""),
        {"run": run_id, "operation": operation_id},
    ).mappings().one_or_none()
    if receipt is None:
        raise DomainError("peer_receipt_missing", 409)
    if not receipt["remote_task_id"]:
        raise DomainError("peer_reconciliation_unavailable", 409)

    plan_record = session.execute(
        text("SELECT plan FROM plan_revisions WHERE run_id=:run AND revision=:revision "
             "AND project_id=:project"),
        {"run": run_id, "revision": run["revision"], "project": run["project_id"]},
    ).scalar_one_or_none()
    approved = session.execute(
        text("SELECT 1 FROM approvals WHERE run_id=:run AND revision=:revision "
             "AND project_id=:project AND plan_digest=:digest"),
        {
            "run": run_id,
            "revision": run["revision"],
            "project": run["project_id"],
            "digest": run["plan_digest"],
        },
    ).scalar_one_or_none()
    if plan_record is None or approved is None:
        raise DomainError("peer_release_unapproved", 409)

    plan = PlanSpec.model_validate(plan_record)
    domain._validate_peer_releases(session, run_id, run["project_id"], plan)
    release = next((item for item in plan.peer_releases if item.release_id == receipt["release_id"]), None)
    if (
        release is None
        or release.peer_id != receipt["peer_id"]
        or release.message_id != receipt["message_id"]
    ):
        raise DomainError("peer_release_unapproved", 409)
    if not release.allow_get_task:
        raise DomainError("peer_reconciliation_unavailable", 409)
    if receipt["reconciliation_attempts"] >= release.reconciliation_limit:
        raise DomainError("peer_reconciliation_limit", 409)

    attempt = session.execute(
        text("""UPDATE peer_outbound_receipts
                  SET reconciliation_attempts=reconciliation_attempts + 1
                  WHERE run_id=:run AND operation_id=:operation
                  RETURNING reconciliation_attempts"""),
        {"run": run_id, "operation": operation_id},
    ).scalar_one()
    return int(attempt)
