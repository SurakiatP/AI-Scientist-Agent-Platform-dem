from __future__ import annotations

from hashlib import sha256
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from scientist.auth import DomainError, authorize
from scientist.contracts import ArtifactView, CitationView, DecisionSubmit, FileView, FindingView, PendingDecisionView, PlanSpec, PlanView, MessageView, Principal, ProjectView, RunEvent, RunView, SessionView
from scientist import limits, settings

_SNAPSHOT_MAX_BYTES = 1024 * 1024


def submit_run(db: Session, principal: Principal, project_id: UUID, session_id: UUID,
               submission_key: str, question: str, input_ids: list[UUID],
               provider_id: UUID, model: str, retry_of: UUID | None = None) -> RunView:
    authorize(db, principal, "work:submit", project_id)
    if not 1 <= len(submission_key) <= 200 or not question.strip() or len(question) > 100000 or not model.strip() or len(model) > 200:
        raise DomainError("forbidden", 400)
    if len(input_ids) > 1000:
        raise DomainError("request_too_large", 413)
    request = {"question": question, "input_ids": [str(i) for i in input_ids], "provider_id": str(provider_id), "model": model}
    # retry_of joins the hash only when set, so pre-008 submission hashes stay valid.
    payload_hash = _digest({"project_id": str(project_id), "session_id": str(session_id), **request,
                            **({"retry_of": str(retry_of)} if retry_of else {})})
    # ponytail: one PostgreSQL advisory lock per caller/key; replace with per-key row locks only if contention matters.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"), {"lock_key": f"{principal.identity}:{submission_key}"})
    existing = db.execute(text("SELECT id, submission_hash FROM runs WHERE caller_identity = :caller AND submission_key = :key"), {
        "caller": principal.identity, "key": submission_key,
    }).one_or_none()
    if existing:
        if existing.submission_hash.strip() != payload_hash:
            raise DomainError("idempotency_conflict", 409)
        return _run_view(db, existing.id)
    valid_session = db.execute(text("SELECT 1 FROM sessions WHERE id = :session AND project_id = :project"), {
        "session": session_id, "project": project_id,
    }).scalar_one_or_none()
    if not valid_session:
        raise DomainError("not_found", 404)
    if len(input_ids) != len(set(input_ids)):
        raise DomainError("forbidden", 400)
    if retry_of:
        prior = db.execute(text("SELECT state, caller_identity FROM runs WHERE id = :id AND project_id = :project FOR UPDATE"),
                           {"id": retry_of, "project": project_id}).one_or_none()
        # Same own-submission rule as stop: external callers may only retry runs they submitted.
        if prior is None or (principal.kind == "external" and prior.caller_identity != principal.identity):
            raise DomainError("not_found", 404)
        if prior.state not in ("failed", "canceled"):
            raise DomainError("revision_conflict", 409)
    manifest = _capture_snapshot(db, project_id, session_id, request)
    encoded_manifest = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if len(encoded_manifest.encode()) > _SNAPSHOT_MAX_BYTES:
        raise DomainError("request_too_large", 413)
    digest = _digest(manifest)
    run_id = uuid4()
    db.execute(text("""
        INSERT INTO runs (id, project_id, session_id, caller_identity, submission_key, submission_hash,
                          state, token_limit, retry_of)
        VALUES (:id, :project, :session, :caller, :key, :hash, 'awaiting_approval', 0, :retry_of)
    """), {"id": run_id, "project": project_id, "session": session_id, "caller": principal.identity,
          "key": submission_key, "hash": payload_hash, "retry_of": retry_of})
    # Snapshot captured above, so the question is not in its own conversation; retries record their (possibly edited) question too.
    db.execute(text("INSERT INTO messages (id, project_id, session_id, role, content, run_id) VALUES (:id, :project, :session, 'user', :content, :run)"),
               {"id": uuid4(), "project": project_id, "session": session_id, "content": question, "run": run_id})
    db.execute(text("INSERT INTO input_snapshots (id, project_id, run_id, digest, manifest) VALUES (:id, :project, :run, :digest, CAST(:manifest AS jsonb))"), {
        "id": uuid4(), "project": project_id, "run": run_id, "digest": digest,
        "manifest": encoded_manifest,
    })
    plan = PlanSpec(input_snapshot_digest=digest, provider_id=provider_id, model=model, stages=["Review requested research"],
                    allowed_ops=[], data_recipients=[], packages=[], token_limit=0, elapsed_limit_ms=0)
    plan_digest = _plan_digest(plan)
    db.execute(text("UPDATE runs SET plan_digest = :digest WHERE id = :run"), {"digest": plan_digest, "run": run_id})
    _insert_plan(db, run_id, project_id, 1, plan, plan_digest)
    _event(db, run_id, 1, "run.state", {"state": "awaiting_approval"})
    return _run_view(db, run_id)


def revise_plan(db: Session, owner: Principal, run_id: UUID, expected_revision: int, plan: PlanSpec) -> RunView:
    if owner.kind != "owner":
        raise DomainError("forbidden", 403)
    row = _locked_run(db, owner, run_id, "plan:approve")
    if row.revision != expected_revision or row.state != "awaiting_approval":
        raise DomainError("revision_conflict", 409)
    snapshot_digest = db.execute(text("SELECT digest FROM input_snapshots WHERE run_id = :run"), {"run": run_id}).scalar_one()
    if plan.input_snapshot_digest != snapshot_digest:
        raise DomainError("revision_conflict", 409)
    allowed = settings.allowed_recipients(plan.provider_id)  # data_recipients come from configuration only; peers are checked by the broker
    if any(r not in allowed and not r.startswith("peer:") for r in plan.data_recipients):
        raise DomainError("data_destinations_not_configured", 409)
    digest = _plan_digest(plan)
    revision = row.revision + 1
    db.execute(text("UPDATE runs SET revision = :revision, plan_digest = :digest WHERE id = :run"), {
        "revision": revision, "digest": digest, "run": run_id,
    })
    _insert_plan(db, run_id, row.project_id, revision, plan, digest)
    _event(db, run_id, revision, "plan.ready", {"plan_digest": digest})
    return _run_view(db, run_id)


