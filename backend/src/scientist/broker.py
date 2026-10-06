"""Durable, capability-scoped journal for approved outbound research effects."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import socket
import ssl
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Timer
from typing import Callable, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from scientist.auth import DomainError
from scientist.contracts import (CsvDescribeGrantV1, CrossrefQueryV1, ObjectRef, OperationRequest, OperationResult, PeerReleaseSpec, PlanSpec, Principal, RunView, ScientificBindingV2, canonical_peer_parameters_bytes)
from scientist.domain import _event, _run_view
from scientist import limits
from scientist.model_payload import ModelPayloadError, build_chat_completion_body, llm_input_reserve
from scientist.secrets import read_secret

@dataclass(frozen=True)
class DispatchTarget:
    kind: str
    url: str
    approved_recipients: tuple[str, ...]
    credential_id: UUID | None = None
    expected_sha256: str | None = None
    model: str | None = None
    peer_release: PeerReleaseSpec | None = None


@dataclass(frozen=True)
class UnknownOperationContext:
    run_id: UUID
    project_id: UUID
    operation_id: str
    generation: int
    kind: str
    payload_hash: str
    request: OperationRequest


Transport = Callable[[OperationRequest, DispatchTarget], tuple[bytes, int | None]]
PersistResult = Callable[[Session, UUID, bytes, str], ObjectRef]
Resolver = Callable[[str, int], list[str]]
ResultVerifier = Callable[[UnknownOperationContext, ObjectRef], bool]
DispatchInactivityProof = Callable[[UUID, str, int], bool]

_transport: Transport | None = None
_persist_result: PersistResult | None = None
_capability_key: bytes | None = None
_resolver: Resolver = lambda host, port: [item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
_peer_destinations: dict[str, str] = {}
_provider_destinations: dict[str, str] = {}
_dispatch_is_inactive: DispatchInactivityProof | None = None
_dispatch_inactivity_proof: DispatchInactivityProof | None = None  # host-registered; survives configure()
_inactive_dispatches: set[tuple[UUID, str, int]] = set()
_active_operations: set[tuple[UUID, str, int]] = set()
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_REQUEST_BYTES = 256 * 1024
_MAX_TIMEOUT_SECONDS = 20
_MAX_HTTP_TOTAL_SECONDS = 20.0
_COMPUTE_WAIT_SECONDS = 60.0
_COMPUTE_POLL_SECONDS = 0.1
_LLM_CONTROLS = {
    "temperature", "top_p", "stop", "presence_penalty", "frequency_penalty", "seed",
    "parallel_tool_calls", "logprobs", "top_logprobs", "tools", "tool_choice",
}
_METADATA_HOSTS = {"metadata.google.internal", "metadata.google", "metadata.azure.internal", "instance-data"}
_METADATA_ADDRESSES = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("100.100.100.200"),
                       ipaddress.ip_address("168.63.129.16"), ipaddress.ip_address("fd00:ec2::254")}
_active_session: ContextVar[Session] = ContextVar("broker_db_session")
_verify_result: ResultVerifier | None = None


def configure(
    *,
    transport: Transport | None = None,
    persist_result: PersistResult | None = None,
    capability_key: bytes | None = None,
    resolver: Resolver | None = None,
    peer_destinations: dict[str, str] | None = None,
    provider_destinations: dict[str, str] | None = None,
    dispatch_is_inactive: DispatchInactivityProof | None = None,
) -> None:
    """Inject trusted runtime dependencies; workers cannot configure these values."""
    global _transport, _persist_result, _capability_key, _resolver, _peer_destinations, _provider_destinations
    global _dispatch_is_inactive
    _transport, _persist_result, _capability_key = transport, persist_result, capability_key
    _resolver = resolver or (lambda host, port: [item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)])
    _peer_destinations = dict(peer_destinations or {})
    _provider_destinations = dict(provider_destinations or {})
    _dispatch_is_inactive = dispatch_is_inactive


def configure_dispatch_inactivity(proof: DispatchInactivityProof | None) -> None:
    """Register the host's exact dispatch-inactivity proof; configure() never resets it."""
    global _dispatch_inactivity_proof
    _dispatch_inactivity_proof = proof


def issue_capability(db: Session, run_id: UUID, generation: int, ttl_seconds: int) -> str:
    if not 1 <= ttl_seconds <= 900 or generation < 0:
        raise DomainError("forbidden", 400)
    row = _load_run(db, run_id)
    if row.generation != generation or row.state != "running":
        raise DomainError("forbidden", 403)
    if row.lease_expires_at is None or row.lease_expires_at <= datetime.now(timezone.utc):
        raise DomainError("forbidden", 403)
    approved = db.execute(text("SELECT 1 FROM approvals WHERE run_id = :run AND revision = :revision AND plan_digest = :digest"),
                          {"run": run_id, "revision": row.revision, "digest": row.plan_digest.strip()}).scalar_one_or_none()
    if approved is None:
        raise DomainError("forbidden", 403)
    _load_plan(db, run_id, row.revision)
    return _sign_capability({
        "run_id": str(run_id),
        "generation": generation,
        "revision": row.revision,
        "plan_digest": row.plan_digest.strip(),
        "exp": int((datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).timestamp()),
    })


def execute(db: Session, worker_capability: str, request: OperationRequest) -> OperationResult:
    claims = _verify_capability(worker_capability)
    if claims["run_id"] != str(request.run_id) or claims["generation"] != request.generation:
        raise DomainError("forbidden", 403)
    fingerprint = _fingerprint(request)
    row = _load_run(db, request.run_id, lock=True)
    _check_capability_claims(row, claims)
    plan = _load_plan(db, request.run_id, row.revision)
    target = _validate_scope(db, row.project_id, plan, request)

    existing = db.execute(text("SELECT * FROM operations WHERE run_id = :run AND operation_id = :operation FOR UPDATE"),
                          {"run": request.run_id, "operation": request.operation_id}).one_or_none()
    if existing is not None:
        if existing.payload_hash.strip() != fingerprint:
            raise DomainError("idempotency_conflict", 409)
        pending = existing.result or {}
        if existing.state == "unknown" and pending.get("usage_known") and pending.get("ref"):
            return _finalize_staged_success(db, request.run_id, request.operation_id)
        if existing.state == "unknown" and pending.get("retry_identity"):
            return _continue_owner_retry(db, row, existing, request)
        return _operation_result(existing)
    if limits.budget_exhausted(db, row, reserve_tokens=request.reserve_tokens):
        decision_id = limits.mark_budget_wait(db, request.run_id, row.revision, _event, reserve_tokens=request.reserve_tokens)
        if decision_id is None:
            db.rollback()
            raise DomainError("forbidden", 403)
        db.commit()
        raise DomainError("budget_exhausted", 409)

    reserved = db.execute(text("""
        UPDATE runs SET reserved_tokens = reserved_tokens + :reserve
        WHERE id = :run AND generation = :generation
          AND state = 'running' AND lease_expires_at > now()
          AND usage_tokens + reserved_tokens + :reserve <= token_limit
          AND elapsed_used_ms + CASE
                WHEN elapsed_active_since IS NULL THEN 0
                ELSE GREATEST(0, FLOOR(EXTRACT(EPOCH FROM
                    (clock_timestamp() - elapsed_active_since)) * 1000)::bigint)
              END < elapsed_limit_ms
          RETURNING id
    """), {"run": request.run_id, "generation": request.generation, "reserve": request.reserve_tokens}).scalar_one_or_none()
    if reserved is None:
        current = _load_run(db, request.run_id)
        if current.generation != request.generation or current.state != "running" or current.lease_expires_at is None or current.lease_expires_at <= datetime.now(timezone.utc):
            raise DomainError("forbidden", 403)
        if limits.budget_exhausted(db, current, reserve_tokens=request.reserve_tokens):
            decision_id = limits.mark_budget_wait(db, request.run_id, current.revision, _event, reserve_tokens=request.reserve_tokens)
            if decision_id is not None:
                db.commit()
                raise DomainError("budget_exhausted", 409)
            db.rollback()
            raise DomainError("forbidden", 403)
        # No decision can be recorded here, so a 409 would leave the adapter waiting forever.
        raise DomainError("forbidden", 403)
    db.execute(text("""
        INSERT INTO operations (id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens, result)
        VALUES (:id, :run, :operation, :generation, :kind, :hash, 'reserved', :reserve, CAST(:result AS jsonb))
    """), {
        "id": uuid4(), "run": request.run_id, "operation": request.operation_id,
        "generation": request.generation, "kind": request.kind, "hash": fingerprint,
        "reserve": request.reserve_tokens,
        "result": json.dumps({"request": request.model_dump(mode="json")}, separators=(",", ":")),
    })
    identity = (request.run_id, request.operation_id, request.generation)
    _active_operations.add(identity)
    try:
        db.commit()  # Both the operation identity and budget reservation precede dispatch.
        result = _finish_reserved_operation(db, request, row.revision, target)
    except Exception:
        _inactive_dispatches.add(identity)
        raise
    else:
        _inactive_dispatches.add(identity)
        return result
    finally:
        _active_operations.discard(identity)


