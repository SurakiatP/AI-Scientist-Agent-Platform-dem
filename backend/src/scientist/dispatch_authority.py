"""Durable operation-to-physical-executor binding before outbound transport."""

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from scientist.auth import DomainError
from scientist.broker import DispatchTarget, Transport
from scientist.contracts import OperationRequest
from scientist.db import session
from scientist.runtime_contracts import operation_fingerprint

DeathProbe = Callable[[UUID, str, str], bool]


def bind_dispatch(db: Session, executor_id: UUID, incarnation: UUID, request: OperationRequest) -> None:
    executor = db.execute(text("SELECT * FROM runtime_executors WHERE id=:id FOR UPDATE"),
                          {"id":executor_id}).one_or_none()
    operation = db.execute(text("SELECT * FROM operations WHERE run_id=:run AND operation_id=:op FOR UPDATE"),
                           {"run":request.run_id,"op":request.operation_id}).one_or_none()
    if (executor is None or operation is None or executor.kind != "dispatch"
            or executor.state != "active" or not executor.container_id or not executor.engine_id
            or executor.process_incarnation != incarnation or executor.run_id != request.run_id
            or executor.generation != request.generation or operation.generation != request.generation
            or operation.state != "reserved" or operation.kind != request.kind
            or operation.reserve_tokens != request.reserve_tokens
            or operation.payload_hash.strip() != operation_fingerprint(request)):
        raise DomainError("forbidden", 403)
    prior = db.execute(text("SELECT executor_id,generation FROM operation_executors WHERE run_id=:run AND operation_id=:op"),
                       {"run":request.run_id,"op":request.operation_id}).one_or_none()
    if prior is not None:
        if prior.executor_id != executor_id or prior.generation != request.generation:
            raise DomainError("forbidden", 403)
        return
    db.execute(text("INSERT INTO operation_executors(run_id,operation_id,generation,executor_id) "
                    "VALUES (:run,:op,:generation,:executor)"),
               {"run":request.run_id,"op":request.operation_id,"generation":request.generation,"executor":executor_id})


class BoundDispatchTransport:
    def __init__(self, executor_id: UUID, incarnation: UUID, transport: Transport):
        self.executor_id = executor_id
        self.incarnation = incarnation
        self.transport = transport

    def __call__(self, request: OperationRequest, target: DispatchTarget) -> tuple[bytes, int]:
        with session() as db:
            bind_dispatch(db, self.executor_id, self.incarnation, request)
            db.commit()  # A restart can find this binding before any outbound byte.
        return self.transport(request, target)


def dispatch_is_inactive(db: Session, run_id: UUID, operation_id: str, generation: int,
                         *, probe: DeathProbe | None = None) -> bool:
    """Require exact operation binding and fresh trusted physical death evidence.

    A stopped run-wide container, lease timestamp or saved proof alone is
    insufficient. The probe must establish death of this exact incarnation on
    this exact engine/container; exceptions and unknown inspection fail closed.
    Local inactivity never proves the remote outcome or releases reservation.
    """
    if probe is None:
        return False
    row = db.execute(text("SELECT e.* FROM operation_executors o JOIN runtime_executors e "
                          "ON e.id=o.executor_id AND e.run_id=o.run_id AND e.generation=o.generation "
                          "WHERE o.run_id=:run AND o.operation_id=:op AND o.generation=:generation"),
                     {"run":run_id,"op":operation_id,"generation":generation}).one_or_none()
    if (row is None or row.kind != "dispatch" or row.state != "inactive"
            or not row.container_id or not row.engine_id):
        return False
    try:
        return probe(row.process_incarnation, row.engine_id, row.container_id) is True
    except Exception:
        return False
