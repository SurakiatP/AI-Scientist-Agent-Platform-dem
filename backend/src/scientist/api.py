"""Owner REST resource routes. Routers are mounted by the host composition, not here."""
from __future__ import annotations

import asyncio
import json
import re
import time
from urllib.parse import quote
from tempfile import SpooledTemporaryFile
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import text
from typing import Annotated
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from scientist import domain, files, objects, research, supervisor, profile_preparation
from scientist import secrets as secret_store
from scientist.auth import DomainError, authenticate_owner_session
from scientist.contracts import DecisionSubmit, ObjectRef, PlanSpec, Principal, PreparationSubmit
from scientist.db import session as database_session

MAX_UPLOAD_BYTES = objects.MAX_UPLOAD_BYTES
SSE_POLL_SECONDS = 1.0
SSE_HEARTBEAT_SECONDS = 15.0
SSE_MAX_SECONDS = 300.0  # bounded stream; clients reconnect with the last cursor
_TERMINAL = {"completed", "failed", "canceled", "rejected"}
_INLINE_SAFE = {"text/plain", "text/markdown", "text/csv", "application/json", "application/pdf", "image/png"}

router = APIRouter(prefix="/api/v1")
# Wave 5b implements these; kept apart so the parent mounts them deliberately.
control_router = APIRouter(prefix="/api/v1")