def effective_operation(db: Session, run_id: UUID, operation_id: str):
    """Follow owner retry links from operation_id; return (last existing row, next retry id or None).

    The second element is set only when the last row carries a retry decision whose row is not yet created.
    """
    seen: set[str] = set()
    row = db.execute(text("SELECT * FROM operations WHERE run_id = :run AND operation_id = :op"),
                     {"run": run_id, "op": operation_id}).one_or_none()
    while row is not None:
        seen.add(row.operation_id)
        retry_id = (row.result or {}).get("retry_identity") if row.state == "unknown" else None
        if not retry_id or retry_id in seen:
            return row, None
        nxt = db.execute(text("SELECT * FROM operations WHERE run_id = :run AND operation_id = :op"),
                         {"run": run_id, "op": retry_id}).one_or_none()
        if nxt is None:
            return row, retry_id
        row = nxt
    return None, None


def _continue_owner_retry(db: Session, run, original, request: OperationRequest) -> OperationResult:
    """Replay of an unknown operation the owner chose to retry: send the last stored retry identity once.

    The caller holds the run lock (held through the nested execute, which commits the retry row and its
    reservation before dispatch), so a concurrent or later replay finds that row and sends nothing.
    The original stays unknown with its reservation retained; the outcome is returned under its operation_id.
    """
    tip, next_id = effective_operation(db, run.id, original.operation_id)
    if next_id is not None:
        try:
            stored = OperationRequest.model_validate((tip.result or {})["retry_request"])
        except (KeyError, ValueError) as exc:
            raise DomainError("storage_unavailable", 503) from exc
        if stored.operation_id != next_id or stored.model_copy(
                update={"operation_id": request.operation_id, "generation": request.generation}) != request:
            raise DomainError("storage_unavailable", 503)
        execute(db, issue_capability(db, run.id, request.generation, 300),
                stored.model_copy(update={"generation": request.generation}))
        db.execute(text("""
            UPDATE operations SET result = result || jsonb_build_object('retry_of', CAST(:original AS text))
            WHERE run_id = :run AND operation_id = :retry
        """), {"original": tip.operation_id, "run": run.id, "retry": next_id})
        db.commit()
        tip, _ = effective_operation(db, run.id, next_id)
    staged = tip.result or {}
    if tip.state == "unknown" and staged.get("usage_known") and staged.get("ref"):
        return _finalize_staged_success(db, run.id, tip.operation_id).model_copy(
            update={"operation_id": original.operation_id})
    return _operation_result(tip).model_copy(update={"operation_id": original.operation_id})


def _finish_reserved_operation(
    db: Session, request: OperationRequest, revision: int, target: DispatchTarget
) -> OperationResult:
    if request.kind == "compute":
        return _wait_for_compute_result(db, request, revision)
    db_token = _active_session.set(db)
    try:
        data, usage_tokens = _dispatch(request, target)
    except Exception:
        return _record_unknown(db, request, revision, usage_tokens=None, result_ref=None)
    finally:
        _active_session.reset(db_token)
    if len(data) > _MAX_RESPONSE_BYTES or (
        usage_tokens is not None
        and (isinstance(usage_tokens, bool) or not isinstance(usage_tokens, int) or usage_tokens < 0)
    ):
        return _record_unknown(db, request, revision, usage_tokens=None, result_ref=None)
    if target.expected_sha256 is not None and hashlib.sha256(data).hexdigest() != target.expected_sha256:
        return _record_unknown(db, request, revision, usage_tokens=usage_tokens, result_ref=None)
    try:
        result_ref = _persist(db, request.run_id, data, "application/octet-stream")
    except Exception:
        db.rollback()
        return _record_unknown(db, request, revision, usage_tokens=usage_tokens, result_ref=None)
    peer_terminal = None
    if target.kind == "peer":
        try:
            task_state = json.loads(data).get("status", {}).get("state")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            return _record_unknown(db, request, revision, usage_tokens=None, result_ref=result_ref)
        peer_terminal = task_state is None or task_state in {
            "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"
        }
    if usage_tokens is None or peer_terminal is False:
        return _record_unknown(
            db, request, revision, usage_tokens=None, result_ref=result_ref,
            peer_task_terminal=peer_terminal,
        )
    return record_verified_completion(
        db, request, revision, usage_tokens=usage_tokens, result_ref=result_ref,
        peer_task_terminal=peer_terminal,
    )


def _wait_for_compute_result(
    db: Session, request: OperationRequest, revision: int,
) -> OperationResult:
    """Observe the host-owned compute journal; this path never launches or uploads."""
    deadline = time.monotonic() + _COMPUTE_WAIT_SECONDS
    while True:
        row = db.execute(
            text("SELECT * FROM operations WHERE run_id=:run AND operation_id=:operation"),
            {"run": request.run_id, "operation": request.operation_id},
        ).one_or_none()
        if row is None or row.payload_hash.strip() != _fingerprint(request):
            raise DomainError("storage_unavailable", 503)
        if row.state == "committed":
            return _operation_result(row)
        pending = row.result or {}
        if row.state == "unknown":
            if pending.get("usage_known") and pending.get("ref"):
                return _finalize_staged_success(db, request.run_id, request.operation_id)
            return _operation_result(row)
        if row.state != "reserved":
            return _operation_result(row)
        if time.monotonic() >= deadline:
            return _record_unknown(db, request, revision, usage_tokens=None, result_ref=None)
        time.sleep(_COMPUTE_POLL_SECONDS)