def approve_run(db: Session, owner: Principal, run_id: UUID, expected_revision: int, plan_digest: str) -> RunView:
    if owner.kind != "owner":
        raise DomainError("forbidden", 403)
    row = _locked_run(db, owner, run_id, "plan:approve")
    if row.revision != expected_revision or row.plan_digest != plan_digest or row.state != "awaiting_approval":
        raise DomainError("revision_conflict", 409)
    plan_record = db.execute(text("SELECT plan FROM plan_revisions WHERE run_id = :run AND revision = :revision"), {
        "run": run_id, "revision": row.revision,
    }).scalar_one()
    plan = PlanSpec.model_validate(plan_record)
    db.execute(text("INSERT INTO approvals (id, run_id, revision, project_id, owner_identity, plan_digest) VALUES (:id, :run, :revision, :project, :owner, :digest)"), {
        "id": uuid4(), "run": run_id, "revision": row.revision, "project": row.project_id,
        "owner": owner.identity, "digest": plan_digest,
    })
    updated = db.execute(text("""
        UPDATE runs SET state = 'queued', token_limit = :token_limit, elapsed_limit_ms = :elapsed_limit_ms
        WHERE id = :run AND usage_tokens + reserved_tokens <= :token_limit
        RETURNING id
    """), {"run": run_id, "token_limit": plan.token_limit, "elapsed_limit_ms": plan.elapsed_limit_ms}).scalar_one_or_none()
    if updated is None:
        raise DomainError("budget_exhausted", 409)
    _event(db, run_id, row.revision, "run.state", {"state": "queued"})
    return _run_view(db, run_id)


def extend_run_budget(
    db: Session,
    owner: Principal,
    run_id: UUID,
    expected_revision: int,
    decision_id: UUID,
    idempotency_key: str,
    token_limit: int,
    elapsed_limit_ms: int,
) -> RunView:
    """Record an immutable owner extension while keeping the run paused."""
    if owner.kind != "owner":
        raise DomainError("forbidden", 403)
    if (
        not isinstance(idempotency_key, str)
        or not 1 <= len(idempotency_key) <= 200
        or type(expected_revision) is not int
        or expected_revision < 1
        or type(token_limit) is not int
        or token_limit < 0
        or type(elapsed_limit_ms) is not int
        or elapsed_limit_ms < 0
    ):
        raise DomainError("forbidden", 400)

    row = _load_run(db, run_id, lock=True)
    payload_hash = _digest({
        "run_id": str(run_id),
        "revision": expected_revision,
        "decision_id": str(decision_id),
        "token_limit": token_limit,
        "elapsed_limit_ms": elapsed_limit_ms,
    })
    existing = db.execute(text("""
        SELECT caller_identity, payload_hash
        FROM run_budget_extensions
        WHERE run_id = :run AND idempotency_key = :key
    """), {"run": run_id, "key": idempotency_key}).one_or_none()
    if existing is not None:
        if existing.caller_identity != owner.identity:
            raise DomainError("forbidden", 403)
        if existing.payload_hash.strip() != payload_hash:
            raise DomainError("idempotency_conflict", 409)
        return _run_view(db, run_id)

    if row.revision != expected_revision:
        raise DomainError("revision_conflict", 409)
    if (
        row.state != "waiting_input"
        or row.waiting_reason != "budget_exhausted"
        or row.budget_decision_id != decision_id
    ):
        raise DomainError("revision_conflict", 409)
    if token_limit < row.token_limit or elapsed_limit_ms < row.elapsed_limit_ms:
        raise DomainError("forbidden", 400)
    if token_limit == row.token_limit and elapsed_limit_ms == row.elapsed_limit_ms:
        raise DomainError("forbidden", 400)

    db.execute(text("""
        INSERT INTO run_budget_extensions (
            id, run_id, idempotency_key, caller_identity, expected_revision,
            payload_hash, token_limit_before, token_limit_after,
            elapsed_limit_before_ms, elapsed_limit_after_ms
        ) VALUES (
            :id, :run, :key, :caller, :revision, :hash,
            :token_before, :token_after, :elapsed_before, :elapsed_after
        )
    """), {
        "id": uuid4(),
        "run": run_id,
        "key": idempotency_key,
        "caller": owner.identity,
        "revision": expected_revision,
        "hash": payload_hash,
        "token_before": row.token_limit,
        "token_after": token_limit,
        "elapsed_before": row.elapsed_limit_ms,
        "elapsed_after": elapsed_limit_ms,
    })
    db.execute(text("""
        UPDATE runs
        SET token_limit = :token_limit, elapsed_limit_ms = :elapsed_limit_ms,
            budget_decision_id = NULL
        WHERE id = :run AND state = 'waiting_input'
    """), {
        "token_limit": token_limit,
        "elapsed_limit_ms": elapsed_limit_ms,
        "run": run_id,
    })
    _event(db, run_id, row.revision, "usage.updated", {
        "usage_tokens": row.usage_tokens,
        "reserved_tokens": row.reserved_tokens,
        "token_limit": token_limit,
    })
    updated = _load_run(db, run_id)
    if limits.budget_exhausted(db, updated):
        limits.mark_budget_wait(db, run_id, row.revision, _event)
    db.commit()
    return _run_view(db, run_id)


