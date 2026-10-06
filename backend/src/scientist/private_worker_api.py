"""Capability-scoped worker control/result APIs, composed only on private listeners."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from scientist import broker, broker_api, limits, objects
from scientist.auth import DomainError
from scientist.contracts import ArtifactView, CheckpointManifest, ObjectRef, ScientificBinding
from scientist.db import session
from scientist.runtime_contracts import (
    BoundaryAck, BoundaryRequest, MAX_BOUNDARY_BYTES, RUNTIME_COMMIT,
    RuntimeContextV1, WorkspaceEntry, canonical_bytes, operation_fingerprint,
)

_MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_EFFECT_BYTES = 2 * 1024 * 1024
Capture = Callable[[Session, UUID, int, bytes, Path], CheckpointManifest]
ResultReader = Callable[[ObjectRef], bytes]


class RuntimePins(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    runtime_commit: str = RUNTIME_COMMIT
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    skills_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse_boundary(data: bytes, *, maximum_bytes: int = MAX_BOUNDARY_BYTES) -> BoundaryRequest:
    if len(data) > maximum_bytes:
        raise DomainError("request_too_large", 413)
    try:
        def reject_constant(value):
            raise ValueError("nonfinite JSON constant")
        value = json.loads(data, object_pairs_hook=_reject_duplicate_keys, parse_constant=reject_constant)
        return BoundaryRequest.model_validate(value)
    except (ValueError, UnicodeError, RecursionError, ValidationError) as exc:
        raise DomainError("invalid_boundary", 422) from exc


def _capture(db, run, generation, context, workspace):
    from scientist.checkpoints import capture
    return capture(db, run, generation, context, workspace)


def _read_result(ref: ObjectRef) -> bytes:
    with objects.open_verified(ref) as source:
        return source.read(_MAX_RESULT_BYTES + 1)


class WorkerController:
    def __init__(self, *, pins: RuntimePins, provider_destinations: Mapping[UUID, str],
                 capture: Capture = _capture, result_reader: ResultReader = _read_result,
                 scientific_validator: Callable[[Session, UUID, ScientificBinding], None] | None = None):
        if pins.runtime_commit != RUNTIME_COMMIT:
            raise ValueError("unreviewed runtime commit")
        self.pins = pins
        self.provider_destinations = MappingProxyType(dict(provider_destinations))
        self.capture = capture
        self.result_reader = result_reader
        self.scientific_validator = scientific_validator

    def _run(self, db: Session, capability: str, *, lock: bool):
        claims = broker._verify_capability(capability)
        try:
            run_id = UUID(claims["run_id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise DomainError("forbidden", 403) from exc
        row = broker._load_run(db, run_id, lock=lock)
        broker._check_capability_claims(row, claims)
        return row

    def _context_authority(self, db: Session, row, context: RuntimeContextV1) -> None:
        plan = broker._load_plan(db, row.id, row.revision)
        if (context.run_id != row.id or context.project_id != row.project_id
                or context.generation != row.generation or context.revision != row.revision
                or context.plan_digest != row.plan_digest.strip()
                or context.plan != plan
                or context.provider_endpoint != self.provider_destinations.get(plan.provider_id)
                or any(getattr(context, field) != getattr(self.pins, field)
                       for field in ("runtime_commit", "image_digest", "skills_digest", "environment_digest"))):
            raise DomainError("forbidden", 403)
        snapshot = db.execute(text("SELECT digest FROM input_snapshots WHERE run_id=:run"), {"run":row.id}).scalar_one()
        if context.input_snapshot_digest != snapshot.strip():
            raise DomainError("forbidden", 403)
        if context.plan.scientific is not None:
            if self.scientific_validator is None:
                raise DomainError("scientific_environment_unavailable", 409)
            self.scientific_validator(db, row.id, context.plan.scientific)
        for mapping in context.operation_mappings:
            request = mapping.request
            broker._validate_scope(db, row.project_id, plan, request)
            operation = db.execute(text("SELECT * FROM operations WHERE run_id=:run AND operation_id=:op"),
                                   {"run":row.id, "op":mapping.operation_id}).one_or_none()
            if operation is None:
                if request.generation != row.generation:
                    raise DomainError("forbidden", 403)
            elif (operation.generation != request.generation or operation.kind != request.kind
                  or operation.reserve_tokens != request.reserve_tokens
                  or operation.payload_hash.strip() != operation_fingerprint(request)):
                raise DomainError("forbidden", 403)

    def bootstrap_context(self, db: Session, verified_context: bytes, run_id: UUID,
                          generation: int) -> RuntimeContextV1:
        """Trusted supervisor-only generation rebinding after verified restore/reaping.

        The caller must have verified immutable checkpoint bytes and executor death.
        No HTTP route accepts this input or exposes this method to a worker.
        """
        saved = RuntimeContextV1.model_validate_json(verified_context)
        row = broker._load_run(db, run_id, lock=True)
        if (type(generation) is not int or generation != row.generation
                or saved.run_id != run_id or saved.generation > generation
                or saved.revision > row.revision):
            raise DomainError("forbidden", 403)
        data = saved.model_dump(mode="json")
        # ADR-012: the snapshot comes from the ledger, never from checkpoint bytes.
        data.update(generation=generation, revision=row.revision,
                    budget_remaining_tokens=limits.remaining_tokens(row))
        for mapping in data["operation_mappings"]:
            operation = db.execute(text("SELECT generation FROM operations WHERE run_id=:run AND operation_id=:op"),
                                   {"run":run_id,"op":mapping["operation_id"]}).one_or_none()
            if operation is None:
                # Never sent: the newly fenced executor will create the journal row.
                mapping["request"]["generation"] = generation
            elif operation.generation != mapping["request"]["generation"]:
                raise DomainError("forbidden", 403)
        effective = RuntimeContextV1.model_validate(data)
        self._context_authority(db, row, effective)
        return effective

    def boundary(self, db: Session, capability: str, request: BoundaryRequest) -> BoundaryAck:
        # Revalidate even programmatically supplied model_copy values at the trust boundary.
        request = BoundaryRequest.model_validate_json(request.model_dump_json())
        row = self._run(db, capability, lock=True)
        self._context_authority(db, row, request.context)
        digest = hashlib.sha256(canonical_bytes(request.model_dump(mode="json"))).hexdigest()
        previous = db.execute(text("SELECT * FROM checkpoint_boundaries WHERE run_id=:run AND boundary_id=:boundary"),
                              {"run":row.id, "boundary":request.boundary_id}).one_or_none()
        if previous is not None:
            if previous.generation != row.generation or previous.payload_hash.strip() != digest:
                raise DomainError("revision_conflict", 409)
            return BoundaryAck.model_validate(previous.ack)
        revision = db.execute(text("SELECT COALESCE(MAX(revision),0) FROM checkpoints WHERE run_id=:run"),
                              {"run":row.id}).scalar_one()
        if request.expected_checkpoint_revision != revision:
            raise DomainError("revision_conflict", 409)
        files = sorted(request.workspace, key=lambda entry: entry.path)
        files_by_path = {entry.path: entry for entry in files}
        for receipt in request.context.scientific_results:
            entry = files_by_path.get(receipt.path)
            if entry is None or entry.sha256 != receipt.sha256 or entry.size != receipt.size:
                raise DomainError("invalid_scientific_result", 422)
            from scientist.resource_recipe import validate_resource_result
            binding = request.context.plan.scientific
            try:
                validate_resource_result(entry.decoded_data(), profile_id=binding.profile_id,
                                         instruction_fingerprint=binding.instruction_fingerprint,
                                         max_bytes=binding.max_result_bytes)
            except (ValueError, TypeError, AttributeError) as exc:
                raise DomainError("invalid_scientific_result", 422) from exc
        authoritative_context = RuntimeContextV1.model_validate({
            **request.context.model_dump(mode="json"),
            "workspace_manifest":[WorkspaceEntry(path=entry.path, sha256=entry.sha256, size=entry.size).model_dump()
                                  for entry in files],
            "budget_remaining_tokens": limits.remaining_tokens(row),
        })
        context_bytes = authoritative_context.model_dump_json().encode()
        with tempfile.TemporaryDirectory(prefix="scientist-boundary-") as staging:
            workspace = Path(staging)
            for entry in files:
                path = workspace / entry.path
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with path.open("xb") as output:
                    output.write(entry.decoded_data())
                path.chmod(0o600)
            manifest = self.capture(db, row.id, row.generation, context_bytes, workspace)
        if (manifest.run_id != row.id or manifest.revision != revision + 1
                or manifest.plan_digest != authoritative_context.plan_digest
                or manifest.context.project_id != row.project_id
                or manifest.context.sha256 != hashlib.sha256(context_bytes).hexdigest()
                or manifest.context.size != len(context_bytes)
                or manifest.context.content_type != "application/octet-stream"
                or any(getattr(manifest, field) != getattr(self.pins, field)
                       for field in ("runtime_commit", "image_digest", "skills_digest", "environment_digest"))
                or manifest.operation_ids != [item.operation_id for item in authoritative_context.operation_mappings]
                or len(manifest.workspace) != len(files)
                or any(ref.project_id != row.project_id or ref.sha256 != entry.sha256 or ref.size != entry.size
                       or ref.content_type != "application/octet-stream"
                       for ref, entry in zip(manifest.workspace, files))):
            raise DomainError("storage_unavailable", 503)
        checkpoint = db.execute(text("SELECT id,manifest FROM checkpoints WHERE run_id=:run AND revision=:revision"),
                                {"run":row.id,"revision":manifest.revision}).one_or_none()
        if checkpoint is None or CheckpointManifest.model_validate(checkpoint.manifest) != manifest:
            raise DomainError("storage_unavailable", 503)
        self._register_scientific_results(db, row, authoritative_context, files, manifest, checkpoint.id)
        ack = BoundaryAck(schema_version=1,boundary_id=request.boundary_id,checkpoint_id=checkpoint.id,
                          checkpoint_revision=manifest.revision,manifest=manifest)
        db.execute(text("INSERT INTO checkpoint_boundaries(id,run_id,boundary_id,checkpoint_revision,generation,"
                        "expected_checkpoint_revision,payload_hash,checkpoint_id,ack) VALUES "
                        "(:id,:run,:boundary,:revision,:generation,:expected,:digest,:checkpoint,CAST(:ack AS jsonb))"),
                   {"id":uuid4(),"run":row.id,"boundary":request.boundary_id,"revision":manifest.revision,
                    "generation":row.generation,"expected":revision,"digest":digest,"checkpoint":checkpoint.id,
                    "ack":ack.model_dump_json()})
        return ack

    def _register_scientific_results(self, db, row, context, files, manifest, checkpoint_id):
        """Reuse captured immutable objects; receipt and event commit with the ACK."""
        from scientist.domain import _event
        references = {entry.path: ref for entry, ref in zip(files, manifest.workspace, strict=True)}
        for receipt in context.scientific_results:
            digest = hashlib.sha256(canonical_bytes(receipt.model_dump(mode="json"))).hexdigest()
            prior = db.execute(text("SELECT artifact_id,receipt_sha256 FROM scientific_artifact_receipts "
                                    "WHERE run_id=:run AND tool_call_id=:call"),
                               {"run": row.id, "call": receipt.tool_call_id}).one_or_none()
            if prior is not None:
                if prior.receipt_sha256.strip() != digest:
                    raise DomainError("revision_conflict", 409)
                if context.boundary == "final":
                    changed = db.execute(text("UPDATE artifacts SET partial=false WHERE id=:id "
                                              "AND project_id=:project AND run_id=:run AND partial=true"),
                                         {"id": prior.artifact_id, "project": row.project_id, "run": row.id}).rowcount
                    if changed:
                        view = ArtifactView(artifact_id=prior.artifact_id, project_id=row.project_id,
                                            run_id=row.id, title="Resource measurements", kind="file",
                                            sha256=receipt.sha256, size=receipt.size,
                                            content_type="application/json", partial=False)
                        _event(db, row.id, row.revision, "artifact.ready", {"artifact": view.model_dump(mode="json")})
                continue
            ref = references[receipt.path]
            artifact_id = uuid4()
            partial = context.boundary != "final"
            db.execute(text("INSERT INTO artifacts(id,project_id,run_id,title,kind,object_key,sha256,size,content_type,partial) "
                            "VALUES(:id,:project,:run,'Resource measurements','file',:key,:sha,:size,'application/json',:partial)"),
                       {"id": artifact_id, "project": row.project_id, "run": row.id, "key": ref.key,
                        "sha": receipt.sha256, "size": receipt.size, "partial": partial})
            db.execute(text("INSERT INTO scientific_artifact_receipts(run_id,tool_call_id,project_id,checkpoint_id,"
                            "artifact_id,receipt_sha256) VALUES(:run,:call,:project,:checkpoint,:artifact,:sha)"),
                       {"run": row.id, "call": receipt.tool_call_id, "project": row.project_id,
                        "checkpoint": checkpoint_id, "artifact": artifact_id, "sha": digest})
            view = ArtifactView(artifact_id=artifact_id, project_id=row.project_id, run_id=row.id,
                                title="Resource measurements", kind="file", sha256=receipt.sha256,
                                size=receipt.size, content_type="application/json", partial=partial)
            _event(db, row.id, row.revision, "artifact.ready", {"artifact": view.model_dump(mode="json")})

    def result(self, db: Session, capability: str, operation_id: str) -> bytes:
        row = self._run(db, capability, lock=False)
        if not operation_id or len(operation_id) > 200:
            raise DomainError("forbidden", 403)
        operation, _ = broker.effective_operation(db, row.id, operation_id)
        if operation is None or operation.state != "committed":
            raise DomainError("result_unavailable", 409)
        ref = broker._operation_result(operation).result
        if ref is None or ref.project_id != row.project_id or ref.size > _MAX_RESULT_BYTES:
            raise DomainError("storage_unavailable", 503)
        try:
            data = self.result_reader(ref)
        except Exception as exc:
            raise DomainError("storage_unavailable", 503) from exc
        if len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise DomainError("storage_unavailable", 503)
        return data


class _EffectBodyLimit:
    """Bound actual incoming effect bytes before FastAPI's JSON allocation."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http' or scope['method'] != 'POST' or scope['path'] != '/effects':
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            if len(body) + len(chunk) > MAX_EFFECT_BYTES:
                await JSONResponse(status_code=413, content={'detail':{'code':'request_too_large'}})(
                    scope, receive, send)
                return
            body.extend(chunk)
            if not message.get('more_body', False):
                break
        forwarded = False

        async def bounded_receive():
            nonlocal forwarded
            if not forwarded:
                forwarded = True
                return {'type':'http.request', 'body':bytes(body), 'more_body':False}
            return await receive()

        await self.app(scope, bounded_receive, send)


def create_private_app(controller: WorkerController) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(_EffectBodyLimit)
    app.include_router(broker_api.router)

    @app.post("/control/boundary", response_model=BoundaryAck)
    async def boundary(request: Request, x_worker_capability: str = Header(min_length=1,max_length=4096)):
        try:
            # Count actual streamed bytes before JSON/base64 parsing.
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_BOUNDARY_BYTES:
                    raise DomainError("request_too_large", 413)
                body.extend(chunk)
            parsed = parse_boundary(bytes(body))
            with session() as db:
                ack = controller.boundary(db, x_worker_capability, parsed)
                db.commit()
                return ack
        except DomainError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code) from exc
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="invalid_boundary") from exc

    @app.get("/effects/{operation_id}/result")
    def result(operation_id: str, x_worker_capability: str = Header(min_length=1,max_length=4096)):
        try:
            with session() as db:
                content = controller.result(db, x_worker_capability, operation_id)
            return Response(content=content, media_type="application/octet-stream",
                            headers={"Cache-Control":"no-store", "X-Content-Type-Options":"nosniff"})
        except DomainError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code) from exc

    return app