def record_verified_completion(
    db: Session,
    request: OperationRequest,
    revision: int,
    *,
    usage_tokens: int,
    result_ref: ObjectRef,
    peer_task_terminal: bool | None = None,
) -> OperationResult:
    """Stage and finalize verified evidence with run→operation locks and one-time accounting."""
    if isinstance(usage_tokens, bool) or not isinstance(usage_tokens, int) or usage_tokens < 0:
        raise DomainError("provider_unavailable", 502)
    run = _load_run(db, request.run_id, lock=True)
    operation = db.execute(
        text("SELECT * FROM operations WHERE run_id = :run AND operation_id = :operation FOR UPDATE"),
        {"run": request.run_id, "operation": request.operation_id},
    ).one_or_none()
    if operation is None:
        raise DomainError("not_found", 404)
    if operation.payload_hash.strip() != _fingerprint(request):
        raise DomainError("idempotency_conflict", 409)
    if operation.state == "committed":
        return _operation_result(operation)
    pending = dict(operation.result or {})
    if operation.state == "unknown":
        if pending.get("ref") is not None and ObjectRef.model_validate(pending["ref"]) != result_ref:
            raise DomainError("idempotency_conflict", 409)
        if pending.get("usage_known") and pending.get("ref"):
            return _finalize_staged_success(db, request.run_id, request.operation_id)
        if pending.get("usage_known") and operation.usage_tokens != usage_tokens:
            raise DomainError("revision_conflict", 409)
        if run.reserved_tokens < operation.reserve_tokens and not pending.get("usage_known"):
            raise DomainError("revision_conflict", 409)
        pending["ref"] = result_ref.model_dump(mode="json")
        pending["usage_tokens"] = usage_tokens
        if peer_task_terminal is not None:
            pending["peer_task_terminal"] = peer_task_terminal
        if not pending.get("usage_known"):
            pending["usage_known"] = True
            db.execute(
                text("UPDATE runs SET reserved_tokens = reserved_tokens - :reserve, usage_tokens = usage_tokens + :usage WHERE id = :run"),
                {"reserve": operation.reserve_tokens, "usage": usage_tokens, "run": request.run_id},
            )
            db.execute(text("UPDATE operations SET usage_tokens = :usage WHERE id = :id"), {"usage": usage_tokens, "id": operation.id})
            _emit_usage_event(db, request.run_id, revision)
    elif operation.state == "reserved":
        pending = {
            "request": request.model_dump(mode="json"),
            "ref": result_ref.model_dump(mode="json"),
            "usage_known": True,
            "usage_tokens": usage_tokens,
        }
        if peer_task_terminal is not None:
            pending["peer_task_terminal"] = peer_task_terminal
        db.execute(
            text("UPDATE operations SET state = 'unknown', usage_tokens = :usage, result = CAST(:result AS jsonb) WHERE id = :id AND state = 'reserved'"),
            {"usage": usage_tokens, "result": json.dumps(pending, separators=(",", ":")), "id": operation.id},
        )
        db.execute(
            text("UPDATE runs SET reserved_tokens = reserved_tokens - :reserve, usage_tokens = usage_tokens + :usage WHERE id = :run"),
            {"reserve": operation.reserve_tokens, "usage": usage_tokens, "run": request.run_id},
        )
        _emit_usage_event(db, request.run_id, revision)
    else:
        return _operation_result(operation)
    db.execute(
        text("UPDATE operations SET result = CAST(:result AS jsonb) WHERE id = :id AND state = 'unknown'"),
        {"result": json.dumps(pending, separators=(",", ":")), "id": operation.id},
    )
    current = _load_run(db, request.run_id)
    if (
        current.usage_tokens + current.reserved_tokens >= current.token_limit
        or limits.effective_elapsed_ms(db, request.run_id) >= current.elapsed_limit_ms
    ):
        limits.mark_budget_wait(db, request.run_id, revision, _event)
    db.commit()
    if peer_task_terminal is False:
        return OperationResult(operation_id=request.operation_id, state="unknown", result=result_ref, usage_tokens=None)
    return _finalize_staged_success(db, request.run_id, request.operation_id)

def _finalize_staged_success(db: Session, run_id: UUID, operation_id: str) -> OperationResult:
    run = _load_run(db, run_id, lock=True)
    operation = db.execute(
        text("SELECT * FROM operations WHERE run_id = :run AND operation_id = :operation FOR UPDATE"),
        {"run": run_id, "operation": operation_id},
    ).one_or_none()
    if operation is None:
        raise DomainError("not_found", 404)
    if operation.state == "committed":
        return _operation_result(operation)
    staged = operation.result or {}
    if operation.state != "unknown" or not staged.get("usage_known") or not staged.get("ref"):
        raise DomainError("revision_conflict", 409)
    result_ref = ObjectRef.model_validate(staged["ref"])
    staged["usage_tokens"] = operation.usage_tokens
    committed = db.execute(text("""
        UPDATE operations SET state = 'committed', result = CAST(:result AS jsonb)
        WHERE id = :id AND state = 'unknown'
    """), {"result": json.dumps(staged, separators=(",", ":")), "id": operation.id})
    if committed.rowcount != 1:
        db.rollback()
        current = db.execute(
            text("SELECT * FROM operations WHERE run_id = :run AND operation_id = :operation"),
            {"run": run_id, "operation": operation_id},
        ).one()
        if current.state == "committed":
            return _operation_result(current)
        raise DomainError("revision_conflict", 409)
    db.execute(text("""
        UPDATE owner_decisions SET state='resolved', resolved_at=now(),
            resolution=CAST(:resolution AS jsonb)
        WHERE run_id=:run AND operation_id=:operation
          AND reason='unknown_outcome' AND state='pending'
    """), {"run": run_id, "operation": operation_id,
            "resolution": json.dumps({"source": "verified_completion", "operation_id": operation_id})})
    resumed = db.execute(text("""
        UPDATE runs SET state=CASE WHEN lease_expires_at>now() THEN 'running' ELSE 'queued' END,
            waiting_reason=NULL
        WHERE id=:run AND state='waiting_input' AND waiting_reason='unknown_outcome'
          AND NOT cancel_requested
          AND NOT EXISTS (SELECT 1 FROM operations WHERE run_id=:run AND state IN ('reserved','unknown'))
        RETURNING state
    """), {"run": run_id}).scalar_one_or_none()
    if resumed is not None:
        _event(db, run_id, run.revision, "run.state", {"state": resumed})
    db.commit()
    return OperationResult(operation_id=operation_id, state="committed", result=result_ref, usage_tokens=operation.usage_tokens)