def authorize_stop(db: Session, principal: Principal, run_id: UUID):
    """work:cancel plus the external own-submission rule; returns the locked run row."""
    row = _locked_run(db, principal, run_id, "work:cancel")
    if principal.kind == "external" and row.caller_identity != principal.identity:
        raise DomainError("forbidden", 403)
    return row


def request_stop(db: Session, principal: Principal, run_id: UUID) -> RunView:
    row = authorize_stop(db, principal, run_id)
    if row.state not in {"completed", "failed", "canceled", "rejected"}:
        db.execute(text("UPDATE runs SET cancel_requested = true, state = 'stopping' WHERE id = :run"), {"run": run_id})
        if row.state != "stopping":
            _event(db, run_id, row.revision, "run.state", {"state": "stopping"})
    return _run_view(db, run_id)


def submit_decision(db: Session, principal: Principal, run_id: UUID, body: DecisionSubmit) -> RunView:
    """Resolve one owner decision exactly once; the receipt commits with the resolution (D1-D3)."""
    from scientist import broker  # deferred: broker imports this module
    if principal.kind != "owner":
        raise DomainError("forbidden", 403)
    run = _load_run(db, run_id, lock=True)
    payload_hash = _digest({"run_id": str(run_id), **body.model_dump(mode="json", exclude={"idempotency_key"})})
    receipt = db.execute(text("SELECT payload_hash FROM owner_decisions WHERE run_id = :run AND idempotency_key = :key"),
                         {"run": run_id, "key": body.idempotency_key}).scalar_one_or_none()
    if receipt is not None:
        if receipt.strip() != payload_hash:
            raise DomainError("idempotency_conflict", 409)
        # D3 replay returns the CURRENT RunView; side effects (retry, extension) are never repeated.
        return _run_view(db, run_id)
    mapped = db.execute(text("SELECT * FROM owner_decisions WHERE decision_id = :id AND run_id = :run FOR UPDATE"),
                        {"id": body.decision_id, "run": run_id}).one_or_none()
    budget = mapped is None and run.budget_decision_id == body.decision_id
    if mapped is None and not budget:
        raise DomainError("not_found", 404)
    if mapped is not None and mapped.state != "pending":
        raise DomainError("revision_conflict", 409)
    if run.revision != body.expected_revision:
        raise DomainError("revision_conflict", 409)
    if not budget:
        if body.choice == "extend":
            raise DomainError("forbidden", 400)
        if body.choice == "confirm_usage" or run.state == "canceled":
            return _confirm_canceled_usage(db, run, mapped, body, payload_hash, broker)
        candidates = db.execute(text("""
            SELECT count(*) FROM operations WHERE run_id = :run AND state = 'unknown'
              AND NOT COALESCE(result ? 'retry_identity', false)
              AND NOT (COALESCE(result ->> 'usage_known', 'false') = 'true' AND result ? 'ref')
        """), {"run": run_id}).scalar_one()
        if candidates > 1:
            raise DomainError("decision_ambiguous", 409)
        return broker.resolve_unknown(db, principal, run_id, mapped.operation_id, body.choice, body.result,
                                      queued_only=True, receipt=(body.idempotency_key, payload_hash))
    if body.choice not in {"extend", "stop"}:
        raise DomainError("forbidden", 400)
    if run.state != "waiting_input" or run.waiting_reason != "budget_exhausted":
        raise DomainError("revision_conflict", 409)
    db.execute(text("""
        INSERT INTO owner_decisions (decision_id, run_id, reason, state, resolution, idempotency_key, payload_hash, resolved_at)
        VALUES (:id, :run, 'budget_exhausted', 'resolved', CAST(:resolution AS jsonb), :key, :hash, now())
    """), {"id": body.decision_id, "run": run_id, "resolution": json.dumps({"choice": body.choice}),
           "key": body.idempotency_key, "hash": payload_hash})
    if body.choice == "stop":
        db.execute(text("UPDATE runs SET cancel_requested = true, state = 'stopping', waiting_reason = NULL WHERE id = :run"), {"run": run_id})
        _event(db, run_id, run.revision, "run.state", {"state": "stopping"})
        db.commit()
        return _run_view(db, run_id)
    try:
        return extend_run_budget(db, principal, run_id, body.expected_revision, body.decision_id, body.idempotency_key,
                                 run.token_limit + body.add_tokens, run.elapsed_limit_ms + body.add_elapsed_ms)
    except Exception:
        db.rollback()
        raise


