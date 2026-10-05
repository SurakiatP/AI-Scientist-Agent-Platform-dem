"""Revalidate a bounded peer read from immutable server-owned records."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from scientist.auth import DomainError
from scientist.contracts import OperationRequest

if TYPE_CHECKING:
    from scientist.broker import DispatchTarget


@dataclass(frozen=True)
class PeerReadScope:
    request: OperationRequest
    target: DispatchTarget
    remote_task_id: str
    remote_context_id: str | None
    attempt: int
    reader_executor_id: UUID | None = None
    reader_incarnation: UUID | None = None


def load_peer_read_scope(db: Session, run_id: UUID, operation_id: str,
                         generation: int, attempt: int) -> PeerReadScope:
    from scientist import broker

    run = broker._load_run(db, run_id, lock=True)
    if (run.generation != generation or run.state != "waiting_input" or
            run.waiting_reason != "unknown_outcome" or run.cancel_requested):
        raise DomainError("forbidden", 403)
    operation = db.execute(text("SELECT * FROM operations WHERE run_id=:run AND operation_id=:op FOR UPDATE"),
                           {"run": run_id, "op": operation_id}).one_or_none()
    if operation is None or operation.kind != "peer" or operation.state != "unknown":
        raise DomainError("forbidden", 403)
    try:
        request = OperationRequest.model_validate((operation.result or {})["request"])
    except (KeyError, ValueError) as exc:
        raise DomainError("storage_unavailable", 503) from exc
    if (request.run_id != run_id or request.operation_id != operation_id or request.kind != "peer" or
            request.generation != operation.generation or request.reserve_tokens != operation.reserve_tokens or
            operation.payload_hash.strip() != broker._fingerprint(request) or request.generation >= generation):
        raise DomainError("forbidden", 403)
    approved = db.execute(text("SELECT 1 FROM approvals WHERE run_id=:run AND project_id=:project AND revision=:revision AND plan_digest=:digest"),
                          {"run":run_id,"project":run.project_id,"revision":run.revision,"digest":run.plan_digest}).scalar_one_or_none()
    if approved is None:
        raise DomainError("forbidden", 403)
    plan = broker._load_plan(db,run_id,run.revision)
    target = broker._validate_scope(db,run.project_id,plan,request)
    release = target.peer_release
    receipt = db.execute(text("SELECT * FROM peer_outbound_receipts WHERE run_id=:run AND operation_id=:op FOR UPDATE"),
                         {"run":run_id,"op":operation_id}).one_or_none()
    if (release is None or receipt is None or not release.allow_get_task or
            isinstance(attempt,bool) or not isinstance(attempt,int) or not 1 <= attempt <= release.reconciliation_limit or
            receipt.reconciliation_attempts != attempt or receipt.project_id != run.project_id or
            receipt.release_id != release.release_id or receipt.peer_id != release.peer_id or
            receipt.message_id != release.message_id or
            receipt.endpoint_fingerprint.strip() != release.endpoint_fingerprint.lower() or
            receipt.parameters_sha256.strip() != release.parameters_sha256.lower() or
            not receipt.remote_task_id or receipt.remote_task_id != receipt.remote_task_id.strip() or
            (receipt.remote_context_id is not None and (not receipt.remote_context_id or receipt.remote_context_id != receipt.remote_context_id.strip()))):
        raise DomainError("forbidden", 403)
    original = db.execute(text("SELECT e.* FROM operation_executors b JOIN runtime_executors e ON e.id=b.executor_id WHERE b.run_id=:run AND b.operation_id=:op AND b.generation=:generation"),
                          {"run":run_id,"op":operation_id,"generation":request.generation}).mappings().one_or_none()
    if original is None or original["state"] != "inactive":
        raise DomainError("forbidden", 403)
    proof = original["proof"] or {}
    if (proof.get("source") not in {"owned-engine-exact-container","owned-engine-generation-fence"} or
            not original["container_id"] or not original["engine_id"] or
            proof.get("container_id") != original["container_id"] or proof.get("engine_id") != original["engine_id"]):
        raise DomainError("forbidden", 403)
    return PeerReadScope(request,target,receipt.remote_task_id,receipt.remote_context_id,attempt)