def _record_unknown(
    db: Session,
    request: OperationRequest,
    revision: int,
    *,
    usage_tokens: int | None,
    result_ref: ObjectRef | None,
    peer_task_terminal: bool | None = None,
) -> OperationResult:
    prior = _load_run(db, request.run_id, lock=True)
    operation = db.execute(
        text("SELECT * FROM operations WHERE run_id = :run AND operation_id = :operation FOR UPDATE"),
        {"run": request.run_id, "operation": request.operation_id},
    ).one_or_none()
    if operation is None:
        raise DomainError("not_found", 404)
    if operation.payload_hash.strip() != _fingerprint(request):
        raise DomainError("idempotency_conflict", 409)
    if operation.state == "committed":
        return _operation_result(operation)
    if operation.state != "reserved":
        pending = operation.result or {}
        if operation.state == "unknown" and pending.get("usage_known") and pending.get("ref"):
            return _finalize_staged_success(db, request.run_id, request.operation_id)
        return _operation_result(operation)
    result = {"request": request.model_dump(mode="json"), "usage_known": usage_tokens is not None}
    if result_ref is not None:
        result["ref"] = result_ref.model_dump(mode="json")
    if peer_task_terminal is not None:
        result["peer_task_terminal"] = peer_task_terminal
    changed = db.execute(text("""
        UPDATE operations SET state = 'unknown', usage_tokens = COALESCE(:usage, usage_tokens),
            result = CAST(:result AS jsonb)
        WHERE id = :id AND state = 'reserved'
    """), {"usage": usage_tokens, "result": json.dumps(result, separators=(",", ":")), "id": operation.id})
    if changed.rowcount != 1:
        db.rollback()
        return _operation_result(db.execute(
            text("SELECT * FROM operations WHERE run_id = :run AND operation_id = :operation"),
            {"run": request.run_id, "operation": request.operation_id},
        ).one())
    if usage_tokens is not None:
        db.execute(text("""
            UPDATE runs SET reserved_tokens = reserved_tokens - :reserve, usage_tokens = usage_tokens + :usage
            WHERE id = :run
        """), {"reserve": operation.reserve_tokens, "usage": usage_tokens, "run": request.run_id})
        _emit_usage_event(db, request.run_id, revision)
    waiting = db.execute(text("""
        UPDATE runs SET waiting_reason = CASE WHEN state = 'waiting_input'
            THEN waiting_reason ELSE 'unknown_outcome' END, state = 'waiting_input'
        WHERE id = :run AND state NOT IN ('completed', 'failed', 'canceled', 'rejected', 'stopping')
            AND NOT cancel_requested
    """), {"run": request.run_id}).rowcount == 1
    if waiting:
        if prior.state != 'waiting_input':
            _event(db, request.run_id, revision, "run.state", {"state": "waiting_input"})
        _issue_unknown_decision(db, request.run_id, revision, request.operation_id)
    db.commit()
    return OperationResult(operation_id=request.operation_id, state="unknown", result=result_ref, usage_tokens=usage_tokens)

def _issue_unknown_decision(db: Session, run_id: UUID, revision: int, operation_id: str) -> None:
    """Bind a decision_id to the operation and emit decision.required in the caller's transaction."""
    decision_id = db.execute(text("""
        INSERT INTO owner_decisions (decision_id, run_id, operation_id, reason)
        VALUES (:id, :run, :operation, 'unknown_outcome')
        ON CONFLICT (run_id, operation_id) WHERE state = 'pending' AND reason = 'unknown_outcome' DO NOTHING
        RETURNING decision_id
    """), {"id": uuid4(), "run": run_id, "operation": operation_id}).scalar_one_or_none()
    if decision_id is not None:
        _event(db, run_id, revision, "decision.required", {"decision_id": str(decision_id), "reason": "unknown_outcome"})


def _emit_usage_event(db: Session, run_id: UUID, revision: int) -> None:
    row = db.execute(text("SELECT usage_tokens, reserved_tokens, token_limit FROM runs WHERE id = :run"),
                     {"run": run_id}).one()
    _event(db, run_id, revision, "usage.updated", {
        "usage_tokens": row.usage_tokens,
        "reserved_tokens": row.reserved_tokens,
        "token_limit": row.token_limit,
    })


def reconcile(db: Session, run_id: UUID, operation_id: str) -> OperationResult:
    run = _load_run(db, run_id, lock=True)
    row = db.execute(text("SELECT * FROM operations WHERE run_id = :run AND operation_id = :operation FOR UPDATE"),
                     {"run": run_id, "operation": operation_id}).one_or_none()
    if row is None:
        raise DomainError("not_found", 404)
    pending = row.result or {}
    if row.state == "unknown" and pending.get("usage_known") and pending.get("ref"):
        return _finalize_staged_success(db, run_id, operation_id)
    if row.state in {"reserved", "unknown"}:
        if not _quiescent(run, row):
            raise DomainError("revision_conflict", 409)
    stopped = run.state in limits._TERMINAL or run.cancel_requested
    if row.state == "reserved":
        db.execute(text("UPDATE operations SET state = 'unknown' WHERE id = :id AND state = 'reserved'"), {"id": row.id})
        if not stopped:
            if run.state != "waiting_input" or run.waiting_reason != "unknown_outcome":
                db.execute(text("UPDATE runs SET state = 'waiting_input', waiting_reason = 'unknown_outcome' WHERE id = :run"), {"run": run_id})
                if run.state != "waiting_input":
                    _event(db, run_id, run.revision, "run.state", {"state": "waiting_input"})
            _issue_unknown_decision(db, run_id, run.revision, operation_id)
        if run.state == "canceled":
            _issue_unknown_decision(db, run_id, run.revision, operation_id)
        db.commit()
        row = db.execute(text("SELECT * FROM operations WHERE id = :id"), {"id": row.id}).one()
    elif row.state == "unknown" and run.state == "canceled" and not pending.get("retry_identity") and not pending.get("usage_known"):
        _issue_unknown_decision(db, run_id, run.revision, operation_id)
        db.commit()
    elif row.state == "unknown" and not stopped and not pending.get("retry_identity"):
        if run.state != "waiting_input" or run.waiting_reason != "unknown_outcome":
            db.execute(text("UPDATE runs SET state = 'waiting_input', waiting_reason = 'unknown_outcome' WHERE id = :run"), {"run": run_id})
            if run.state != "waiting_input":
                _event(db, run_id, run.revision, "run.state", {"state": "waiting_input"})
        _issue_unknown_decision(db, run_id, run.revision, operation_id)
        db.commit()
    return _operation_result(row)