def _confirm_canceled_usage(db: Session, run, mapped, body: DecisionSubmit, payload_hash: str, broker) -> RunView:
    """Owner-confirmed usage for the one operation a canceled run left unknown (never an automatic release)."""
    if body.choice != "confirm_usage":
        raise DomainError("revision_conflict", 409)  # canceled: only confirm_usage is meaningful
    if run.state != "canceled":
        raise DomainError("forbidden", 400)
    operation = db.execute(text("SELECT * FROM operations WHERE run_id = :run AND operation_id = :op FOR UPDATE"),
                           {"run": run.id, "op": mapped.operation_id}).one_or_none()
    pending = (operation.result or {}) if operation is not None else {}
    if (operation is None or operation.state != "unknown" or pending.get("retry_identity")
            or pending.get("usage_known") or not broker._quiescent(run, operation)):
        raise DomainError("revision_conflict", 409)
    try:
        limits.confirm_unknown_usage(db, run.id, operation, body.usage_tokens, _event)
    except ValueError as exc:
        db.rollback()
        raise DomainError("forbidden" if "exceeds" in str(exc) else "revision_conflict", 400 if "exceeds" in str(exc) else 409) from exc
    db.execute(text("""
        UPDATE owner_decisions SET state = 'resolved', resolved_at = now(), resolution = CAST(:resolution AS jsonb),
            idempotency_key = :key, payload_hash = :hash
        WHERE decision_id = :id AND state = 'pending'
    """), {"resolution": json.dumps({"choice": "confirm_usage", "usage_tokens": body.usage_tokens}),
           "key": body.idempotency_key, "hash": payload_hash, "id": body.decision_id})
    db.commit()
    return _run_view(db, run.id)


def get_run(db: Session, principal: Principal, run_id: UUID) -> RunView:
    row = _load_run(db, run_id)
    _authorize_run(db, principal, "result:read", row.project_id)
    return _run_view(db, run_id)


def get_pending_decisions(db: Session, owner: Principal, run_id: UUID) -> list[PendingDecisionView]:
    """Return only owner decisions that are actionable for the run's current state."""
    if owner.kind != "owner":
        raise DomainError("forbidden", 403)
    run = _load_run(db, run_id)
    _authorize_run(db, owner, "result:read", run.project_id)
    if run.state not in {"waiting_input", "canceled"}:
        return []

    pending: list[tuple[int, PendingDecisionView]] = []
    if run.state == "waiting_input" and run.waiting_reason == "budget_exhausted" and run.budget_decision_id:
        row = db.execute(text("""
            SELECT sequence, payload FROM events
            WHERE run_id=:run AND kind='decision.required' AND payload->>'decision_id'=:decision
            ORDER BY sequence DESC LIMIT 1
        """), {"run": run_id, "decision": str(run.budget_decision_id)}).mappings().one_or_none()
        if row is not None:
            payload = PendingDecisionView.model_validate(row["payload"])
            if payload.reason == "budget_exhausted":
                pending.append((row["sequence"], payload))

    if run.state == "canceled" or (run.state == "waiting_input" and run.waiting_reason == "unknown_outcome"):
        from scientist import broker  # deferred: broker imports this module

        rows = db.execute(text("""
            SELECT d.decision_id, d.reason, o.operation_id, o.generation, o.reserve_tokens, o.result,
                   COALESCE((SELECT min(e.sequence) FROM events e
                     WHERE e.run_id=d.run_id AND e.kind='decision.required'
                       AND e.payload->>'decision_id'=d.decision_id::text), 2147483647) AS sequence
            FROM owner_decisions d
            JOIN operations o ON o.run_id=d.run_id AND o.operation_id=d.operation_id
            WHERE d.run_id=:run AND d.state='pending' AND d.reason='unknown_outcome'
              AND o.state='unknown'
              AND NOT COALESCE(o.result ? 'retry_identity', false)
              AND NOT (COALESCE(o.result->>'usage_known', 'false')='true' AND o.result ? 'ref')
            ORDER BY sequence, d.decision_id
        """), {"run": run_id}).mappings().all()
        for row in rows:
            if not broker._quiescent(run, row):
                continue
            pending.append((row["sequence"], PendingDecisionView(
                decision_id=row["decision_id"], reason="unknown_outcome",
                operation_reserved_tokens=row["reserve_tokens"],
            )))
    return [decision for _, decision in sorted(pending, key=lambda item: (item[0], str(item[1].decision_id)))]


def get_plan(db: Session, owner: Principal, run_id: UUID) -> PlanView:
    if owner.kind != "owner":
        raise DomainError("forbidden", 403)
    row = _load_run(db, run_id)
    plan_row = db.execute(text("SELECT revision, digest, plan FROM plan_revisions WHERE run_id = :run AND revision = :revision"), {
        "run": run_id, "revision": row.revision,
    }).one()
    return PlanView(run_id=run_id, revision=plan_row.revision, plan_digest=plan_row.digest.strip(), plan=PlanSpec.model_validate(plan_row.plan))


def get_events(db: Session, principal: Principal, run_id: UUID, after: int, limit: int = 100) -> list[dict[str, Any]]:
    if after < 0 or not 1 <= limit <= 200:
        raise DomainError("cursor_expired", 400)
    row = _load_run(db, run_id)
    _authorize_run(db, principal, "result:read", row.project_id)
    events = []
    for event in db.execute(text("""
        SELECT 1 AS schema_version, run_id, sequence, revision, occurred_at, kind, payload
        FROM events WHERE run_id = :run AND sequence > :after ORDER BY sequence LIMIT :limit
    """), {"run": run_id, "after": after, "limit": limit}).mappings():
        item = dict(event)
        item["occurred_at"] = item["occurred_at"].astimezone(timezone.utc)
        events.append(RunEvent.model_validate(item).model_dump(mode="json"))
    return events


def _locked_run(db: Session, principal: Principal, run_id: UUID, action: str):
    row = _load_run(db, run_id, lock=True)
    _authorize_run(db, principal, action, row.project_id)
    return row


def _authorize_run(db: Session, principal: Principal, action: str, project_id: UUID) -> None:
    try:
        authorize(db, principal, action, project_id)
    except DomainError as exc:
        if principal.kind == "external":
            raise DomainError("not_found", 404) from exc
        raise


