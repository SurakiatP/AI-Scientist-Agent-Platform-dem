"""Durable operation-to-physical-executor binding before outbound transport."""

from collections.abc import Callable
from dataclasses import replace
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


    def reconcile_peer(self, run_id: UUID, generation: int, target):
        from scientist import broker
        with session() as db:
            scope = bind_peer_reconciliation(db, self.executor_id, self.incarnation,
                                             run_id, generation, target)
            db.commit()
        return broker.http_reconcile_peer(scope, generation)


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


def bind_peer_reconciliation(db: Session, executor_id: UUID, incarnation: UUID,
                             run_id: UUID, generation: int, target):
    """Claim one read by an exact fresh executor while preserving the original binding."""
    from scientist.peer_reconciliation_scope import load_peer_read_scope
    scope = load_peer_read_scope(db,run_id,target.operation_id,generation,target.attempt)
    executor = db.execute(text('SELECT * FROM runtime_executors WHERE id=:id FOR UPDATE'),{'id':executor_id}).one_or_none()
    if (executor is None or executor.kind!='dispatch' or executor.run_id!=run_id or
            executor.generation!=generation or executor.process_incarnation!=incarnation or
            executor.state!='active' or not executor.container_id or not executor.engine_id or
            executor.operation_id!=target.operation_id or executor.peer_reconciliation_attempt!=target.attempt or
            executor.peer_reconciliation_started):
        raise DomainError('forbidden',403)
    others = db.execute(text("SELECT 1 FROM runtime_executors WHERE run_id=:run AND id<>:id AND state<>'inactive' LIMIT 1"),
                        {'run':run_id,'id':executor_id}).scalar_one_or_none()
    if others is not None:
        raise DomainError('forbidden',403)
    claimed = db.execute(text('UPDATE runtime_executors SET peer_reconciliation_started=true WHERE id=:id AND NOT peer_reconciliation_started'),
                         {'id':executor_id}).rowcount
    if claimed!=1:
        raise DomainError('forbidden',403)
    return replace(scope, reader_executor_id=executor_id, reader_incarnation=incarnation)


def validate_peer_reader(db: Session, scope, generation: int) -> None:
    """Recheck the exact reader after the response, before any durable result."""
    executor = db.execute(text("SELECT * FROM runtime_executors WHERE id=:id FOR UPDATE"),
                          {"id": scope.reader_executor_id}).one_or_none()
    if (executor is None or executor.kind != "dispatch" or
            executor.run_id != scope.request.run_id or executor.generation != generation or
            executor.process_incarnation != scope.reader_incarnation or
            executor.state != "active" or not executor.container_id or not executor.engine_id or
            executor.operation_id != scope.request.operation_id or
            executor.peer_reconciliation_attempt != scope.attempt or
            not executor.peer_reconciliation_started):
        raise DomainError("forbidden", 403)
    other = db.execute(text("SELECT 1 FROM runtime_executors WHERE run_id=:run AND id<>:id AND state<>'inactive' LIMIT 1"),
                       {"run": scope.request.run_id, "id": scope.reader_executor_id}).scalar_one_or_none()
    if other is not None:
        raise DomainError("forbidden", 403)