def resolve_unknown(
    db: Session,
    owner: Principal,
    run_id: UUID,
    operation_id: str,
    decision: Literal["verified_result", "retry", "stop"],
    result: ObjectRef | None,
    *,
    queued_only: bool = True,
    receipt: tuple[str, str] | None = None,
) -> RunView:
    """queued_only (default) forces a retry through the supervisor queue (never dispatched in this process);
    queued_only=False keeps the lease-alive in-process dispatch for the legacy worker-embedded tests only.

    receipt=(idempotency_key, payload_hash) is committed atomically with the resolution."""
    if owner.kind != "owner":
        raise DomainError("forbidden", 403)
    run = _load_run(db, run_id, lock=True)
    operation = db.execute(text("SELECT * FROM operations WHERE run_id = :run AND operation_id = :operation FOR UPDATE"),
                           {"run": run_id, "operation": operation_id}).one_or_none()
    if operation is None or operation.state != "unknown" or (operation.result or {}).get("retry_identity"):
        raise DomainError("revision_conflict", 409)
    if not _quiescent(run, operation):
        raise DomainError("revision_conflict", 409)
    if run.state != "waiting_input" or run.waiting_reason != "unknown_outcome":
        raise DomainError("revision_conflict", 409)
    if decision != "verified_result" and result is not None:
        raise DomainError("forbidden", 400)
    retry_request: OperationRequest | None = None
    retry_now = False
    if decision == "retry":
        # Persist the owner decision and link before an active or replacement worker can dispatch.
        retry_id = f"retry-{uuid4()}"
        pending = operation.result or {}
        try:
            original = OperationRequest.model_validate(pending["request"])
        except (KeyError, ValueError) as exc:
            raise DomainError("storage_unavailable", 503) from exc
        retry_now = (not queued_only and run.lease_expires_at is not None
                     and run.lease_expires_at > datetime.now(timezone.utc))
        retry_generation = run.generation if retry_now else run.generation + 1
        retry_request = original.model_copy(update={"operation_id": retry_id, "generation": retry_generation})
        pending["retry_identity"] = retry_id
        pending["retry_request"] = retry_request.model_dump(mode="json")
        db.execute(text("UPDATE operations SET result = CAST(:result AS jsonb) WHERE id = :id"),
                   {"result": json.dumps(pending, separators=(",", ":")), "id": operation.id})
        db.execute(text("UPDATE runs SET state = :state, waiting_reason = NULL WHERE id = :run"),
                   {"state": "running" if retry_now else "queued", "run": run_id})
        _event(db, run_id, run.revision, "run.state", {"state": "running" if retry_now else "queued"})
    elif decision == "verified_result":
        if result is None or result.project_id != run.project_id or _verify_result is None:
            raise DomainError("forbidden", 400)
        pending = operation.result or {}
        try:
            original = OperationRequest.model_validate(pending["request"])
        except (KeyError, ValueError) as exc:
            raise DomainError("storage_unavailable", 503) from exc
        if (original.run_id != run_id or original.operation_id != operation_id or original.generation != operation.generation
                or _fingerprint(original) != operation.payload_hash.strip()):
            raise DomainError("storage_unavailable", 503)
        context = UnknownOperationContext(run_id=run_id, project_id=run.project_id, operation_id=operation_id,
                                          generation=operation.generation, kind=operation.kind,
                                          payload_hash=operation.payload_hash.strip(), request=original)
        staged_ref = ObjectRef.model_validate(pending["ref"]) if pending.get("ref") else None
        if staged_ref is not None and staged_ref != result:
            raise DomainError("forbidden", 400)
        reused = db.execute(text("""
            SELECT 1 FROM operations o JOIN runs r ON r.id = o.run_id
            WHERE r.project_id = :project AND o.id <> :operation
              AND o.result -> 'ref' ->> 'key' = :key
        """), {"project": run.project_id, "operation": operation.id, "key": result.key}).scalar_one_or_none()
        try:
            verified = not reused and _verify_result(context, result)
        except Exception:
            verified = False
        if not verified:
            raise DomainError("forbidden", 400)
        verified_result = {
            "request": pending["request"],
            "ref": result.model_dump(mode="json"),
            "usage_known": pending.get("usage_known") is True,
        }
        if verified_result["usage_known"]:
            verified_result["usage_tokens"] = operation.usage_tokens
        else:
            verified_result["usage_tokens"] = None
        db.execute(text("UPDATE operations SET state = 'committed', result = CAST(:result AS jsonb) WHERE id = :id"),
                   {"result": json.dumps(verified_result, separators=(",", ":")), "id": operation.id})
        # No trustworthy usage reconciliation exists; retain the reservation conservatively.
        next_state = "running" if run.lease_expires_at is not None and run.lease_expires_at > datetime.now(timezone.utc) else "queued"
        db.execute(text("UPDATE runs SET state = :state, waiting_reason = NULL WHERE id = :run"),
                   {"state": next_state, "run": run_id})
        _event(db, run_id, run.revision, "run.state", {"state": next_state})
    else:
        db.execute(text("UPDATE runs SET state = 'stopping', cancel_requested = true, waiting_reason = NULL WHERE id = :run"), {"run": run_id})
        _event(db, run_id, run.revision, "run.state", {"state": "stopping"})
    db.execute(text("""
        UPDATE owner_decisions SET state = 'resolved', resolved_at = now(), resolution = CAST(:resolution AS jsonb),
            idempotency_key = :key, payload_hash = :hash
        WHERE run_id = :run AND operation_id = :operation AND state = 'pending' AND reason = 'unknown_outcome'
    """), {"resolution": json.dumps({"choice": decision}), "key": receipt[0] if receipt else None,
           "hash": receipt[1] if receipt else None, "run": run_id, "operation": operation_id})
    db.commit()
    if retry_request is not None and retry_now:
        try:
            capability = issue_capability(db, run_id, run.generation, 300)
            execute(db, capability, retry_request)
            db.execute(text("""
                UPDATE operations SET result = result || jsonb_build_object('retry_of', CAST(:original AS text))
                WHERE run_id = :run AND operation_id = :retry
            """), {"original": operation_id, "run": run_id, "retry": retry_request.operation_id})
            db.commit()
        except DomainError as exc:
            if exc.code != "budget_exhausted":
                raise
            db.rollback()
            limits.mark_budget_wait(db, run_id, run.revision, _event, reserve_tokens=retry_request.reserve_tokens)  # no-op if execute already marked it
            db.commit()
    return _run_view(db, run_id)


def configure_result_verifier(verifier: ResultVerifier | None) -> None:
    global _verify_result
    _verify_result = verifier


def _validate_scope(db: Session, project_id: UUID, plan: PlanSpec, request: OperationRequest) -> DispatchTarget:
    if request.kind not in plan.allowed_ops:
        raise DomainError("forbidden", 403)
    if request.kind not in {"llm", "search", "package", "peer", "compute"}:
        raise DomainError("forbidden", 403)
    if (request.kind == "compute" or (request.kind == "search" and isinstance(plan.scientific, ScientificBindingV2))) and request.reserve_tokens != 0:
        raise DomainError("forbidden", 403)
    payload = request.payload
    if request.kind == "search" and isinstance(plan.scientific, ScientificBindingV2):
        _reject_fields(payload, {"request_id", "query"})
        if set(payload) != {"request_id", "query"}:
            raise DomainError("forbidden", 400)
        request_id = payload.get("request_id")
        approved = plan.scientific.approved_crossref_queries.get(request_id) if isinstance(request_id, str) else None
        try:
            query = CrossrefQueryV1.model_validate(payload["query"])
        except (TypeError, ValueError) as exc:
            raise DomainError("forbidden", 400) from exc
        recipient = "https://api.crossref.org/works"
        if approved is None or query != approved or recipient not in plan.data_recipients:
            raise DomainError("forbidden", 403)
        _approved_recipient(plan, recipient)
        from scientist.scholarly_retrieval import build_crossref_url
        return DispatchTarget("search", build_crossref_url(approved), tuple(plan.data_recipients))
    if request.kind == "compute":
        _reject_fields(payload, {"grant_id", "grant"})
        if set(payload) != {"grant_id", "grant"} or not isinstance(plan.scientific, ScientificBindingV2):
            raise DomainError("forbidden", 400)
        grant_id = payload.get("grant_id")
        approved_grant = plan.scientific.csv_describe_grants.get(grant_id) if isinstance(grant_id, str) else None
        try:
            grant = CsvDescribeGrantV1.model_validate(payload["grant"])
        except (TypeError, ValueError) as exc:
            raise DomainError("forbidden", 400) from exc
        if approved_grant is None or grant != approved_grant:
            raise DomainError("forbidden", 403)
        return DispatchTarget("compute", "", ())
    if request.kind == "llm":
        _reject_fields(payload, {"provider_id", "model", "recipient", "credential_id", "max_output_tokens", "prompt", "messages", "timeout_seconds", *_LLM_CONTROLS})
        if not {"provider_id", "model", "recipient", "credential_id", "max_output_tokens"} <= set(payload):
            raise DomainError("forbidden", 400)
        if str(payload.get("provider_id")) != str(plan.provider_id) or payload.get("model") != plan.model:
            raise DomainError("forbidden", 403)
        max_output = payload.get("max_output_tokens")
        if isinstance(max_output, bool) or not isinstance(max_output, int) or max_output < 1:
            raise DomainError("budget_exhausted", 409)
        if request.reserve_tokens < _llm_input_reserve(payload) + max_output:
            raise DomainError("budget_exhausted", 409)
        _validate_timeout(payload)
        recipient = _provider_destinations.get(str(plan.provider_id))
        if not recipient or payload.get("recipient") != recipient:
            raise DomainError("forbidden", 403)
        _approved_recipient(plan, recipient)
        credential_id = payload.get("credential_id")
        if str(credential_id) != str(plan.provider_id):
            raise DomainError("forbidden", 403)
        found = db.execute(text("SELECT 1 FROM credentials WHERE id = :id AND (project_id IS NULL OR project_id = :project)"),
                           {"id": plan.provider_id, "project": project_id}).scalar_one_or_none()
        if found is None:
            raise DomainError("forbidden", 403)
        return DispatchTarget("llm", recipient, tuple(plan.data_recipients), plan.provider_id, model=plan.model)
    elif request.kind == "search":
        _reject_fields(payload, {"url", "timeout_seconds"})
        url = payload.get("url")
        if not isinstance(url, str):
            raise DomainError("forbidden", 400)
        _validate_timeout(payload)
        _validate_url(url, plan.data_recipients)
        return DispatchTarget("search", url, tuple(plan.data_recipients))
    elif request.kind == "package":
        _reject_fields(payload, {"name", "version", "source", "sha256"})
        if not {"name", "version", "source", "sha256"} <= set(payload):
            raise DomainError("forbidden", 400)
        package = next((item for item in plan.packages if item.name == payload.get("name") and item.version == payload.get("version")
                        and item.source == payload.get("source") and item.sha256 == payload.get("sha256")), None)
        if package is None:
            raise DomainError("forbidden", 403)
        _validate_url(package.source, plan.data_recipients)
        return DispatchTarget("package", package.source, tuple(plan.data_recipients), expected_sha256=package.sha256)
    else:
        _reject_fields(payload, {"release_id", "parameters"})
        if set(payload) != {"release_id", "parameters"}:
            raise DomainError("forbidden", 403)
        release = next((item for item in plan.peer_releases if str(item.release_id) == payload["release_id"]), None)
        if release is None or request.reserve_tokens != release.reserved_tokens:
            raise DomainError("forbidden", 403)
        try:
            parameters = canonical_peer_parameters_bytes(payload["parameters"])
        except ValueError as exc:
            raise DomainError("forbidden", 403) from exc
        if (hashlib.sha256(parameters).hexdigest() != release.parameters_sha256.lower() or
                parameters != canonical_peer_parameters_bytes(release.approved_parameters)):
            raise DomainError("forbidden", 403)
        from scientist.domain import _validate_peer_releases
        from scientist.settings import _origin
        return _validate_peer_target(db, request.run_id, project_id, plan, release)