def _load_run(db: Session, run_id: UUID, lock: bool = False):
    suffix = " FOR UPDATE" if lock else ""
    row = db.execute(text("SELECT * FROM runs WHERE id = :run" + suffix), {"run": run_id}).one_or_none()
    if row is None:
        raise DomainError("not_found", 404)
    return row


def _run_view(db: Session, run_id: UUID) -> RunView:
    row = _load_run(db, run_id)
    artifacts = [ArtifactView(artifact_id=a.id, project_id=a.project_id, run_id=a.run_id, title=a.title,
                              kind=a.kind, sha256=a.sha256.strip(), size=a.size, content_type=a.content_type,
                              partial=a.partial) for a in db.execute(text("SELECT * FROM artifacts WHERE run_id = :run"), {"run": run_id})]
    return RunView(run_id=row.id, project_id=row.project_id, session_id=row.session_id, revision=row.revision,
                   state=row.state, stage=row.stage, waiting_reason=row.waiting_reason, error_code=row.error_code,
                   plan_digest=row.plan_digest.strip() if row.plan_digest else None, latest_cursor=row.latest_cursor,
                   usage_tokens=row.usage_tokens, reserved_tokens=row.reserved_tokens, planning_tokens=row.planning_tokens,
                   token_limit=row.token_limit, artifacts=artifacts, retry_of=row.retry_of)