@router.get("/projects/{project_id}/research-setup")
def research_setup(request: Request, project_id: UUID):
    with database_session() as db:
        view = profile_preparation.get_setup(db, _principal(request), project_id)
        return JSONResponse(view.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.post("/projects/{project_id}/preparations")
def prepare_environment(request: Request, project_id: UUID, body: PreparationSubmit):
    with database_session() as db:
        view = profile_preparation.request_preparation(db, _principal(request), project_id, body)
        db.commit()
        return JSONResponse(view.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.get("/projects/{project_id}/preparations/{job_id}")
def preparation_status(request: Request, project_id: UUID, job_id: UUID):
    with database_session() as db:
        view = profile_preparation.get_job(db, _principal(request), project_id, job_id)
        return JSONResponse(view.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.get("/runs/{run_id}/readiness")
def run_readiness(request: Request, run_id: UUID):
    with database_session() as db:
        principal = _principal(request)
        view = profile_preparation.get_readiness(db, principal, run_id)
        plan = domain.get_plan(db, principal, run_id)
        if plan.plan.scientific is not None:
            from scientist.scientific_authority import validate_plan_binding
            from scientist.contracts import ResearchRequirementView
            run = domain.get_run(db, principal, run_id)
            try:
                validate_plan_binding(db, principal, run.project_id, plan.plan.scientific, require_ready=False)
            except DomainError:
                view = view.model_copy(update={'state': 'blocked', 'requirements': [*view.requirements,
                    ResearchRequirementView(id='approved_research_context', label='Current research plan',
                        purpose='Rebuild the plan after its approved environment or instructions change.',
                        state='blocked', action='request_approval', reason='scientific_binding_unavailable')]})
        return JSONResponse(view.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(Body):
    name: str
    instructions: str = ""


class ProjectPatch(Body):
    expected_revision: int
    name: str | None = None
    instructions: str | None = None


class SessionCreate(Body):
    title: str


class FindingCreate(Body):
    session_id: UUID
    text: str
    artifact_id: UUID | None = None
    citation_ids: list[UUID] = Field(default_factory=list)


class RunCreate(Body):
    submission_key: str
    question: str
    input_ids: list[UUID] = Field(default_factory=list)
    provider_id: UUID
    model: str
    retry_of: UUID | None = None


class PlanPatch(Body):
    expected_revision: int
    plan: PlanSpec


class PreparePlan(Body):
    expected_revision: int
    search_terms: list[str] = Field(default_factory=list, max_length=10)
    workflow: Literal['literature', 'resources'] = 'literature'


class Publish(Body):
    artifact_ids: list[UUID]
    expected_project_revision: int
    publication_key: str


class ConnectionCreate(Body):
    provider_id: UUID
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    model: str = Field(min_length=1, max_length=200)
    secret: str = Field(min_length=1, max_length=16384)


def _principal(request: Request) -> Principal:
    return request.state.principal


def _error(request: Request, status: int, code: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": "The event cursor or page size is invalid.",
                                                     "request_id": getattr(request.state, "request_id", ""), **extra})


@router.get("/capabilities")
def capabilities():
    return {"file_types": sorted(files._TYPES), "max_upload_bytes": MAX_UPLOAD_BYTES,
            "protocols": {"mcp": "not_configured", "a2a": "not_configured"}}


@router.get("/connections")
def list_connections(request: Request):
    with database_session() as db:
        views = secret_store.list_connections(db, _principal(request))
    return JSONResponse([v.model_dump(mode="json") for v in views], headers={"Cache-Control": "no-store"})


@router.post("/connections", status_code=201)
def create_connection(request: Request, body: ConnectionCreate):
    with database_session() as db:
        view = secret_store.create_connection(db, _principal(request), body.provider_id, body.label, body.model, body.secret)
        db.commit()
    return JSONResponse(view.model_dump(mode="json"), status_code=201, headers={"Cache-Control": "no-store"})


@router.delete("/connections/{connection_id}", status_code=204)
def revoke_connection(request: Request, connection_id: UUID):
    with database_session() as db:
        secret_store.revoke_connection(db, _principal(request), connection_id)
        db.commit()
    return Response(status_code=204)


@router.get("/projects")
def list_projects(request: Request, after: UUID | None = None, limit: int = 100):
    with database_session() as db:
        return domain.list_projects(db, _principal(request), after, limit)


@router.post("/projects", status_code=201)
def create_project(request: Request, body: ProjectCreate):
    with database_session() as db:
        project = domain.create_project(db, _principal(request), body.name, body.instructions)
        db.commit()
        return project


@router.get("/projects/{project_id}")
def get_project(request: Request, project_id: UUID):
    with database_session() as db:
        return domain.get_project(db, _principal(request), project_id)


@router.patch("/projects/{project_id}")
def update_project(request: Request, project_id: UUID, body: ProjectPatch):
    with database_session() as db:
        project = domain.update_project(db, _principal(request), project_id, body.expected_revision, body.name, body.instructions)
        db.commit()
        return project


@router.get("/projects/{project_id}/sessions")
def list_sessions(request: Request, project_id: UUID):
    with database_session() as db:
        return domain.list_sessions(db, _principal(request), project_id)


@router.post("/projects/{project_id}/sessions", status_code=201)
def create_session(request: Request, project_id: UUID, body: SessionCreate):
    with database_session() as db:
        session = domain.create_session(db, _principal(request), project_id, body.title)
        db.commit()
        return session


@router.get("/sessions/{session_id}/messages")
def list_messages(request: Request, session_id: UUID, after_sequence: int = 0, limit: int = 200):
    with database_session() as db:
        return domain.list_messages(db, _principal(request), session_id, after_sequence, limit)


@router.get("/projects/{project_id}/files")
def list_files(request: Request, project_id: UUID):
    with database_session() as db:
        return domain.list_files(db, _principal(request), project_id)


@router.post("/projects/{project_id}/files", status_code=201)
async def upload_file(request: Request, project_id: UUID, filename: str = Query(min_length=1, max_length=255)):
    """Raw-body upload (no multipart dependency); the stream is bounded as it arrives."""
    principal = _principal(request)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise DomainError("request_too_large", 413)
    with SpooledTemporaryFile(max_size=2 * 1024 * 1024) as staged:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise DomainError("request_too_large", 413)
            staged.write(chunk)
        staged.seek(0)

        def attach() -> object:
            with database_session() as db:
                try:
                    file_id = files.attach(db, principal, project_id, filename, staged)
                except DomainError as exc:
                    if exc.code == "storage_unavailable":
                        db.commit()  # keep the failed attempt record
                    raise
                db.commit()
                return next(f for f in domain.list_files(db, principal, project_id) if f.id == file_id)

        return await run_in_threadpool(attach)


@router.get("/projects/{project_id}/files/{file_id}/content")
def file_content(request: Request, project_id: UUID, file_id: UUID):
    with database_session() as db:
        row = domain.get_file(db, _principal(request), project_id, file_id)
        if row.state != "ready":
            raise DomainError("not_found", 404)
        ref = ObjectRef(project_id=row.project_id, key=row.object_key, sha256=row.sha256.strip(), size=row.size, content_type="application/octet-stream")
        return _download(ref, row.content_type, row.filename)


@router.delete("/projects/{project_id}/files/{file_id}", status_code=204)
def delete_file(request: Request, project_id: UUID, file_id: UUID):
    with database_session() as db:
        principal = _principal(request)
        domain.get_file(db, principal, project_id, file_id)  # 404 unless it belongs to this project
        files.tombstone(db, principal, file_id)
        db.commit()
    return Response(status_code=204)


@router.get("/projects/{project_id}/findings")
def list_findings(request: Request, project_id: UUID):
    with database_session() as db:
        return domain.list_findings(db, _principal(request), project_id)


@router.post("/projects/{project_id}/findings", status_code=201)
def save_finding(request: Request, project_id: UUID, body: FindingCreate):
    with database_session() as db:
        finding = domain.save_finding(db, _principal(request), project_id, body.session_id, body.text, body.artifact_id, body.citation_ids)
        db.commit()
        return finding


@router.delete("/projects/{project_id}/findings/{finding_id}", status_code=204)
def remove_finding(request: Request, project_id: UUID, finding_id: UUID):
    with database_session() as db:
        domain.remove_finding(db, _principal(request), project_id, finding_id)
        db.commit()
    return Response(status_code=204)


@router.get("/projects/{project_id}/sources")
def list_sources(request: Request, project_id: UUID):
    with database_session() as db:
        return domain.list_citations(db, _principal(request), project_id)


@router.get("/citations/{citation_id}")
def get_citation(request: Request, citation_id: UUID):
    with database_session() as db:
        return domain.get_citation(db, _principal(request), citation_id)


@router.post("/sessions/{session_id}/runs", status_code=201)
def create_run(request: Request, session_id: UUID, body: RunCreate):
    with database_session() as db:
        project_id = domain.session_project(db, _principal(request), session_id, "work:submit")
        run = domain.submit_run(db, _principal(request), project_id, session_id, body.submission_key, body.question,
                                body.input_ids, body.provider_id, body.model, body.retry_of)
        db.commit()
        return run


@router.get("/projects/{project_id}/runs")
def list_runs(request: Request, project_id: UUID):
    with database_session() as db:
        return domain.list_runs(db, _principal(request), project_id)


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: UUID):
    with database_session() as db:
        return domain.get_run(db, _principal(request), run_id)


@router.get("/runs/{run_id}/pending-decisions")
def get_pending_decisions(request: Request, run_id: UUID):
    with database_session() as db:
        decisions = domain.get_pending_decisions(db, _principal(request), run_id)
        return JSONResponse(
            [decision.model_dump(mode="json") for decision in decisions],
            headers={"Cache-Control": "no-store"},
        )


@router.get("/runs/{run_id}/plan")
def get_plan(request: Request, run_id: UUID):
    with database_session() as db:
        return domain.get_plan(db, _principal(request), run_id)


@router.patch("/runs/{run_id}/plan")
def patch_plan(request: Request, run_id: UUID, body: PlanPatch):
    with database_session() as db:
        run = domain.revise_plan(db, _principal(request), run_id, body.expected_revision, body.plan)
        db.commit()
        return run


@router.post("/runs/{run_id}/prepare-plan")
def prepare_plan(request: Request, run_id: UUID, body: PreparePlan):
    with database_session() as db:
        principal = _principal(request)
        plan = research.build_plan(db, principal, run_id, body.search_terms, workflow=body.workflow)
        run = domain.revise_plan(db, principal, run_id, body.expected_revision, plan)
        db.commit()
        return run


@router.get("/runs/{run_id}/event-page")
def event_page(request: Request, run_id: UUID, after: int = 0, limit: int = 100):
    with database_session() as db:
        principal = _principal(request)
        if after < 0 or not 1 <= limit <= 200:
            raise DomainError("cursor_expired", 400)
        snapshot = domain.get_run(db, principal, run_id)
        if after > snapshot.latest_cursor:
            return _resync(request, snapshot)
        return {"events": domain.get_events(db, principal, run_id, after, limit), "latest_cursor": snapshot.latest_cursor}


def _resync(request: Request, snapshot) -> JSONResponse:
    return _error(request, 410, "cursor_expired", snapshot=snapshot.model_dump(mode="json"))


@router.get("/runs/{run_id}/events")
def event_stream(request: Request, run_id: UUID, after: int | None = None, last_event_id: str | None = Header(default=None)):
    principal = _principal(request)
    header_cursor = int(last_event_id) if last_event_id and re.fullmatch(r"[0-9]{1,18}", last_event_id) else 0
    after = max(after or 0, header_cursor)
    if after < 0:
        raise DomainError("cursor_expired", 400)
    with database_session() as db:
        snapshot = domain.get_run(db, principal, run_id)
    if after > snapshot.latest_cursor:
        return _resync(request, snapshot)
    return StreamingResponse(_replay(principal, request.cookies.get("owner_session", ""), run_id, after), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def _poll(principal: Principal, cookie: str, run_id: UUID, after: int):
    with database_session() as db:
        if principal.kind == "owner":
            authenticate_owner_session(db, cookie)  # a revoked or expired session ends the stream
        batch = domain.get_events(db, principal, run_id, after, 100)
        terminal = not batch and domain.get_run(db, principal, run_id).state in _TERMINAL
    return batch, terminal


async def _replay(principal: Principal, cookie: str, run_id: UUID, after: int):
    """Replay persisted events, then follow; re-authenticates every poll and sends non-content heartbeats."""
    started = last_sent = time.monotonic()
    while time.monotonic() - started < SSE_MAX_SECONDS:
        try:
            batch, terminal = await run_in_threadpool(_poll, principal, cookie, run_id, after)
        except Exception:
            return  # access revoked, run gone or storage down; the client resynchronizes via REST
        for event in batch:
            after = event["sequence"]
            yield f"id: {after}\nevent: {event['kind']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
            last_sent = time.monotonic()
        if terminal:
            return
        if not batch:
            if time.monotonic() - last_sent >= SSE_HEARTBEAT_SECONDS:
                yield ": heartbeat\n\n"
                last_sent = time.monotonic()
            await asyncio.sleep(SSE_POLL_SECONDS)


@router.get("/runs/{run_id}/artifacts")
def list_artifacts(request: Request, run_id: UUID):
    with database_session() as db:
        return domain.list_artifacts(db, _principal(request), run_id)


@router.get("/artifacts/{artifact_id}/content")
def artifact_content(request: Request, artifact_id: UUID):
    with database_session() as db:
        row = domain.get_artifact(db, _principal(request), artifact_id)
        ref = ObjectRef(project_id=row.project_id, key=row.object_key, sha256=row.sha256.strip(), size=row.size, content_type="application/octet-stream")
        return _download(ref, row.content_type, f"artifact-{artifact_id}")


@router.post("/runs/{run_id}/publish")
def publish(request: Request, run_id: UUID, body: Publish):
    with database_session() as db:
        principal = _principal(request)
        if principal.kind != "owner":
            raise DomainError("forbidden", 403)
        if len(body.artifact_ids) > 1000 or len(set(body.artifact_ids)) != len(body.artifact_ids):
            raise DomainError("idempotency_conflict", 409)
        # Server-side mapping: only artifacts of this run, never client-supplied object keys.
        keys = []
        for artifact_id in body.artifact_ids:
            key = db.execute(text("SELECT object_key FROM artifacts WHERE id = :a AND run_id = :r"), {"a": artifact_id, "r": run_id}).scalar_one_or_none()
            if key is None:
                raise DomainError("not_found", 404)
            keys.append(key)
        ids = files.publish(db, principal, run_id, keys, body.expected_project_revision, body.publication_key)
        db.commit()
        return {"file_ids": [str(i) for i in ids]}


def _download(ref: ObjectRef, content_type: str, name: str) -> Response:
    """Verified bytes, always as an attachment; unknown types are never sniffed or rendered."""
    try:
        with objects.open_verified(ref) as stream:
            body = stream.read()
    except objects.StorageIntegrityError as exc:
        raise DomainError("storage_unavailable", 503) from exc
    safe_name = "".join(c if c.isascii() and (c.isalnum() or c in "._-") else "_" for c in name)[:100] or "download"
    return Response(body, media_type=content_type if content_type in _INLINE_SAFE else "application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename=\"{safe_name}\"; filename*=UTF-8''{quote(name[:100], safe='')}", "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


class ApproveBody(Body):
    expected_revision: int
    plan_digest: str


STOP_GRACE_SECONDS = 10


def _require_runtime() -> None:
    # Stop and budget resume need the trusted supervisor; fail closed until the host configures it.
    if supervisor._config is None:
        raise DomainError("runtime_unavailable", 503)


@control_router.post("/runs/{run_id}/approve")
def approve_run(request: Request, run_id: UUID, body: ApproveBody):
    with database_session() as db:
        run = domain.approve_run(db, _principal(request), run_id, body.expected_revision, body.plan_digest)
        db.commit()
        return run


@control_router.post("/runs/{run_id}/stop")
def stop_run(request: Request, run_id: UUID):
    _require_runtime()
    with database_session() as db:
        domain.authorize_stop(db, _principal(request), run_id)
        db.rollback()  # release the authorization lock; supervisor.stop takes its own fenced lock
        return supervisor.stop(db, run_id, STOP_GRACE_SECONDS)


@control_router.post("/runs/{run_id}/decisions")
def decide_run(request: Request, run_id: UUID, body: DecisionSubmit):
    if body.choice in ("stop", "extend", "retry"):
        _require_runtime()  # check before committing a decision the host could not act on
    with database_session() as db:
        try:
            run = domain.submit_decision(db, _principal(request), run_id, body)
        except DomainError:
            db.rollback()
            raise
        db.commit()
        if body.choice == "stop":
            return supervisor.stop(db, run_id, STOP_GRACE_SECONDS)  # fenced finish; idempotent on replay
        if body.choice == "extend" and run.state == "waiting_input" and run.waiting_reason == "budget_exhausted":
            # Existing recover path (executor reaping, checkpoint check, budget re-check); it only queues.
            return supervisor.recover(db, run_id, budget_resume=True)
        return run