def validate_peer_reconciliation(
    db: Session, run_id: UUID, project_id: UUID, plan: PlanSpec, release_id: UUID,
) -> DispatchTarget:
    """Check current config, delegation, and credential before reserving recovery."""
    release = next((item for item in plan.peer_releases if item.release_id == release_id), None)
    if release is None or not release.allow_get_task:
        raise DomainError("peer_release_unapproved", 409)
    return _validate_peer_target(db, run_id, project_id, plan, release)


def _validate_peer_target(
    db: Session, run_id: UUID, project_id: UUID, plan: PlanSpec, release: PeerReleaseSpec,
) -> DispatchTarget:
    """Shared live peer binding checks for SendMessage and recovery GetTask."""
    from scientist.domain import _validate_peer_releases
    from scientist.settings import _origin

    _validate_peer_releases(db, run_id, project_id, plan)
    peer_id = str(release.peer_id)
    destination = _peer_destinations.get(peer_id)
    if (
        not destination or _origin(destination) != destination
        or hashlib.sha256(destination.encode()).hexdigest() != release.endpoint_fingerprint.lower()
    ):
        raise DomainError("forbidden", 403)
    delegated = db.execute(text("""
        SELECT 1 FROM delegations
        WHERE project_id=:project AND peer_id=:peer AND revoked_at IS NULL
          AND :action=ANY(actions)
    """), {"project": project_id, "peer": release.peer_id, "action": "peer"}).scalar_one_or_none()
    if delegated is None:
        raise DomainError("forbidden", 403)
    credential = db.execute(text("""
        SELECT id FROM credentials
        WHERE project_id=:project AND provider=:provider AND model IS NULL
        ORDER BY created_at DESC,id LIMIT 1
    """), {"project": project_id, "provider": f"peer:{peer_id}"}).scalar_one_or_none()
    if credential is None or len(destination + "/a2a") > 2048:
        raise DomainError("forbidden", 403)
    return DispatchTarget(
        "peer", destination + "/a2a", (destination,),
        credential_id=credential, peer_release=release,
    )



def _reject_fields(payload: dict, allowed: set[str]) -> None:
    if set(payload) - allowed:
        raise DomainError("forbidden", 403)


def _validate_timeout(payload: dict) -> None:
    value = payload.get("timeout_seconds", 10)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_TIMEOUT_SECONDS:
        raise DomainError("forbidden", 400)


def _llm_input_reserve(payload: dict) -> int:
    if ("prompt" in payload) == ("messages" in payload):
        raise DomainError("forbidden", 400)
    messages = payload.get("messages")
    if "prompt" in payload:
        prompt = payload["prompt"]
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 200_000:
            raise DomainError("forbidden", 400)
        messages = [{"role": "user", "content": prompt}]
    controls = {key: payload[key] for key in _LLM_CONTROLS if key in payload}
    try:
        return llm_input_reserve(messages, **controls)
    except ModelPayloadError as exc:
        raise DomainError("forbidden", 400) from exc


def _approved_recipient(plan: PlanSpec, recipient: object) -> None:
    if not isinstance(recipient, str) or recipient not in plan.data_recipients:
        raise DomainError("forbidden", 403)
    _validate_url(recipient, plan.data_recipients)


def _validate_url(url: object, recipients: list[str], *, allow_lan: bool = False, resolver: Resolver | None = None) -> tuple[str, int, str, str]:
    if not isinstance(url, str) or len(url) > 2048:
        raise DomainError("forbidden", 403)
    # urlsplit strips \t\r\n and accepts spaces/NUL/non-ASCII; http.client would then fail after reservation.
    if not url.isascii() or any(c <= " " or c == "\x7f" for c in url):
        raise DomainError("forbidden", 403)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise DomainError("forbidden", 403)
    host = parsed.hostname.rstrip(".").lower()
    if host in _METADATA_HOSTS:
        raise DomainError("forbidden", 403)
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise DomainError("forbidden", 403) from exc
    if port != 443 and not allow_lan:
        raise DomainError("forbidden", 403)
    def origin(value: str) -> tuple[str, int] | None:
        part = urlsplit(value if "://" in value else f"https://{value}")
        try:
            return (part.hostname.rstrip(".").lower(), part.port or 443) if part.hostname and part.scheme == "https" else None
        except ValueError:
            return None
    if not any(origin(item) == (host, port) for item in recipients):
        raise DomainError("forbidden", 403)
    try:
        try:
            literal = ipaddress.ip_address(host)
            addresses = [str(literal)]
        except ValueError:
            addresses = (resolver or _resolver)(host, port)
        if not addresses:
            raise ValueError("empty DNS result")
        parsed_addresses = [ipaddress.ip_address(address.split("%", 1)[0]) for address in addresses]
    except (OSError, ValueError) as exc:
        raise DomainError("provider_unavailable", 503) from exc
    if not allow_lan and any(not address.is_global for address in parsed_addresses):
        raise DomainError("forbidden", 403)
    if any(address in _METADATA_ADDRESSES for address in parsed_addresses):
        raise DomainError("forbidden", 403)
    if allow_lan:
        if len({address.is_global for address in parsed_addresses}) != 1:
            raise DomainError("forbidden", 403)
        for address in parsed_addresses:
            checked = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address
            if checked.is_loopback or checked.is_multicast or checked.is_unspecified or checked.is_link_local or checked.is_reserved:
                raise DomainError("forbidden", 403)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return host, port, path, str(parsed_addresses[0])