def _insert_plan(db: Session, run_id: UUID, project_id: UUID, revision: int, plan: PlanSpec, digest: str) -> None:
    db.execute(text("INSERT INTO plan_revisions (run_id, project_id, revision, digest, plan) VALUES (:run, :project, :revision, :digest, CAST(:plan AS jsonb))"), {
        "run": run_id, "project": project_id, "revision": revision, "digest": digest,
        "plan": json.dumps(plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
    })


def _event(db: Session, run_id: UUID, revision: int, kind: str, payload: dict[str, Any]) -> None:
    sequence = db.execute(text("SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE run_id = :run"), {"run": run_id}).scalar_one()
    event = RunEvent(schema_version=1, run_id=run_id, sequence=sequence, revision=revision,
                     occurred_at=datetime.now(timezone.utc), kind=kind, payload=payload)
    db.execute(text("""
        INSERT INTO events (run_id, sequence, revision, occurred_at, kind, payload)
        VALUES (:run, :sequence, :revision, :occurred_at, :kind, CAST(:payload AS jsonb))
    """), {
        "run": run_id, "sequence": sequence, "revision": revision, "occurred_at": event.occurred_at,
        "kind": event.kind, "payload": json.dumps(event.payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
    })
    db.execute(text("UPDATE runs SET latest_cursor = :sequence WHERE id = :run"), {"sequence": sequence, "run": run_id})


def _plan_digest(plan: PlanSpec) -> str:
    return _digest(plan.model_dump(mode="json"))


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return sha256(body).hexdigest()


def _capture_snapshot(db: Session, project_id: UUID, session_id: UUID, request: dict[str, Any]) -> dict[str, Any]:
    raw = db.execute(text("""
        /* scientist.snapshot.capture */
        WITH project_row AS MATERIALIZED (
            SELECT id, revision, instructions FROM projects WHERE id = :project
        ), conversation AS (
            SELECT COALESCE(jsonb_agg(jsonb_build_object(
                       'id', id::text, 'sequence', sequence, 'role', role, 'content', content
                   ) ORDER BY sequence), '[]'::jsonb) AS data,
                   COALESCE(sum(octet_length(content) + 256), 0) AS size
            FROM messages WHERE project_id = :project AND session_id = :session
        ), citations_by_finding AS (
            SELECT fc.finding_id,
                   jsonb_agg(jsonb_build_object(
                       'id', c.id::text, 'title', c.title, 'authors', c.authors, 'year', c.year,
                       'identifier', c.identifier, 'original_url', c.original_url, 'access', c.access,
                       'verification', c.verification, 'source_metadata', s.metadata
                   ) ORDER BY c.id) AS data,
                   sum(octet_length(c.title) + octet_length(c.authors::text)
                       + COALESCE(octet_length(c.identifier), 0) + COALESCE(octet_length(c.original_url), 0)
                       + COALESCE(octet_length(s.metadata::text), 0) + 512) AS size
            FROM finding_citations fc
            JOIN citations c ON c.id = fc.citation_id AND c.project_id = fc.project_id
            LEFT JOIN sources s ON s.id = c.source_id AND s.project_id = c.project_id
            WHERE fc.project_id = :project GROUP BY fc.finding_id
        ), finding_detail AS (
            SELECT f.id, f.session_id, f.artifact_id, f.text, f.citation_ids,
                   to_jsonb(f.citation_ids) AS citation_ids_json,
                   COALESCE(cf.data, '[]'::jsonb) AS citations,
                   COALESCE(cf.size, 0) AS citation_size,
                   CASE WHEN f.artifact_id IS NULL THEN 'null'::jsonb ELSE jsonb_build_object(
                       'id', a.id::text, 'title', a.title, 'kind', a.kind, 'object_key', a.object_key,
                       'sha256', a.sha256, 'size', a.size, 'content_type', a.content_type, 'partial', a.partial
                   ) END AS artifact,
                   CASE WHEN f.artifact_id IS NULL THEN 0 ELSE
                       COALESCE(octet_length(a.title), 0) + COALESCE(octet_length(a.object_key), 0)
                       + COALESCE(octet_length(a.content_type), 0) + 512 END AS artifact_size
            FROM findings f
            LEFT JOIN artifacts a ON a.id = f.artifact_id AND a.project_id = f.project_id
            LEFT JOIN citations_by_finding cf ON cf.finding_id = f.id
            WHERE f.project_id = :project
        ), findings AS (
            SELECT COALESCE(jsonb_agg(jsonb_build_object(
                       'id', id::text, 'session_id', session_id::text,
                       'artifact_id', artifact_id::text, 'artifact', artifact, 'text', text,
                       'citation_ids', citation_ids_json, 'citations', citations
                   ) ORDER BY id), '[]'::jsonb) AS data,
                   COALESCE(sum(octet_length(text) + 512 + cardinality(citation_ids) * 40
                               + citation_size + artifact_size), 0) AS size
            FROM finding_detail
        ), selected_inputs AS (
            SELECT picked.id, picked.ordinality
            FROM unnest(CAST(:input_ids AS uuid[])) WITH ORDINALITY AS picked(id, ordinality)
        ), file_detail AS (
            SELECT picked.id AS requested_id, picked.ordinality, v.id, v.filename, v.object_key,
                   v.sha256, v.size, v.content_type, v.state
            FROM selected_inputs picked
          LEFT JOIN file_versions v ON v.id = picked.id AND v.project_id = :project
                                   AND v.tombstoned_at IS NULL
        ), files AS (
            SELECT COALESCE(jsonb_agg(jsonb_build_object(
                       'requested_id', requested_id::text, 'id', id::text, 'filename', filename,
                       'object_key', object_key, 'sha256', sha256, 'size', size,
                       'content_type', content_type, 'state', state
                   ) ORDER BY ordinality), '[]'::jsonb) AS data,
                   COALESCE(sum(COALESCE(octet_length(filename), 0) + COALESCE(octet_length(object_key), 0)
                               + COALESCE(octet_length(sha256), 0) + COALESCE(octet_length(content_type), 0) + 256), 0) AS size
            FROM file_detail
        ), measured AS (
            SELECT project_row.*, conversation.data AS conversation, findings.data AS findings, files.data AS files,
                   octet_length(project_row.instructions) + conversation.size + findings.size + files.size
                   + :question_size + 512 AS total_size
            FROM project_row CROSS JOIN conversation CROSS JOIN findings CROSS JOIN files
        )
        SELECT id::text AS project_id, revision,
               total_size > :snapshot_max AS oversized,
               CASE WHEN total_size > :snapshot_max THEN NULL ELSE instructions END AS instructions,
               CASE WHEN total_size > :snapshot_max THEN NULL ELSE conversation END AS conversation,
               CASE WHEN total_size > :snapshot_max THEN NULL ELSE findings END AS findings,
               CASE WHEN total_size > :snapshot_max THEN NULL ELSE files END AS files
        FROM measured
    """), {
        "project": project_id, "session": session_id,
        "input_ids": [UUID(value) for value in request["input_ids"]],
        "question_size": len(request["question"].encode()), "snapshot_max": _SNAPSHOT_MAX_BYTES,
    }).mappings().one_or_none()
    if raw is None:
        raise DomainError("not_found", 404)
    if raw["oversized"]:
        raise DomainError("request_too_large", 413)
    conversation, findings, files = raw["conversation"], raw["findings"], raw["files"]
    if len(files) != len(request["input_ids"]):
        raise DomainError("storage_unavailable", 503)
    for finding in findings:
        citation_ids = set(finding["citation_ids"] or [])
        if citation_ids != {citation["id"] for citation in finding["citations"]}:
            raise DomainError("storage_unavailable", 503)
        artifact = finding["artifact"]
        if finding["artifact_id"]:
            digest = (artifact["sha256"] or "").strip()
            if (not artifact["id"] or not artifact["object_key"] or not artifact["object_key"].strip()
                    or not re.fullmatch(r"[a-fA-F0-9]{64}", digest) or artifact["size"] < 0
                    or not artifact["content_type"].strip()):
                raise DomainError("storage_unavailable", 503)
            artifact["sha256"] = digest
    captured_files = []
    for selected in files:
        if selected["id"] is None or selected["id"] != selected["requested_id"]:
            raise DomainError("not_found", 404)
        if selected["state"] != "ready":
            raise DomainError("not_found", 404)
        digest = (selected["sha256"] or "").strip()
        if (not selected["object_key"] or not selected["object_key"].strip()
                or not re.fullmatch(r"[a-fA-F0-9]{64}", digest) or selected["size"] < 0
                or not selected["content_type"].strip()):
            raise DomainError("storage_unavailable", 503)
        captured_files.append({key: selected[key] for key in ("id", "filename", "object_key", "size", "content_type")} | {"sha256": digest})
    return {
        "project": {"id": raw["project_id"], "revision": raw["revision"], "instructions": raw["instructions"]},
        "question": request["question"], "provider_id": request["provider_id"], "model": request["model"],
        "conversation": conversation, "findings": findings, "files": captured_files,
    }


# --- Project resources (single policy source for REST and later protocol adapters) ---

def _project_access(db: Session, principal: Principal, action: str, project_id: UUID) -> None:
    """Missing and ungranted projects look identical to external callers."""
    exists = db.execute(text("SELECT 1 FROM projects WHERE id = :id"), {"id": project_id}).scalar_one_or_none()
    if not exists:
        raise DomainError("not_found", 404)
    _authorize_run(db, principal, action, project_id)


def _project_view(row) -> ProjectView:
    return ProjectView(id=row.id, name=row.name, revision=row.revision, instructions=row.instructions)


def list_projects(db: Session, principal: Principal, after: UUID | None, limit: int = 100) -> list[ProjectView]:
    if not 1 <= limit <= 200:
        raise DomainError("cursor_expired", 400)
    if principal.kind == "owner":
        scope, params = "TRUE", {}
    elif principal.kind == "external":
        scope = """id IN (SELECT g.project_id FROM access_grants g JOIN access_tokens t ON t.id = g.token_id
                          WHERE t.id = :token AND t.revoked_at IS NULL AND (t.expires_at IS NULL OR t.expires_at > now())
                            AND 'project:read' = ANY(g.actions))"""
        params = {"token": principal.identity}
    else:
        raise DomainError("forbidden", 403)
    rows = db.execute(text(f"SELECT id, name, revision, instructions FROM projects WHERE {scope} "
                           "AND (CAST(:after AS uuid) IS NULL OR id > CAST(:after AS uuid)) ORDER BY id LIMIT :limit"),
                      {**params, "after": after, "limit": limit}).all()
    return [_project_view(r) for r in rows]


def create_project(db: Session, owner: Principal, name: str, instructions: str = "") -> ProjectView:
    if owner.kind != "owner":
        raise DomainError("forbidden", 403)
    if not name.strip() or len(name) > 200 or len(instructions) > 100000:
        raise DomainError("forbidden", 400)
    project_id = uuid4()
    db.execute(text("INSERT INTO projects (id, name, instructions) VALUES (:id, :name, :instructions)"),
               {"id": project_id, "name": name, "instructions": instructions})
    return get_project(db, owner, project_id)


def get_project(db: Session, principal: Principal, project_id: UUID) -> ProjectView:
    _project_access(db, principal, "project:read", project_id)
    return _project_view(db.execute(text("SELECT id, name, revision, instructions FROM projects WHERE id = :id"), {"id": project_id}).one())


def update_project(db: Session, owner: Principal, project_id: UUID, expected_revision: int,
                   name: str | None = None, instructions: str | None = None) -> ProjectView:
    if owner.kind != "owner":
        raise DomainError("forbidden", 403)
    if (name is not None and (not name.strip() or len(name) > 200)) or (instructions is not None and len(instructions) > 100000):
        raise DomainError("forbidden", 400)
    row = db.execute(text("SELECT revision FROM projects WHERE id = :id FOR UPDATE"), {"id": project_id}).one_or_none()
    if row is None:
        raise DomainError("not_found", 404)
    if row.revision != expected_revision:
        raise DomainError("revision_conflict", 409)
    db.execute(text("UPDATE projects SET name = COALESCE(:name, name), instructions = COALESCE(:instructions, instructions), "
                    "revision = revision + 1 WHERE id = :id"), {"id": project_id, "name": name, "instructions": instructions})
    return get_project(db, owner, project_id)


def list_sessions(db: Session, principal: Principal, project_id: UUID) -> list[SessionView]:
    _project_access(db, principal, "project:read", project_id)
    return [SessionView(id=r.id, project_id=r.project_id, title=r.title) for r in db.execute(
        text("SELECT id, project_id, title FROM sessions WHERE project_id = :p ORDER BY created_at, id"), {"p": project_id})]


def create_session(db: Session, owner: Principal, project_id: UUID, title: str) -> SessionView:
    if owner.kind != "owner":
        raise DomainError("forbidden", 403)
    if not title.strip() or len(title) > 200:
        raise DomainError("forbidden", 400)
    _project_access(db, owner, "project:read", project_id)
    session_id = uuid4()
    db.execute(text("INSERT INTO sessions (id, project_id, title) VALUES (:id, :p, :title)"), {"id": session_id, "p": project_id, "title": title})
    return SessionView(id=session_id, project_id=project_id, title=title)


def session_project(db: Session, principal: Principal, session_id: UUID, action: str = "project:read") -> UUID:
    """Resolve and authorize in one step so missing and ungranted sessions look identical."""
    project_id = db.execute(text("SELECT project_id FROM sessions WHERE id = :id"), {"id": session_id}).scalar_one_or_none()
    if project_id is None:
        raise DomainError("not_found", 404)
    _project_access(db, principal, action, project_id)
    return project_id


def list_messages(db: Session, principal: Principal, session_id: UUID, after_sequence: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    if after_sequence < 0 or not 1 <= limit <= 500:
        raise DomainError("cursor_expired", 400)
    session_project(db, principal, session_id)
    return [MessageView(id=r.id, sequence=r.sequence, role=r.role, content=r.content, created_at=r.created_at.astimezone(timezone.utc), run_id=r.run_id).model_dump(mode="json")
            for r in db.execute(text("SELECT id, sequence, role, content, created_at, run_id FROM messages WHERE session_id = :s AND sequence > :a ORDER BY sequence LIMIT :l"),
                                {"s": session_id, "a": after_sequence, "l": limit})]


def list_runs(db: Session, principal: Principal, project_id: UUID) -> list[RunView]:
    _project_access(db, principal, "result:read", project_id)
    ids = db.execute(text("SELECT id FROM runs WHERE project_id = :p ORDER BY (SELECT min(occurred_at) FROM events WHERE run_id = runs.id), id"), {"p": project_id}).scalars().all()
    return [_run_view(db, i) for i in ids]


def list_files(db: Session, principal: Principal, project_id: UUID) -> list[FileView]:
    _project_access(db, principal, "project:read", project_id)
    return [FileView(id=r.id, project_id=r.project_id, filename=r.filename, size=r.size, content_type=r.content_type, state=r.state, error_code=r.error_code)
            for r in db.execute(text("SELECT id, project_id, filename, size, content_type, state, error_code FROM file_versions "
                                     "WHERE project_id = :p AND tombstoned_at IS NULL ORDER BY created_at, id"), {"p": project_id})]


def get_file(db: Session, principal: Principal, project_id: UUID, file_id: UUID):
    """Row of an untombstoned file version, only when it belongs to the addressed project."""
    _project_access(db, principal, "project:read", project_id)
    row = db.execute(text("SELECT id, project_id, filename, object_key, sha256, size, content_type, state FROM file_versions "
                          "WHERE id = :id AND project_id = :p AND tombstoned_at IS NULL"), {"id": file_id, "p": project_id}).one_or_none()
    if row is None:
        raise DomainError("not_found", 404)
    return row


def list_findings(db: Session, principal: Principal, project_id: UUID) -> list[FindingView]:
    _project_access(db, principal, "project:read", project_id)
    return [FindingView(id=r.id, project_id=r.project_id, session_id=r.session_id, artifact_id=r.artifact_id, text=r.text, citation_ids=list(r.citation_ids))
            for r in db.execute(text("SELECT id, project_id, session_id, artifact_id, text, citation_ids FROM findings WHERE project_id = :p ORDER BY id"), {"p": project_id})]


def save_finding(db: Session, owner: Principal, project_id: UUID, session_id: UUID, finding_text: str,
                 artifact_id: UUID | None, citation_ids: list[UUID]) -> FindingView:
    if owner.kind != "owner":
        raise DomainError("forbidden", 403)
    _project_access(db, owner, "project:read", project_id)
    if not finding_text.strip() or len(finding_text) > 100000 or len(citation_ids) > 1000 or len(set(citation_ids)) != len(citation_ids):
        raise DomainError("forbidden", 400)
    # Every referenced object must live in the addressed project; foreign ids look nonexistent.
    if not db.execute(text("SELECT 1 FROM sessions WHERE id = :s AND project_id = :p"), {"s": session_id, "p": project_id}).scalar_one_or_none():
        raise DomainError("not_found", 404)
    if artifact_id is not None and not db.execute(text("SELECT 1 FROM artifacts WHERE id = :a AND project_id = :p"), {"a": artifact_id, "p": project_id}).scalar_one_or_none():
        raise DomainError("not_found", 404)
    if citation_ids and db.execute(text("SELECT count(*) FROM citations WHERE project_id = :p AND id = ANY(:ids)"), {"p": project_id, "ids": citation_ids}).scalar_one() != len(citation_ids):
        raise DomainError("not_found", 404)
    finding_id = uuid4()
    db.execute(text("INSERT INTO findings (id, project_id, session_id, artifact_id, text, citation_ids) VALUES (:id, :p, :s, :a, :t, :c)"),
               {"id": finding_id, "p": project_id, "s": session_id, "a": artifact_id, "t": finding_text, "c": citation_ids})
    for citation_id in citation_ids:
        db.execute(text("INSERT INTO finding_citations (finding_id, citation_id, project_id) VALUES (:f, :c, :p)"), {"f": finding_id, "c": citation_id, "p": project_id})
    return FindingView(id=finding_id, project_id=project_id, session_id=session_id, artifact_id=artifact_id, text=finding_text, citation_ids=citation_ids)


def remove_finding(db: Session, owner: Principal, project_id: UUID, finding_id: UUID) -> None:
    """Removes the shared finding for future runs; captured input snapshots stay immutable."""
    if owner.kind != "owner":
        raise DomainError("forbidden", 403)
    _project_access(db, owner, "project:read", project_id)
    if not db.execute(text("SELECT 1 FROM findings WHERE id = :f AND project_id = :p FOR UPDATE"), {"f": finding_id, "p": project_id}).scalar_one_or_none():
        raise DomainError("not_found", 404)
    db.execute(text("DELETE FROM finding_citations WHERE finding_id = :f"), {"f": finding_id})
    db.execute(text("DELETE FROM findings WHERE id = :f"), {"f": finding_id})


def _citation_view(r) -> CitationView:
    return CitationView(id=r.id, title=r.title, authors=list(r.authors or []), year=r.year, identifier=r.identifier,
                        original_url=r.original_url, access=r.access, verification=r.verification)


_CITATION_COLUMNS = "id, title, authors, year, identifier, original_url, access, verification"


def list_citations(db: Session, principal: Principal, project_id: UUID) -> list[CitationView]:
    _project_access(db, principal, "project:read", project_id)
    return [_citation_view(r) for r in db.execute(text(f"SELECT {_CITATION_COLUMNS} FROM citations WHERE project_id = :p ORDER BY id"), {"p": project_id})]


def get_citation(db: Session, principal: Principal, citation_id: UUID) -> CitationView:
    row = db.execute(text(f"SELECT project_id, {_CITATION_COLUMNS} FROM citations WHERE id = :id"), {"id": citation_id}).one_or_none()
    if row is None:
        raise DomainError("not_found", 404)
    _project_access(db, principal, "project:read", row.project_id)
    return _citation_view(row)


def list_artifacts(db: Session, principal: Principal, run_id: UUID) -> list[ArtifactView]:
    row = _load_run(db, run_id)
    _authorize_run(db, principal, "result:read", row.project_id)
    return _run_view(db, run_id).artifacts


def get_artifact(db: Session, principal: Principal, artifact_id: UUID):
    row = db.execute(text("SELECT id, project_id, object_key, sha256, size, content_type FROM artifacts WHERE id = :id"), {"id": artifact_id}).one_or_none()
    if row is None:
        raise DomainError("not_found", 404)
    _authorize_run(db, principal, "result:read", row.project_id)
    return row
