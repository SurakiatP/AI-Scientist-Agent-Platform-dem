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
from scientist.contracts import ArtifactView, PlanSpec, PlanView, Principal, RunEvent, RunView
from scientist import limits

_SNAPSHOT_MAX_BYTES = 1024 * 1024


def submit_run(db: Session, principal: Principal, project_id: UUID, session_id: UUID,
               submission_key: str, question: str, input_ids: list[UUID],
               provider_id: UUID, model: str) -> RunView:
    authorize(db, principal, "work:submit", project_id)
    if not 1 <= len(submission_key) <= 200 or not question.strip() or len(question) > 100000 or not model.strip() or len(model) > 200:
        raise DomainError("forbidden", 400)
    if len(input_ids) > 1000:
        raise DomainError("request_too_large", 413)
    request = {"question": question, "input_ids": [str(i) for i in input_ids], "provider_id": str(provider_id), "model": model}
    payload_hash = _digest({"project_id": str(project_id), "session_id": str(session_id), **request})
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
    manifest = _capture_snapshot(db, project_id, session_id, request)
    encoded_manifest = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if len(encoded_manifest.encode()) > _SNAPSHOT_MAX_BYTES:
        raise DomainError("request_too_large", 413)
    digest = _digest(manifest)
    run_id = uuid4()
    db.execute(text("""
        INSERT INTO runs (id, project_id, session_id, caller_identity, submission_key, submission_hash,
                          state, token_limit)
        VALUES (:id, :project, :session, :caller, :key, :hash, 'awaiting_approval', 0)
    """), {"id": run_id, "project": project_id, "session": session_id, "caller": principal.identity,
          "key": submission_key, "hash": payload_hash})
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


def request_stop(db: Session, principal: Principal, run_id: UUID) -> RunView:
    row = _locked_run(db, principal, run_id, "work:cancel")
    if principal.kind == "external" and row.caller_identity != principal.identity:
        raise DomainError("forbidden", 403)
    if row.state not in {"completed", "failed", "canceled", "rejected"}:
        db.execute(text("UPDATE runs SET cancel_requested = true, state = 'stopping' WHERE id = :run"), {"run": run_id})
        if row.state != "stopping":
            _event(db, run_id, row.revision, "run.state", {"state": "stopping"})
    return _run_view(db, run_id)


def get_run(db: Session, principal: Principal, run_id: UUID) -> RunView:
    row = _load_run(db, run_id)
    _authorize_run(db, principal, "result:read", row.project_id)
    return _run_view(db, run_id)


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
                   token_limit=row.token_limit, artifacts=artifacts)


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