def _dispatch(request: OperationRequest, target: DispatchTarget) -> tuple[bytes, int | None]:
    if _transport is not None:
        return _transport(request, target)
    return http_transport(request, target)


def http_transport(request: OperationRequest, target: DispatchTarget) -> tuple[bytes, int | None]:
    """Native bounded HTTP transport, usable beneath durable dispatch bindings."""
    if target.kind == "peer":
        return _peer_http_transport(request, target)
    host, port, path, ip = _validate_url(target.url, list(target.approved_recipients))
    if target.kind == "llm":
        messages = request.payload.get("messages")
        if "prompt" in request.payload:
            messages = [{"role": "user", "content": request.payload["prompt"]}]
        controls = {key: request.payload[key] for key in _LLM_CONTROLS if key in request.payload}
        try:
            outbound = build_chat_completion_body(
                target.model or "",
                messages,
                request.payload.get("max_output_tokens"),
                **controls,
            )
        except ModelPayloadError as exc:
            raise DomainError("forbidden", 400) from exc
    elif target.kind in {"search", "package"}:
        outbound = {}  # public GET: the validated URL carries the query; no body is ever sent
    else:
        body_fields = {"credential_id", "url", "source", "endpoint", "destination", "recipient", "provider_id", "model", "peer_id", "timeout_seconds"}
        outbound = {key: value for key, value in request.payload.items() if key not in body_fields}
    body = json.dumps(outbound, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(body) > _MAX_REQUEST_BYTES:
        raise DomainError("request_too_large", 413)
    timeout = min(float(request.payload.get("timeout_seconds", _MAX_TIMEOUT_SECONDS)), _MAX_HTTP_TOTAL_SECONDS)
    deadline = time.monotonic() + timeout
    connection = _PinnedHTTPSConnection(host, ip, port, timeout)
    headers = {"Host": host} if target.kind in {"search", "package"} else {"Content-Type": "application/json", "Host": host}
    if target.kind == "llm" and target.credential_id is not None:
        secret = read_secret(_active_session.get(), target.credential_id)
        headers["Authorization"] = f"Bearer {secret}"
    connection._deadline = deadline
    watchdog = Timer(max(0.0, deadline - time.monotonic()), _abort_connection, args=(connection,))
    watchdog.daemon = True
    watchdog.start()
    try:
        if time.monotonic() >= deadline:
            raise DomainError("provider_unavailable", 502)
        get = target.kind in {"search", "package"}
        connection.request("GET" if get else "POST", path, body=None if get else body, headers=headers)
        response = connection.getresponse()
        if response.status < 200 or response.status >= 300 or response.getheader("Location"):
            raise DomainError("provider_unavailable", 502)
        data = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(data) > _MAX_RESPONSE_BYTES or time.monotonic() > deadline or getattr(response, "length", None):
            raise DomainError("provider_unavailable", 502)  # oversize, late, or Content-Length bytes still outstanding
        if target.kind in {"search", "package"}:
            if target.kind == "search" and isinstance(request.payload.get("query"), dict):
                try:
                    from scientist.scholarly_retrieval import parse_crossref_response
                    query = CrossrefQueryV1.model_validate(request.payload["query"])
                    parsed = parse_crossref_response(query, data)
                    data = json.dumps(
                        parsed, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), allow_nan=False,
                    ).encode("utf-8")
                except (TypeError, ValueError, UnicodeError) as exc:
                    raise DomainError("provider_unavailable", 502) from exc
            return data, 0  # raw bytes: public APIs return lists, XML or text, not an OperationResult object
        payload = json.loads(data)
        usage = payload.get("usage_tokens", payload.get("usage", {}).get("total_tokens"))
        if target.kind != "llm" and usage is None:
            usage = 0
        if isinstance(usage, bool) or not isinstance(usage, int) or usage < 0:
            raise DomainError("provider_unavailable", 502)
        return data, usage
    finally:
        watchdog.cancel()
        connection.close()



def _peer_http_transport(request: OperationRequest, target: DispatchTarget) -> tuple[bytes, int | None]:
    import asyncio
    from google.protobuf.json_format import MessageToDict
    from scientist.a2a_outbound import PeerOutboundCallbacks, submit_peer_release
    from scientist import peer_http_exchange, peer_receipt_runtime

    release = target.peer_release
    if release is None or target.credential_id is None:
        raise DomainError("forbidden", 403)
    credential = read_secret(_active_session.get(), target.credential_id)
    origin = target.approved_recipients[0]
    captured: list[bytes] = []
    def capture(_run, _operation, task, accounting):
        captured.append(json.dumps(MessageToDict(task), ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False).encode())
    callbacks = PeerOutboundCallbacks(
        prepare=peer_receipt_runtime.prepare_submission,
        record_remote_identity=peer_receipt_runtime.record_remote_identity,
        mark_unknown=peer_receipt_runtime.mark_unknown,
        persist_result=capture,
    )
    result = asyncio.run(submit_peer_release(
        release, request.run_id, request.operation_id, endpoint_url=target.url,
        expected_authority=urlsplit(origin).netloc, callbacks=callbacks,
        exchange=peer_http_exchange.pinned_exchange(origin, credential, request_bytes_limit=release.request_bytes_limit),
    ))
    if len(captured) != 1:
        raise DomainError("provider_unavailable", 502)
    # The common broker PUT happens after the SDK callback committed the remote identity.
    # Standard A2A has no authoritative usage contract; None preserves the reservation.
    return captured[0], result.accounting.usage_tokens


def _abort_connection(connection: http.client.HTTPConnection) -> None:
    expired = getattr(connection, "_deadline_expired", None)
    if expired is not None:
        expired.set()
    sockets = {getattr(connection, "_deadline_sock", None), getattr(connection, "sock", None)}
    for sock in sockets - {None}:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
    connection.close()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip
        self._deadline = time.monotonic() + timeout
        self._deadline_sock = None
        self._deadline_expired = Event()

    def connect(self) -> None:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("provider request deadline exceeded")
        raw = socket.create_connection((self._ip, self.port), remaining)
        self.sock = raw
        self._deadline_sock = raw
        try:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0 or self._deadline_expired.is_set():
                raise socket.timeout("provider request deadline exceeded")
            raw.settimeout(remaining)
            secured = self._context.wrap_socket(raw, server_hostname=self.host)
            self.sock = secured
            self._deadline_sock = secured
            if time.monotonic() > self._deadline or self._deadline_expired.is_set():
                raise socket.timeout("provider request deadline exceeded")
        except Exception:
            raw.close()
            self.sock = None
            raise


def _persist(db: Session, run_id: UUID, data: bytes, content_type: str) -> ObjectRef:
    if _persist_result is None:
        raise DomainError("storage_unavailable", 503)
    project_id = _load_run_project(db, run_id)
    ref = _persist_result(db, project_id, data, content_type)
    if ref.sha256 != hashlib.sha256(data).hexdigest() or ref.size != len(data) or ref.project_id != project_id or ref.content_type != content_type:
        raise DomainError("storage_unavailable", 503)
    return ref


def _load_run_project(db: Session, run_id: UUID) -> UUID:
    row = db.execute(text("SELECT project_id FROM runs WHERE id = :run"), {"run": run_id}).one()
    return row.project_id


def _sign_capability(claims: dict) -> str:
    key = _key()
    body = base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()).rstrip(b"=")
    signature = hmac.new(key, body, hashlib.sha256).digest()
    return body.decode() + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


def _verify_capability(token: str) -> dict:
    try:
        body, signature = token.split(".", 1)
        expected = base64.urlsafe_b64encode(hmac.new(_key(), body.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        if not hmac.compare_digest(expected, signature):
            raise ValueError
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if not isinstance(claims, dict) or claims.get("exp", 0) <= int(datetime.now(timezone.utc).timestamp()):
            raise ValueError
        return claims
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise DomainError("forbidden", 403) from exc


def _key() -> bytes:
    key = _capability_key or os.environ.get("SCIENTIST_BROKER_CAPABILITY_KEY", "").encode()
    if len(key) < 32:
        raise DomainError("storage_unavailable", 503)
    return key


def _check_capability_claims(row, claims: dict) -> None:
    if (claims.get("run_id") != str(row.id) or claims.get("generation") != row.generation
            or claims.get("revision") != row.revision or claims.get("plan_digest") != row.plan_digest.strip()
            or row.state != "running" or row.lease_expires_at is None
            or row.lease_expires_at <= datetime.now(timezone.utc)):
        raise DomainError("forbidden", 403)


def _load_run(db: Session, run_id: UUID, *, lock: bool = False):
    suffix = " FOR UPDATE" if lock else ""
    row = db.execute(text("SELECT * FROM runs WHERE id = :run" + suffix), {"run": run_id}).one_or_none()
    if row is None:
        raise DomainError("not_found", 404)
    return row


def _quiescent(run, operation) -> bool:
    pending = operation.result or {}
    try:
        original = OperationRequest.model_validate(pending["request"])
    except (KeyError, ValueError):
        return False
    if (original.run_id != run.id or original.operation_id != operation.operation_id
            or original.generation != operation.generation):
        return False
    identity = (original.run_id, original.operation_id, original.generation)
    if identity in _active_operations:
        return False
    proof = _dispatch_inactivity_proof  # registered exact proof takes precedence over every test shortcut
    if proof is None:
        if identity in _inactive_dispatches:
            return True
        proof = _dispatch_is_inactive
    if proof is None:
        return False
    try:
        return bool(proof(*identity))
    except Exception:
        return False


def _load_plan(db: Session, run_id: UUID, revision: int) -> PlanSpec:
    row = db.execute(text("SELECT plan FROM plan_revisions WHERE run_id = :run AND revision = :revision"),
                     {"run": run_id, "revision": revision}).one_or_none()
    if row is None:
        raise DomainError("revision_conflict", 409)
    return PlanSpec.model_validate(row.plan)


def _fingerprint(request: OperationRequest) -> str:
    identity = request.model_dump(mode="json")
    identity.pop("generation")
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _operation_result(row) -> OperationResult:
    if row.state not in {"committed", "unknown", "denied", "reserved"}:
        raise DomainError("storage_unavailable", 503)
    if row.state in {"unknown", "reserved"}:
        result = row.result or {}
        usage = row.usage_tokens if result.get("usage_known") else None
        return OperationResult(operation_id=row.operation_id, state="unknown", result=None, usage_tokens=usage)
    result = row.result or {}
    ref = ObjectRef.model_validate(result["ref"]) if result.get("ref") else None
    return OperationResult(operation_id=row.operation_id, state=row.state, result=ref,
                           usage_tokens=result.get("usage_tokens", row.usage_tokens))


def reconcile_peer_from_dispatch(identity) -> OperationResult:
    """Trusted one-shot entrypoint; no caller-selected endpoints or remote identities."""
    from scientist.dispatch_authority import BoundDispatchTransport
    if (identity.mode != "peer_get_task" or identity.peer_reconciliation is None or
            not isinstance(_transport, BoundDispatchTransport) or
            _transport.executor_id != identity.executor_id or
            _transport.incarnation != identity.process_incarnation):
        raise DomainError("forbidden", 403)
    return _transport.reconcile_peer(identity.run_id,identity.generation,identity.peer_reconciliation)


def http_reconcile_peer(scope, generation: int) -> OperationResult:
    import asyncio
    from google.protobuf.json_format import MessageToDict
    from scientist.a2a_outbound import PeerOutboundCallbacks,reconcile_peer_task
    from scientist import peer_http_exchange,peer_receipt_runtime
    from scientist.db import session
    from scientist.peer_reconciliation_scope import load_peer_read_scope
    from scientist.dispatch_authority import validate_peer_reader

    request,target=scope.request,scope.target
    release=target.peer_release
    if release is None or target.credential_id is None:
        raise DomainError("forbidden",403)
    with session() as db:
        credential=read_secret(db,target.credential_id)
    origin=target.approved_recipients[0]
    captured=[]
    def capture(_run,_op,task,_accounting):
        captured.append(json.dumps(MessageToDict(task),ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode())
    callbacks=PeerOutboundCallbacks(prepare=peer_receipt_runtime.prepare_submission,
        record_remote_identity=peer_receipt_runtime.record_remote_identity,
        mark_unknown=peer_receipt_runtime.mark_unknown,persist_result=capture)
    try:
        asyncio.run(reconcile_peer_task(release,request.run_id,request.operation_id,scope.remote_task_id,scope.remote_context_id,
            attempt=scope.attempt,endpoint_url=target.url,expected_authority=urlsplit(origin).netloc,callbacks=callbacks,
            exchange=peer_http_exchange.pinned_exchange(origin,credential,request_bytes_limit=release.request_bytes_limit)))
        if len(captured)!=1:
            raise DomainError("provider_unavailable",502)
        data=captured[0]
        with session() as db:
            refreshed = load_peer_read_scope(db, request.run_id, request.operation_id,
                                             generation, scope.attempt)
            validate_peer_reader(db, scope, generation)
            task_context = json.loads(data).get("contextId") or None
            if (refreshed.remote_task_id != scope.remote_task_id or
                    refreshed.remote_context_id != task_context or
                    (scope.remote_context_id is not None and
                     refreshed.remote_context_id != scope.remote_context_id)):
                raise DomainError("forbidden", 403)
            current=_load_run(db,request.run_id,lock=True)
            operation=db.execute(text("SELECT * FROM operations WHERE run_id=:run AND operation_id=:op FOR UPDATE"),
                                 {"run":request.run_id,"op":request.operation_id}).one()
            if current.generation!=generation or operation.state!="unknown" or operation.payload_hash.strip()!=_fingerprint(request):
                raise DomainError("forbidden",403)
            ref=_persist(db,request.run_id,data,"application/json")
            staged=dict(operation.result or {})
            staged["ref"]=ref.model_dump(mode="json")
            staged["peer_task_terminal"]=json.loads(data).get("status",{}).get("state") in {
                "TASK_STATE_COMPLETED","TASK_STATE_FAILED","TASK_STATE_CANCELED","TASK_STATE_REJECTED"}
            db.execute(text("UPDATE operations SET result=CAST(:result AS jsonb) WHERE run_id=:run AND operation_id=:op AND state='unknown'"),
                       {"run":request.run_id,"op":request.operation_id,"result":json.dumps(staged,separators=(",",":"))})
            db.commit()
    except Exception:
        peer_receipt_runtime.mark_unknown(request.run_id,request.operation_id,"reconciliation_failed")
        return OperationResult(operation_id=request.operation_id,state="unknown",result=None,usage_tokens=None)
    # GetTask supplies no accounting contract. Reading/storing never settles tokens or resumes a run.
    return OperationResult(operation_id=request.operation_id,state="unknown",result=ref,usage_tokens=None)
