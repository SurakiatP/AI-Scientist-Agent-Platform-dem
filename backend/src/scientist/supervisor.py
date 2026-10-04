"""Durable run supervisor and owned-Colima worker isolation controls.

The only concrete Docker target accepted here is the project-owned test profile.
Workers remain blocked in their immutable entrypoint until namespace policy has
been installed and read back by the trusted host supervisor.
"""
from __future__ import annotations

import hashlib
import ipaddress
import io
import json
import re
import subprocess
import tarfile
import base64
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Protocol, Sequence
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from scientist import checkpoints
from scientist.auth import DomainError
from scientist.contracts import CheckpointManifest, RunView
from scientist.domain import _event, _run_view
from scientist import limits
from scientist.dispatch_authority import dispatch_is_inactive
from scientist.runtime_contracts import (
    BootstrapMetadata,
    RUNTIME_COMMIT,
    RuntimeContextV1,
    WorkspaceFile as BoundaryWorkspaceFile,
)


if TYPE_CHECKING:
    from scientist.private_worker_api import WorkerController


_DOCKER_CONTEXT = "colima-scientist-platform-test"
_COLIMA_PROFILE = "scientist-platform-test"
_MAX_ACTIVE = 3
_LEASE_SECONDS = 300
_RUN_LABEL = "scientist.platform/run"
_GEN_LABEL = "scientist.platform/generation"
_EXEC_LABEL = "scientist.platform/executor"
_KIND_LABEL = "scientist.platform/kind"


class DispatchPreLaunchRejected(RuntimeError):
    """Affirmative immutable-validation rejection raised before any launch intent."""


@dataclass(frozen=True)
class ExecutorRef:
    """Immutable physical identity used when proving an executor inactive."""

    executor_id: UUID
    run_id: UUID
    generation: int
    kind: str
    operation_id: str | None
    process_incarnation: UUID
    engine_id: str
    container_id: str


@dataclass(frozen=True)
class WorkerBootstrap:
    context: bytes
    workspace: Sequence[BoundaryWorkspaceFile]
    metadata: BootstrapMetadata


def continuation_bootstrap(db: Session, run_id: UUID, generation: int, controller: WorkerController) -> WorkerBootstrap:
    """Fail-closed bootstrap for a claimed generation > first: latest verified checkpoint, rebound.

    Never falls back to a fresh context. Raises on no checkpoint, integrity failure
    or a generation/revision the controller does not accept. Never touches usage or operations.
    A fresh context is lawful only when the run has no checkpoint AND no operation rows;
    callers must choose this function in every other case.
    """
    row = db.execute(text("SELECT manifest FROM checkpoints WHERE run_id=:run ORDER BY revision DESC LIMIT 1"),
                     {"run": run_id}).mappings().one_or_none()
    if row is None:
        raise RuntimeError("no checkpoint to continue from")
    manifest = CheckpointManifest.model_validate(row["manifest"])
    with tempfile.TemporaryDirectory(prefix="scientist-continuation-") as directory:
        raw = checkpoints.restore(db, manifest, Path(directory))
        saved = RuntimeContextV1.model_validate_json(raw)
        files = []
        for entry in saved.workspace_manifest:
            data = (Path(directory) / entry.path).read_bytes()
            files.append(BoundaryWorkspaceFile(
                path=entry.path, sha256=entry.sha256, size=entry.size,
                data_base64=base64.b64encode(data).decode("ascii")))
    context = controller.bootstrap_context(db, raw, run_id, generation)
    return WorkerBootstrap(
        context=context.model_dump_json().encode(), workspace=files,
        metadata=BootstrapMetadata(schema_version=1, checkpoint_revision=manifest.revision))


class DispatchRuntime(Protocol):
    def start(
        self, db: Session, run_id: UUID, generation: int, network: str,
        broker_ip: str, executor_id: UUID, process_incarnation: UUID,
        *, before_mutation: Callable[[str, str | None], None],
    ) -> ExecutorRef: ...

    def find(
        self, db: Session, run_id: UUID, generation: int, executor_id: UUID,
        operation_id: str | None, process_incarnation: UUID,
    ) -> ExecutorRef | None: ...

    def inactive(self, db: Session, executor: ExecutorRef, operation_id: str) -> bool: ...

    def stop(self, db: Session, executor: ExecutorRef, grace_seconds: int) -> bool: ...


def _dispatch_operation_is_inactive(
    db: Session, dispatch: DispatchRuntime, executor: ExecutorRef, operation_id: str
) -> bool:
    def probe(incarnation: UUID, engine_id: str, container_id: str) -> bool:
        if (incarnation, engine_id, container_id) != (
            executor.process_incarnation,
            executor.engine_id,
            executor.container_id,
        ):
            return False
        return dispatch.inactive(db, executor, operation_id)

    return dispatch_is_inactive(
        db, executor.run_id, operation_id, executor.generation, probe=probe
    )


def _record_dispatch_inactive(
    db: Session,
    dispatch: DispatchRuntime,
    executor_id: UUID,
    executor: ExecutorRef,
    operation_ids: list[str],
    proof: dict[str, object],
) -> bool:
    changed = db.execute(
        text("""UPDATE runtime_executors
               SET state='inactive', proof=CAST(:proof AS jsonb), updated_at=now()
               WHERE id=:id AND run_id=:run AND generation=:generation
                 AND kind='dispatch' AND container_id=:container AND engine_id=:engine
                 AND process_incarnation=:incarnation"""),
        {
            "proof": json.dumps(proof, sort_keys=True),
            "id": executor_id,
            "run": executor.run_id,
            "generation": executor.generation,
            "container": executor.container_id,
            "engine": executor.engine_id,
            "incarnation": executor.process_incarnation,
        },
    ).rowcount
    if changed != 1:
        return False
    for operation_id in operation_ids:
        if not _dispatch_operation_is_inactive(db, dispatch, executor, operation_id):
            db.execute(
                text("UPDATE runtime_executors SET state='unknown', updated_at=now() WHERE id=:id"),
                {"id": executor_id},
            )
            return False
    return True


def _inactive_executor_proven(executor) -> bool:
    proof = executor["proof"] or {}
    if proof.get("source") == f"{executor['kind']}-launch-not-attempted":
        return executor["container_id"] is None
    return (
        proof.get("source") in {"owned-engine-exact-container", "owned-engine-generation-fence"}
        and proof.get("engine_id") == executor["engine_id"]
        and proof.get("container_id") == executor["container_id"]
        and bool(executor["container_id"])
        and bool(executor["engine_id"])
    )


@dataclass
class RuntimeConfig:
    image: str
    image_digest: str
    broker_url: str
    broker_ip: str
    broker_port: int
    runtime_commit: str
    skills_digest: str
    environment_digest: str
    bootstrap_factory: Callable[[Session, UUID, int], WorkerBootstrap]
    capability_factory: Callable[[Session, UUID, int], str]
    dispatch: DispatchRuntime
    engine: "DockerWorkerEngine"


_config: RuntimeConfig | None = None


def configure(
    *,
    image: str,
    image_digest: str,
    broker_url: str,
    broker_ip: str,
    broker_port: int,
    runtime_commit: str,
    skills_digest: str,
    environment_digest: str,
    bootstrap_factory: Callable[[Session, UUID, int], WorkerBootstrap],
    capability_factory: Callable[[Session, UUID, int], str],
    dispatch: DispatchRuntime,
    engine: "DockerWorkerEngine | None" = None,
) -> None:
    """Install trusted runtime inputs; worker requests cannot set these values."""
    global _config
    raw_image = re.fullmatch(r"sha256:[a-f0-9]{64}", image)
    named_image = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:+-]*@(sha256:[a-f0-9]{64})", image)
    if (
        not image
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_digest)
        or not ((raw_image and image == image_digest)
                or (named_image and named_image.group(1) == image_digest))
        or runtime_commit != RUNTIME_COMMIT
        or not re.fullmatch(r"[a-f0-9]{64}", skills_digest)
        or not re.fullmatch(r"[a-f0-9]{64}", environment_digest)
        or not 1 <= broker_port <= 65535
    ):
        raise ValueError("runtime image and trusted pins must be immutable")
    try:
        if ipaddress.ip_address(broker_ip).version != 4:
            raise ValueError("internal broker bridge endpoint must be IPv4")
    except ValueError as exc:
        raise ValueError("internal broker bridge endpoint must be IPv4") from exc
    _config = RuntimeConfig(
        image=image, image_digest=image_digest, broker_url=broker_url,
        broker_ip=broker_ip, broker_port=broker_port, runtime_commit=runtime_commit,
        skills_digest=skills_digest, environment_digest=environment_digest,
        bootstrap_factory=bootstrap_factory, capability_factory=capability_factory,
        dispatch=dispatch, engine=engine or DockerWorkerEngine(),
    )
    checkpoints.configure_trusted_pins(
        image_digest=image_digest, skills_digest=skills_digest,
        environment_digest=environment_digest, runtime_commit=runtime_commit,
    )


def claim(db: Session, max_active: int) -> tuple[UUID, int] | None:
    """Claim one approved queue entry using PostgreSQL row locks."""
    if type(max_active) is not int or not 1 <= max_active <= _MAX_ACTIVE:
        raise ValueError("max_active must be between one and three")
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('scientist.supervisor.claim'))"))
    active = db.execute(
        text("SELECT count(*) FROM runs WHERE state IN ('running','recovering','stopping')")
    ).scalar_one()
    if active >= max_active:
        db.rollback()
        return None
    row = db.execute(text("""
        SELECT r.*
        FROM runs AS r
        WHERE r.state = 'queued' AND r.cancel_requested = false
          AND r.plan_digest IS NOT NULL
          AND EXISTS (SELECT 1 FROM approvals a WHERE a.run_id = r.id
                      AND a.revision = r.revision AND a.plan_digest = r.plan_digest)
        ORDER BY (SELECT MIN(s.created_at) FROM input_snapshots AS s WHERE s.run_id = r.id), r.id
        LIMIT 1 FOR UPDATE OF r SKIP LOCKED
    """)).mappings().one_or_none()
    if row is None:
        db.rollback()
        return None
    if limits.budget_exhausted(db, row):
        limits.mark_budget_wait(db, row["id"], row["revision"], _event)
        db.commit()
        return None
    generation = row["generation"] + 1
    changed = db.execute(text("""
        UPDATE runs SET state = 'running', generation = :generation,
            lease_expires_at = now() + (:lease * interval '1 second'),
            elapsed_active_since = COALESCE(elapsed_active_since, clock_timestamp()),
            waiting_reason = NULL, error_code = NULL
        WHERE id = :run AND state = 'queued' AND generation = :old_generation
        RETURNING id
    """), {"run": row["id"], "generation": generation,
            "old_generation": row["generation"], "lease": _LEASE_SECONDS}).scalar_one_or_none()
    if changed is None:
        db.rollback()
        return None
    _event(db, row["id"], row["revision"], "run.state", {"state": "running"})
    db.commit()
    return row["id"], generation


def start(db: Session, run_id: UUID, generation: int) -> str:
    """Start a worker only after its immutable executor identities are durable."""
    cfg = _require_config()
    run = _active_run(db, run_id, generation)
    worker_id, worker_incarnation = uuid4(), uuid4()
    dispatch_id, dispatch_incarnation = uuid4(), uuid4()
    db.execute(text("""
        INSERT INTO runtime_executors
            (id, run_id, generation, kind, operation_id, process_incarnation, state)
        VALUES (:id, :run, :generation, 'worker', NULL, :incarnation, 'starting'),
               (:dispatch_id, :run, :generation, 'dispatch', NULL, :dispatch_incarnation, 'starting')
    """), {"id": worker_id, "run": run_id, "generation": generation,
            "incarnation": worker_incarnation, "dispatch_id": dispatch_id,
            "dispatch_incarnation": dispatch_incarnation})
    db.commit()
    engine_id = cfg.engine.engine_id()
    _bind_engine(db, worker_id, engine_id)
    dispatch_attempted = False
    dispatch_intent = False
    dispatch_rejected = False

    def _identity_proof(source: str, **extra) -> str:
        return json.dumps({"source": source, "run_id": str(run_id), "generation": generation,
                           "executor_id": str(dispatch_id),
                           "process_incarnation": str(dispatch_incarnation), **extra}, sort_keys=True)

    def before_mutation(dispatch_engine_id: str, _container_id: str | None = None) -> None:
        # Durable intent precedes every dispatch create/start/connect; failure aborts them.
        nonlocal dispatch_intent
        if not dispatch_engine_id or len(dispatch_engine_id) > 200:
            raise RuntimeError("engine identity is invalid")
        try:
            changed = db.execute(text("""
                UPDATE runtime_executors SET engine_id=:engine, proof=CAST(:proof AS jsonb), updated_at=now()
                WHERE id=:id AND run_id=:run AND generation=:generation AND kind='dispatch'
                  AND process_incarnation=:incarnation AND state='starting' AND container_id IS NULL
                  AND proof='{}'::jsonb AND (engine_id IS NULL OR engine_id=:engine)
            """), {"engine": dispatch_engine_id, "id": dispatch_id, "run": run_id,
                    "generation": generation, "incarnation": dispatch_incarnation,
                    "proof": _identity_proof("dispatch-launch-intent", engine_id=dispatch_engine_id)}).rowcount
            if changed != 1:
                raise RuntimeError("dispatch launch intent was not durably recorded")
            db.commit()
        except Exception as exc:
            db.rollback()
            raise RuntimeError("dispatch launch intent was not durably recorded") from exc
        dispatch_intent = True
    worker_create_attempted = False
    try:
        network, broker_ip = cfg.engine.create_run_network(run_id, generation, worker_id)
        dispatch_attempted = True
        try:
            dispatch_ref = cfg.dispatch.start(
                db, run_id, generation, network, broker_ip, dispatch_id, dispatch_incarnation,
                before_mutation=before_mutation,
            )
        except DispatchPreLaunchRejected:
            dispatch_rejected = True
            raise
        if not dispatch_intent:
            raise RuntimeError("dispatch adapter returned without recording launch intent")
        if (dispatch_ref.executor_id, dispatch_ref.process_incarnation, dispatch_ref.run_id,
                dispatch_ref.generation, dispatch_ref.kind) != (
                dispatch_id, dispatch_incarnation, run_id, generation, "dispatch"):
            raise RuntimeError("dispatch executor returned a different physical identity")
        _bind_executor(db, dispatch_ref)
        db.execute(text("UPDATE runtime_executors SET state='active', updated_at=now() WHERE id=:id AND state='starting'"),
                   {"id": dispatch_id})
        db.commit()
        bootstrap = cfg.bootstrap_factory(db, run_id, generation)
        db.commit()  # bootstrap writes nothing; release the run-row lock it may hold
        context = RuntimeContextV1.model_validate_json(bootstrap.context)
        if (context.run_id != run_id or context.generation != generation
                or context.revision != run["revision"]
                or context.runtime_commit != cfg.runtime_commit
                or context.image_digest != cfg.image_digest
                or context.skills_digest != cfg.skills_digest
                or context.environment_digest != cfg.environment_digest):
            raise RuntimeError("trusted bootstrap identity differs from run pins")
        checkpoint_revision = db.execute(
            text("SELECT COALESCE(MAX(revision), 0) FROM checkpoints WHERE run_id=:run"),
            {"run": run_id},
        ).scalar_one()
        if bootstrap.metadata.checkpoint_revision != checkpoint_revision:
            raise RuntimeError("bootstrap checkpoint sequence differs from durable history")
        capability = cfg.capability_factory(db, run_id, generation)
        if not capability or len(capability) > 4096:
            raise RuntimeError("invalid worker capability")
        worker_create_attempted = True
        container_id, engine_id = cfg.engine.create_worker(
            cfg.image, run_id, generation, worker_id, worker_incarnation,
            network, _numeric_broker_url(cfg.broker_url, broker_ip),
        )
        _bind_executor(db, ExecutorRef(worker_id, run_id, generation, "worker", None,
                                       worker_incarnation, engine_id, container_id))
        cfg.engine.start_worker(container_id)
        proof = cfg.engine.install_network_policy(container_id, broker_ip, cfg.broker_port)
        cfg.engine.install_bootstrap(container_id, bootstrap, capability)
        if not cfg.dispatch.ready(db, dispatch_ref, broker_ip, cfg.broker_port):
            raise RuntimeError("private dispatch service did not become ready")
        current = db.execute(text("SELECT state, generation FROM runs WHERE id=:run FOR UPDATE"),
                             {"run": run_id}).one_or_none()
        if current is None or current.state != "running" or current.generation != generation:
            raise DomainError("revision_conflict", 409)
        cfg.engine.release_worker(container_id)
        db.execute(text("""
            UPDATE runtime_executors SET state = 'active', proof = CAST(:proof AS jsonb),
                updated_at = now() WHERE id = :id AND state = 'starting'
        """), {"proof": json.dumps(proof, sort_keys=True), "id": worker_id})
        db.commit()
        return container_id
    except Exception:
        # A partial launch is fenced by exact physical identities before any new
        # generation can obtain authority. Unknown inspection remains paused.
        try:
            if not dispatch_attempted or (dispatch_rejected and not dispatch_intent):
                db.execute(text("""
                    UPDATE runtime_executors SET state='inactive',
                        proof=CAST(:proof AS jsonb), updated_at=now()
                    WHERE id=:id AND state='starting' AND proof='{}'::jsonb
                      AND container_id IS NULL AND engine_id IS NULL
                """), {"id": dispatch_id, "proof": _identity_proof("dispatch-launch-not-attempted")})
                db.commit()
            if not worker_create_attempted:
                db.execute(text("""
                    UPDATE runtime_executors SET state='inactive',
                        proof='{"source":"worker-launch-not-attempted"}'::jsonb,
                        updated_at=now() WHERE id=:id AND state='starting'
                """), {"id": worker_id})
                db.commit()
            recover(db, run_id)
        except Exception:
            db.rollback()
        raise


def stop(db: Session, run_id: UUID, grace_seconds: int) -> RunView:
    if type(grace_seconds) is not int or not 0 <= grace_seconds <= 120:
        raise ValueError("grace_seconds must be between zero and 120")
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('scientist.supervisor.claim'))"))
    row = db.execute(text("SELECT * FROM runs WHERE id = :run FOR UPDATE"), {"run": run_id}).mappings().one_or_none()
    if row is None:
        raise DomainError("not_found", 404)
    if row["state"] in {"completed", "failed", "canceled", "rejected"}:
        return _run_view(db, run_id)
    if row["state"] in {"planning", "awaiting_approval", "queued"}:
        db.execute(text("UPDATE runs SET state='canceled', cancel_requested=true, lease_expires_at=NULL WHERE id=:run"),
                   {"run": run_id})
        _event(db, run_id, row["revision"], "run.state", {"state": "canceled"})
        db.commit()
        return _run_view(db, run_id)
    if row["state"] not in {"running", "recovering", "stopping", "waiting_input"}:
        return _run_view(db, run_id)
    db.execute(text("UPDATE runs SET state='stopping', cancel_requested=true WHERE id=:run"), {"run": run_id})
    db.commit()
    try:
        fenced = _fence_generation(db, run_id, None, grace_seconds)
    except Exception:
        db.rollback()
        fenced = False
    if not fenced:
        current = db.execute(text("SELECT state, revision FROM runs WHERE id=:run FOR UPDATE"),
                             {"run": run_id}).one()
        if current.state not in {"completed", "failed", "canceled", "rejected"}:
            db.execute(text("UPDATE runs SET state='waiting_input', waiting_reason='executor_quiescence_unproven', lease_expires_at=NULL WHERE id=:run"),
                       {"run": run_id})
            _event(db, run_id, current.revision, "run.state", {"state": "waiting_input"})
        db.commit()
        return _run_view(db, run_id)
    latest = db.execute(text("SELECT state FROM runs WHERE id=:run FOR UPDATE"), {"run": run_id}).one()
    if latest.state in {"completed", "failed", "canceled", "rejected"}:
        db.rollback()
        return _run_view(db, run_id)
    limits.settle_active_interval(db, run_id)
    db.execute(text("""
        UPDATE runs SET state='canceled', lease_expires_at=NULL,
            waiting_reason=NULL, error_code=NULL
        WHERE id=:run AND state='stopping' AND generation=:generation
    """), {"run": run_id, "generation": row["generation"]})
    db.commit()
    return _run_view(db, run_id)


def recover(db: Session, run_id: UUID) -> RunView:
    """Reap exact old worker and broker executors before queueing continuation."""
    cfg = _require_config()
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('scientist.supervisor.claim'))"))
    row = db.execute(text("SELECT * FROM runs WHERE id = :run FOR UPDATE"), {"run": run_id}).mappings().one_or_none()
    if row is None:
        raise DomainError("not_found", 404)
    if row["state"] in {"completed", "failed", "canceled", "rejected"}:
        return _run_view(db, run_id)
    generation = row["generation"]
    executors = db.execute(text("""
        SELECT * FROM runtime_executors WHERE run_id=:run
        ORDER BY generation, CASE kind WHEN 'worker' THEN 0 ELSE 1 END, id FOR UPDATE
    """), {"run": run_id}).mappings().all()
    uncertain = False
    for executor in executors:
        if executor["state"] == "inactive":
            if not _inactive_executor_proven(executor):
                uncertain = True
                db.execute(text("UPDATE runtime_executors SET state='unknown', updated_at=now() WHERE id=:id"),
                           {"id": executor["id"]})
            continue
        missing_physical_identity = not executor["container_id"] or not executor["engine_id"]

        if executor["kind"] == "worker":
            ref = None
            if missing_physical_identity:
                if not executor["engine_id"]:
                    ref = None
                else:
                    try:
                        ref = cfg.engine.find_worker(
                            run_id, executor["generation"], executor["id"],
                            executor["process_incarnation"], executor["engine_id"],
                        )
                    except Exception:
                        ref = False
                if (
                    ref is None
                    or ref is False
                    or executor["container_id"] not in (None, ref.container_id)
                    or executor["engine_id"] not in (None, ref.engine_id)
                    or not _matches_executor(
                        {**executor, "container_id": ref.container_id, "engine_id": ref.engine_id}, ref
                    )
                ):
                    uncertain = True
                    db.execute(text("UPDATE runtime_executors SET state='unknown', updated_at=now() WHERE id=:id"),
                               {"id": executor["id"]})
                    continue
                if executor["state"] == "unknown":
                    if not _requalify_unknown_executor(db, executor, ref):
                        uncertain = True
                        continue
                    executor = {**executor, "state": "starting"}
                if missing_physical_identity and not _bind_recovered_worker(db, executor, ref):
                    uncertain = True
                    db.execute(text("UPDATE runtime_executors SET state='unknown', updated_at=now() WHERE id=:id"),
                               {"id": executor["id"]})
                    continue
            else:
                ref = _executor_ref(executor)

            if not cfg.engine.stop_worker(ref, 0):
                uncertain = True
                db.execute(text("UPDATE runtime_executors SET state='unknown', updated_at=now() WHERE id=:id"),
                           {"id": executor["id"]})
                continue
            proof = {
                "source": "owned-engine-exact-container",
                "engine_id": ref.engine_id,
                "container_id": ref.container_id,
                "stopped_at": datetime.now(timezone.utc).isoformat(),
            }
            changed = db.execute(text("""
                UPDATE runtime_executors SET state='inactive', proof=CAST(:proof AS jsonb), updated_at=now()
                WHERE id=:id AND run_id=:run AND generation=:generation AND kind='worker'
                  AND container_id=:container AND engine_id=:engine AND process_incarnation=:incarnation
            """), {"proof": json.dumps(proof, sort_keys=True), "id": executor["id"], "run": run_id,
                  "generation": executor["generation"], "container": ref.container_id,
                  "engine": ref.engine_id, "incarnation": ref.process_incarnation}).rowcount
            if changed != 1:
                uncertain = True
                db.execute(text("UPDATE runtime_executors SET state='unknown', updated_at=now() WHERE id=:id"),
                           {"id": executor["id"]})
            continue

        if executor["kind"] == "dispatch":
            ref = None
            if missing_physical_identity:
                try:
                    ref = cfg.dispatch.find(
                        db, run_id, executor["generation"], executor["id"],
                        executor["operation_id"], executor["process_incarnation"],
                    )
                except Exception:
                    ref = None
                if (
                    ref is None
                    or executor["container_id"] not in (None, ref.container_id)
                    or executor["engine_id"] not in (None, ref.engine_id)
                    or not _matches_executor(
                        {**executor, "container_id": ref.container_id, "engine_id": ref.engine_id}, ref
                    )
                ):
                    uncertain = True
                    db.execute(text("UPDATE runtime_executors SET state='unknown', updated_at=now() WHERE id=:id"),
                               {"id": executor["id"]})
                    continue
                if executor["state"] == "unknown":
                    if not _requalify_unknown_executor(db, executor, ref):
                        uncertain = True
                        continue
                    executor = {**executor, "state": "starting"}
                if not _bind_recovered_dispatch(db, executor, ref):
                    uncertain = True
                    db.execute(text("UPDATE runtime_executors SET state='unknown', updated_at=now() WHERE id=:id"),
                               {"id": executor["id"]})
                    continue
            else:
                ref = _executor_ref(executor)

            if not cfg.dispatch.stop(db, ref, 0):
                uncertain = True
                db.execute(text("UPDATE runtime_executors SET state='unknown', updated_at=now() WHERE id=:id"),
                           {"id": executor["id"]})
                continue
            pending = db.execute(text("""SELECT operation_id FROM operations
                WHERE run_id=:run AND generation=:generation AND state IN ('reserved','unknown')
                ORDER BY operation_id"""),
                {"run": run_id, "generation": executor["generation"]}).scalars().all()
            proof = {
                "source": "owned-engine-exact-container",
                "engine_id": ref.engine_id,
                "container_id": ref.container_id,
                "stopped_at": datetime.now(timezone.utc).isoformat(),
            }
            if not _record_dispatch_inactive(db, cfg.dispatch, executor["id"], ref, pending, proof):
                uncertain = True
            continue

        uncertain = True
        db.execute(text("UPDATE runtime_executors SET state='unknown', updated_at=now() WHERE id=:id"),
                   {"id": executor["id"]})
    pending_unknown = db.execute(text("""
        SELECT 1 FROM operations WHERE run_id=:run AND state='unknown' AND NOT COALESCE(result ? 'retry_identity', false) LIMIT 1
    """), {"run": run_id}).scalar_one_or_none() is not None
    if uncertain:
        reason = "executor_quiescence_unproven"
        db.execute(text("""
            UPDATE runs SET state='waiting_input', waiting_reason=:reason,
                lease_expires_at=NULL WHERE id=:run
        """), {"run": run_id, "reason": reason})
        _event(db, run_id, row["revision"], "run.state", {"state": "waiting_input"})
    elif row["cancel_requested"]:
        limits.settle_active_interval(db, run_id)
        db.execute(text("""
            UPDATE runs SET state='canceled', waiting_reason=NULL,
                lease_expires_at=NULL WHERE id=:run
        """), {"run": run_id})
        _event(db, run_id, row["revision"], "run.state", {"state": "canceled"})
    elif pending_unknown:
        limits.settle_active_interval(db, run_id)
        reason = "unknown_outcome" if pending_unknown else "executor_quiescence_unproven"
        db.execute(text("""
            UPDATE runs SET state='waiting_input', waiting_reason=:reason,
                lease_expires_at=NULL WHERE id=:run
        """), {"run": run_id, "reason": reason})
        _event(db, run_id, row["revision"], "run.state", {"state": "waiting_input"})
    else:
        limits.settle_active_interval(db, run_id)
        # Reserved effects have no completed response. The broker must reconcile
        # them before continuation; without a durable outcome, retain the budget.
        reserved = db.execute(text("""
            SELECT 1 FROM operations WHERE run_id=:run AND state='reserved' LIMIT 1
        """), {"run": run_id}).scalar_one_or_none() is not None
        if reserved:
            db.execute(text("UPDATE operations SET state='unknown' WHERE run_id=:run AND state='reserved'"),
                       {"run": run_id})
            db.execute(text("UPDATE runs SET state='waiting_input', waiting_reason='unknown_outcome', lease_expires_at=NULL WHERE id=:run"),
                       {"run": run_id})
            _event(db, run_id, row["revision"], "run.state", {"state": "waiting_input"})
        else:
            has_effects = db.execute(text("SELECT 1 FROM operations WHERE run_id=:run LIMIT 1"),
                                     {"run": run_id}).scalar_one_or_none() is not None
            checkpoint_row = db.execute(text("""
                SELECT manifest FROM checkpoints WHERE run_id=:run
                ORDER BY revision DESC LIMIT 1
            """), {"run": run_id}).mappings().one_or_none()
            if has_effects and checkpoint_row is None:
                db.execute(text("""
                    UPDATE runs SET state='waiting_input', waiting_reason='checkpoint_missing',
                        lease_expires_at=NULL WHERE id=:run
                """), {"run": run_id})
                _event(db, run_id, row["revision"], "run.state", {"state": "waiting_input"})
            elif checkpoint_row is not None:
                try:
                    manifest = CheckpointManifest.model_validate(checkpoint_row["manifest"])
                    with tempfile.TemporaryDirectory(prefix="scientist-checkpoint-verify-") as restore_dir:
                        raw_context = checkpoints.restore(db, manifest, Path(restore_dir))
                    context = RuntimeContextV1.model_validate_json(raw_context)
                    if context.boundary == "final":
                        if (
                            context.pending_assistant is None
                            and context.run_id == run_id
                            and context.project_id == row["project_id"]
                            and context.revision == row["revision"]
                            and context.plan_digest == row["plan_digest"]
                            and context.generation == generation
                        ):
                            changed = db.execute(text("""
                                UPDATE runs SET state='completed', waiting_reason=NULL,
                                    error_code=NULL, lease_expires_at=NULL
                                WHERE id=:run AND generation=:generation
                                  AND cancel_requested=false
                                  AND state NOT IN ('completed','failed','canceled','rejected')
                            """), {"run": run_id, "generation": generation}).rowcount
                            if changed == 1:
                                _event(db, run_id, row["revision"], "run.state", {"state": "completed"})
                            else:
                                db.rollback()
                                return _run_view(db, run_id)
                        else:
                            db.execute(text("""
                                UPDATE runs SET state='waiting_input',
                                    waiting_reason='checkpoint_integrity_unproven',
                                    lease_expires_at=NULL WHERE id=:run AND generation=:generation
                            """), {"run": run_id, "generation": generation})
                            _event(db, run_id, row["revision"], "run.state", {"state": "waiting_input"})
                    else:
                        _queue_recovered_run(db, run_id, generation, row["revision"])
                except Exception:
                    db.execute(text("""
                        UPDATE runs SET state='waiting_input',
                            waiting_reason='checkpoint_integrity_unproven',
                            lease_expires_at=NULL WHERE id=:run AND generation=:generation
                    """), {"run": run_id, "generation": generation})
                    _event(db, run_id, row["revision"], "run.state", {"state": "waiting_input"})
            else:
                _queue_recovered_run(db, run_id, generation, row["revision"])
    db.commit()
    return _run_view(db, run_id)


def _queue_recovered_run(db: Session, run_id: UUID, generation: int, revision: int) -> None:
    current = db.execute(
        text("SELECT * FROM runs WHERE id=:run AND generation=:generation FOR UPDATE"),
        {"run": run_id, "generation": generation},
    ).mappings().one_or_none()
    if current is None:
        return
    if limits.budget_exhausted(db, current):
        limits.mark_budget_wait(db, run_id, revision, _event)
        return
    changed = db.execute(text("""
        UPDATE runs SET state='queued', waiting_reason=NULL, lease_expires_at=NULL, error_code=NULL
        WHERE id=:run AND generation=:generation AND cancel_requested=false
    """), {"run": run_id, "generation": generation}).rowcount
    if changed == 1:
        _event(db, run_id, revision, "run.state", {"state": "queued"})


def _fence_generation(db: Session, run_id: UUID, generation: int | None, grace_seconds: int) -> bool:
    cfg = _require_config()
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('scientist.supervisor.claim'))"))
    rows = db.execute(text("""
        SELECT * FROM runtime_executors WHERE run_id=:run
            AND (CAST(:generation AS integer) IS NULL OR generation=:generation)
        ORDER BY CASE kind WHEN 'worker' THEN 0 ELSE 1 END, id FOR UPDATE
    """), {"run": run_id, "generation": generation}).mappings().all()
    complete = True
    for row in rows:
        if row["state"] == "inactive":
            if not _inactive_executor_proven(row):
                complete = False
                db.execute(text("UPDATE runtime_executors SET state='unknown', updated_at=now() WHERE id=:id"),
                           {"id": row["id"]})
            continue
        if not row["container_id"] or not row["engine_id"]:
            complete = False
            db.execute(text("UPDATE runtime_executors SET state='unknown', updated_at=now() WHERE id=:id"),
                       {"id": row["id"]})
            continue
        if row["kind"] == "worker":
            ref = _executor_ref(row)
            ok = cfg.engine.stop_worker(ref, grace_seconds)
            if ok:
                proof = {
                    "source": "owned-engine-generation-fence",
                    "engine_id": ref.engine_id,
                    "container_id": ref.container_id,
                    "stopped_at": datetime.now(timezone.utc).isoformat(),
                }
                ok = db.execute(text("""
                    UPDATE runtime_executors
                    SET state='inactive', proof=CAST(:proof AS jsonb), updated_at=now()
                    WHERE id=:id AND run_id=:run AND generation=:generation
                      AND kind='worker' AND container_id=:container AND engine_id=:engine
                      AND process_incarnation=:incarnation
                """), {
                    "proof": json.dumps(proof, sort_keys=True),
                    "id": ref.executor_id,
                    "run": ref.run_id,
                    "generation": ref.generation,
                    "container": ref.container_id,
                    "engine": ref.engine_id,
                    "incarnation": ref.process_incarnation,
                }).rowcount == 1
        else:
            ref = _executor_ref(row)
            stopped = cfg.dispatch.stop(db, ref, grace_seconds)
            pending = db.execute(text("""SELECT operation_id FROM operations WHERE run_id=:run AND generation=:generation
                AND state IN ('reserved','unknown')"""),
                {"run": run_id, "generation": row["generation"]}).scalars().all()
            proof = {"source": "owned-engine-generation-fence", "engine_id": ref.engine_id,
                     "container_id": ref.container_id, "stopped_at": datetime.now(timezone.utc).isoformat()}
            ok = stopped and _record_dispatch_inactive(
                db, cfg.dispatch, row["id"], ref, pending, proof
            )
        state = "inactive" if ok else "unknown"
        complete = complete and ok
        db.execute(text("UPDATE runtime_executors SET state=:state, updated_at=now() WHERE id=:id"),
                   {"state": state, "id": row["id"]})
    db.commit()
    return complete


def _active_run(db: Session, run_id: UUID, generation: int):
    row = db.execute(text("SELECT * FROM runs WHERE id=:run FOR UPDATE"), {"run": run_id}).mappings().one_or_none()
    if row is None:
        raise DomainError("not_found", 404)
    if row["state"] != "running" or row["generation"] != generation:
        raise DomainError("revision_conflict", 409)
    return row


def _bind_executor(db: Session, ref: ExecutorRef) -> None:
    if (not re.fullmatch(r"[a-f0-9]{64}", ref.container_id)
            or not ref.engine_id or len(ref.engine_id) > 200):
        raise RuntimeError("engine returned invalid physical identity")
    changed = db.execute(text("""
        UPDATE runtime_executors SET container_id=:container, engine_id=:engine, updated_at=now()
        WHERE id=:id AND run_id=:run AND generation=:generation AND kind=:kind
          AND operation_id IS NOT DISTINCT FROM :operation_id
          AND process_incarnation=:incarnation AND state='starting' AND container_id IS NULL
          AND (engine_id IS NULL OR engine_id=:engine)
    """), {"container": ref.container_id, "engine": ref.engine_id, "id": ref.executor_id,
            "run": ref.run_id, "generation": ref.generation, "kind": ref.kind,
            "operation_id": ref.operation_id, "incarnation": ref.process_incarnation}).rowcount
    if changed != 1:
        raise RuntimeError("executor physical identity was already bound")
    db.commit()


def _matches_executor(row, ref: ExecutorRef) -> bool:
    return (
        ref.executor_id == row["id"]
        and ref.run_id == row["run_id"]
        and ref.generation == row["generation"]
        and ref.kind == row["kind"]
        and ref.operation_id == row["operation_id"]
        and ref.process_incarnation == row["process_incarnation"]
        and bool(re.fullmatch(r"[a-f0-9]{64}", ref.container_id))
        and bool(ref.engine_id)
        and len(ref.engine_id) <= 200
    )


def _bind_recovered_dispatch(db: Session, row, ref: ExecutorRef) -> bool:
    """Persist an exact labeled dispatch container found after an interrupted launch."""
    if row["container_id"] and row["engine_id"]:
        return row["container_id"] == ref.container_id and row["engine_id"] == ref.engine_id
    if row["state"] not in {"starting", "unknown"}:
        return False
    changed = db.execute(
        text("""
            UPDATE runtime_executors
            SET container_id=:container, engine_id=:engine, updated_at=now()
            WHERE id=:id AND run_id=:run AND generation=:generation
              AND kind='dispatch' AND operation_id IS NOT DISTINCT FROM :operation_id
              AND process_incarnation=:incarnation AND state=:state
              AND (container_id IS NULL OR container_id=:container)
              AND (engine_id IS NULL OR engine_id=:engine)
        """),
        {
            "container": ref.container_id,
            "engine": ref.engine_id,
            "id": row["id"],
            "run": ref.run_id,
            "generation": ref.generation,
            "operation_id": ref.operation_id,
            "incarnation": ref.process_incarnation,
            "state": row["state"],
        },
    ).rowcount
    return changed == 1


def _bind_recovered_worker(db: Session, row, ref: ExecutorRef) -> bool:
    """Bind an exact worker discovery without releasing the locked recovery transaction."""
    changed = db.execute(
        text("""
            UPDATE runtime_executors
            SET container_id=:container, engine_id=:engine, updated_at=now()
            WHERE id=:id AND run_id=:run AND generation=:generation AND kind='worker'
              AND operation_id IS NOT DISTINCT FROM :operation_id
              AND process_incarnation=:incarnation AND state='starting'
              AND (container_id IS NULL OR container_id=:container)
              AND (engine_id IS NULL OR engine_id=:engine)
        """),
        {
            "container": ref.container_id,
            "engine": ref.engine_id,
            "id": row["id"],
            "run": ref.run_id,
            "generation": ref.generation,
            "operation_id": ref.operation_id,
            "incarnation": ref.process_incarnation,
        },
    ).rowcount
    return changed == 1


def _requalify_unknown_executor(db: Session, row, ref: ExecutorRef) -> bool:
    """Allow one exact rediscovery to retry first physical binding after an unknown probe."""
    changed = db.execute(
        text("""
            UPDATE runtime_executors SET state='starting', updated_at=now()
            WHERE id=:id AND run_id=:run AND generation=:generation AND kind=:kind
              AND operation_id IS NOT DISTINCT FROM :operation_id
              AND process_incarnation=:incarnation AND state='unknown'
              AND (container_id IS NULL OR container_id=:container)
              AND (engine_id IS NULL OR engine_id=:engine)
        """),
        {
            "id": row["id"],
            "run": ref.run_id,
            "generation": ref.generation,
            "kind": ref.kind,
            "operation_id": ref.operation_id,
            "incarnation": ref.process_incarnation,
            "container": ref.container_id,
            "engine": ref.engine_id,
        },
    ).rowcount
    return changed == 1


def _bind_engine(db: Session, executor_id: UUID, engine_id: str) -> None:
    if not engine_id or len(engine_id) > 200:
        raise RuntimeError("engine identity is invalid")
    changed = db.execute(text("""
        UPDATE runtime_executors SET engine_id=:engine, updated_at=now()
        WHERE id=:id AND state='starting' AND engine_id IS NULL
    """), {"engine": engine_id, "id": executor_id}).rowcount
    if changed != 1:
        raise RuntimeError("engine identity was already bound")
    db.commit()


def _executor_ref(row) -> ExecutorRef:
    if not row["container_id"] or not row["engine_id"]:
        raise RuntimeError("executor physical identity is unknown")
    return ExecutorRef(row["id"], row["run_id"], row["generation"], row["kind"],
                       row["operation_id"], row["process_incarnation"],
                       row["engine_id"], row["container_id"])


def _require_config() -> RuntimeConfig:
    if _config is None:
        raise RuntimeError("trusted supervisor configuration is unavailable")
    return _config


class DockerWorkerEngine:
    """Docker CLI controls pinned to the dedicated, fixed-version Colima VM."""

    def __init__(self, context: str = _DOCKER_CONTEXT, profile: str = _COLIMA_PROFILE):
        if context != _DOCKER_CONTEXT or profile != _COLIMA_PROFILE:
            raise ValueError("worker supervisor only permits the owned test VM")
        self.context, self.profile = context, profile

    def _docker(self, *args: str, input: bytes | None = None) -> str:
        result = subprocess.run(
            ["docker", "--context", self.context, *args], input=input,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=90,
        )
        if result.returncode:
            raise RuntimeError("owned Docker operation failed: " + result.stderr.decode("utf-8", "replace")[:1000])
        return result.stdout.decode("utf-8", "replace").strip()

    def _vm(self, container_id: str, pid: int, *args: str) -> str:
        if (
            not re.fullmatch(r"[a-f0-9]{64}", container_id)
            or not isinstance(pid, int)
            or pid <= 0
            or not args
            or args[0] not in {"iptables", "ip6tables", "iptables-save", "ip6tables-save"}
        ):
            raise RuntimeError("owned worker namespace identity or command is invalid")
        identity = self._docker(
            "inspect",
            "--format",
            '{{.Id}}|{{.State.Pid}}|{{.State.Running}}|{{index .Config.Labels "scientist.platform/kind"}}',
            container_id,
        ).split("|")
        if (
            len(identity) != 4
            or identity[0] != container_id
            or identity[1] != str(pid)
            or identity[2] != "true"
            or identity[3] != "worker"
        ):
            raise RuntimeError("owned worker namespace identity changed")
        result = subprocess.run(
            [
                "colima", "ssh", "--profile", self.profile, "--",
                "sudo", "-n", "nsenter", "-t", str(pid), "-n", *args,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=60,
        )
        if result.returncode:
            raise RuntimeError("owned VM namespace operation failed: " + result.stderr.decode("utf-8", "replace")[:1000])
        return result.stdout.decode("utf-8", "replace").strip()

    def engine_id(self) -> str:
        engine_id = self._docker("info", "--format", "{{.ID}}")
        if not engine_id or len(engine_id) > 200:
            raise RuntimeError("owned Docker engine identity unavailable")
        version = self._docker("version", "--format", "{{.Server.Version}}")
        if version != "29.8.2":
            raise RuntimeError("owned Docker engine version changed")
        return engine_id

    def create_run_network(self, run_id: UUID, generation: int, executor_id: UUID) -> tuple[str, str]:
        digest = hashlib.sha256(f"{run_id}:{generation}".encode()).digest()
        used = set()
        for network_id in self._docker("network", "ls", "-q").splitlines():
            if not network_id:
                continue
            try:
                configs = json.loads(self._docker("network", "inspect", "--format", "{{json .IPAM.Config}}", network_id))
            except Exception:
                continue
            for config in configs or []:
                subnet = config.get("Subnet")
                if subnet and subnet.startswith("172.29."):
                    try:
                        used.add(int(subnet.split(".")[2]))
                    except (IndexError, ValueError):
                        continue
        start = digest[0] % 190
        third = next((32 + (start + offset) % 190 for offset in range(190)
                      if 32 + (start + offset) % 190 not in used), None)
        if third is None:
            raise RuntimeError("per-run private IPv4 networks exhausted")
        network = f"scientist-run-{run_id.hex[:12]}-g{generation}"
        labels = ["--label", f"{_RUN_LABEL}={run_id}", "--label", f"{_GEN_LABEL}={generation}",
                  "--label", f"{_EXEC_LABEL}={executor_id}"]
        self._docker("network", "create", "--driver", "bridge", "--internal", "--ipv6",
                     "--subnet", f"172.29.{third}.0/29", "--subnet", f"fd42:{digest[1]:x}{digest[2]:x}:{digest[3]:x}::/64",
                     *labels, network)
        # The broker is joined by the separately trusted dispatch launcher. Worker
        # output is limited to the configured broker IP and port by namespace rules.
        return network, f"172.29.{third}.2"

    def _verified_image_id(self, image: str, image_digest: str) -> str:
        raw_image = re.fullmatch(r"sha256:[a-f0-9]{64}", image)
        named_image = re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/:+-]*@(sha256:[a-f0-9]{64})", image
        )
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_digest) or not (
            (raw_image and image == image_digest)
            or (named_image and named_image.group(1) == image_digest)
        ):
            raise DispatchPreLaunchRejected("worker image does not match configured immutable digest")
        raw = self._docker(
            "image", "inspect", "--format", "{{.Id}}|{{json .RepoDigests}}", image
        )
        try:
            image_id, repo_digests_json = raw.split("|", 1)
            repo_digests = json.loads(repo_digests_json)
        except (ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("worker image inspection is invalid") from exc
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise RuntimeError("worker image inspection has no immutable image ID")
        if image_id != image_digest:
            raise DispatchPreLaunchRejected("worker image does not match configured immutable digest")
        if named_image:
            if not isinstance(repo_digests, list):
                raise RuntimeError("worker image inspection is invalid")
            if image not in repo_digests:
                raise DispatchPreLaunchRejected("worker image does not match configured immutable digest")
        return image_id

    def create_worker(
        self, image: str, run_id: UUID, generation: int, executor_id: UUID,
        incarnation: UUID, network: str, broker_url: str,
    ) -> tuple[str, str]:
        cfg = _require_config()
        if image != cfg.image:
            raise RuntimeError("worker image does not match configured immutable digest")
        image_id = self._verified_image_id(image, cfg.image_digest)
        engine_id = self.engine_id()
        labels = ["--label", f"{_RUN_LABEL}={run_id}", "--label", f"{_GEN_LABEL}={generation}",
                  "--label", f"{_EXEC_LABEL}={executor_id}", "--label", f"{_KIND_LABEL}=worker",
                  "--label", f"scientist.platform/incarnation={incarnation}"]
        limits = ["--read-only", "--user", "65532:65532", "--cap-drop", "ALL",
                  "--security-opt", "no-new-privileges:true", "--cpus", "1", "--memory", "1073741824",
            "--memory-swap", "1073741824", "--pids-limit", "128", "--shm-size", "16777216",
            "--ulimit", "nofile=1024:1024",
                  "--network", network, "--restart", "no"]
        tmpfs = []
        for path, size, mode, uid, gid in (
            ("/workspace", "67108864", "1777", 0, 0),
            ("/home/scientist", "33554432", "0700", 65532, 65532),
            ("/run/hermes-home", "67108864", "0700", 65532, 65532),
            ("/tmp", "16777216", "1777", 0, 0),
            ("/run/scientist/bootstrap", "100663296", "0755", 0, 0),
            ("/run/scientist/readiness", "65536", "0755", 0, 0),
            ("/run/scientist/capability", "1048576", "0755", 0, 0),
        ):
            tmpfs.extend(["--tmpfs", f"{path}:rw,noexec,nosuid,nodev,size={size},mode={mode},uid={uid},gid={gid}"])
        env = ["--env", "HOME=/home/scientist", "--env", "HERMES_HOME=/run/hermes-home",
               "--env", "SCIENTIST_BOOTSTRAP_DIR=/run/scientist/bootstrap",
               "--env", "SCIENTIST_READINESS_FILE=/run/scientist/readiness/ready",
               "--env", "SCIENTIST_WORKSPACE=/workspace", "--env", f"SCIENTIST_BROKER_URL={broker_url}",
               "--env", "SCIENTIST_CAPABILITY_FILE=/run/scientist/capability/token"]
        container_id = self._docker("create", *labels, *limits, *tmpfs, *env,
                                    "--workdir", "/opt/scientist", "--entrypoint", "python",
                                    image, "-m", "runtime.entrypoint")
        if not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise RuntimeError("Docker returned invalid worker container identity")
        container_image_id = self._docker("inspect", "--format", "{{.Image}}", container_id)
        if container_image_id != image_id:
            self._docker("rm", "--force", container_id)
            raise RuntimeError("created worker image differs from inspected immutable image")
        return container_id, engine_id

    def start_worker(self, container_id: str) -> None:
        self._docker("start", container_id)

    def install_bootstrap(
        self, container_id: str, bootstrap: WorkerBootstrap, capability: str,
    ) -> dict[str, str]:
        """Write trusted bootstrap into its mounted tmpfs and verify UID 65532 read access."""
        if not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise RuntimeError("worker container identity is invalid")
        if not capability or len(capability) > 4096:
            raise RuntimeError("invalid worker capability")
        files, token = _bootstrap_files(bootstrap, capability)
        bootstrap_dir = "/run/scientist/bootstrap"
        capability_dir = "/run/scientist/capability"
        sources = {
            f"{bootstrap_dir}/context.json": (files["context.json"], 0o444),
            f"{bootstrap_dir}/workspace.json": (files["workspace.json"], 0o444),
            f"{bootstrap_dir}/metadata.json": (files["metadata.json"], 0o444),
            f"{capability_dir}/token": (token, 0o440),
        }
        self._write_tmpfs_files(container_id, sources)
        paths = [
            f"{bootstrap_dir}/context.json",
            f"{bootstrap_dir}/workspace.json",
            f"{bootstrap_dir}/metadata.json",
            f"{capability_dir}/token",
        ]
        script = (
            "import hashlib,json,pathlib,sys; "
            "print(json.dumps({p:hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() "
            "for p in sys.argv[1:]}))"
        )
        raw = self._docker(
            "exec", "--user", "65532:65532", container_id,
            "/opt/python/bin/python3.14", "-c", script, *paths,
        )
        expected = {path: hashlib.sha256(data).hexdigest() for path, data in (
            (paths[0], files["context.json"]),
            (paths[1], files["workspace.json"]),
            (paths[2], files["metadata.json"]),
            (paths[3], token),
        )}
        try:
            actual = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("worker bootstrap readback is invalid") from exc
        if actual != expected:
            raise RuntimeError("worker bootstrap failed UID 65532 readback")
        return expected

    def _write_tmpfs_files(
        self, container_id: str, files: dict[str, tuple[bytes, int]],
    ) -> None:
        allowed = {
            "/run/scientist/bootstrap/context.json": 0o444,
            "/run/scientist/bootstrap/workspace.json": 0o444,
            "/run/scientist/bootstrap/metadata.json": 0o444,
            "/run/scientist/capability/token": 0o440,
            "/run/scientist/readiness/ready": 0o440,
        }
        if (
            not re.fullmatch(r"[a-f0-9]{64}", container_id)
            or not files
            or any(allowed.get(path) != mode for path, (_data, mode) in files.items())
        ):
            raise RuntimeError("worker tmpfs file set is invalid")
        entries = [{"path": path, "mode": mode,
                    "data": base64.b64encode(data).decode("ascii")}
                   for path, (data, mode) in files.items()]
        payload = json.dumps(entries, separators=(",", ":")).encode("ascii")
        script = """\
import base64, json, os, sys
allowed = {
    "/run/scientist/bootstrap/context.json": 0o444,
    "/run/scientist/bootstrap/workspace.json": 0o444,
    "/run/scientist/bootstrap/metadata.json": 0o444,
    "/run/scientist/capability/token": 0o440,
    "/run/scientist/readiness/ready": 0o440,
}
entries = json.load(sys.stdin)
if not isinstance(entries, list) or not 1 <= len(entries) <= len(allowed):
    raise SystemExit(2)
for entry in entries:
    path, mode = entry.get("path"), entry.get("mode")
    if path not in allowed or mode != allowed[path]:
        raise SystemExit(2)
    data = base64.b64decode(entry["data"], validate=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short bootstrap write")
            view = view[written:]
        os.fchmod(fd, mode)
    finally:
        os.close(fd)
"""
        self._docker(
            "exec", "--user", "0:65532", "-i", container_id,
            "/opt/python/bin/python3.14", "-c", script, input=payload,
        )

    def install_network_policy(self, container_id: str, broker_ip: str, broker_port: int) -> dict:
        info = json.loads(self._docker("inspect", "--format", "{{json .State}}", container_id))
        pid = info.get("Pid")
        if not isinstance(pid, int) or pid <= 0 or not info.get("Running"):
            raise RuntimeError("worker namespace is not running")
        # Restrictive defaults apply before the marker. Only the numeric broker
        # endpoint and established return traffic leave the worker namespace.
        for binary, family in (("iptables", "v4"), ("ip6tables", "v6")):
            commands = [
                [binary, "-w", "-F", "OUTPUT"], [binary, "-w", "-P", "OUTPUT", "DROP"],
                [binary, "-w", "-F", "INPUT"], [binary, "-w", "-P", "INPUT", "DROP"],
            ]
            if family == "v4":
                # Docker's embedded DNS target is loopback; this must precede
                # the generic loopback accept or the block is ineffective.
                commands.append([binary, "-w", "-A", "OUTPUT", "-d", "127.0.0.11", "-j", "DROP"])
            commands.extend([
                [binary, "-w", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"],
                [binary, "-w", "-A", "INPUT", "-i", "lo", "-j", "ACCEPT"],
                [binary, "-w", "-A", "INPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
            ])
            if family == "v4":
                commands.extend([
                    [binary, "-w", "-A", "OUTPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
                    [binary, "-w", "-A", "OUTPUT", "-d", broker_ip, "-p", "tcp", "--dport", str(broker_port), "-m", "conntrack", "--ctstate", "NEW", "-j", "ACCEPT"],
                ])
            else:
                commands.append([binary, "-w", "-A", "OUTPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"])
            for command in commands:
                self._vm(container_id, pid, *command)
        v4 = self._vm(container_id, pid, "iptables-save", "-t", "filter")
        v6 = self._vm(container_id, pid, "ip6tables-save", "-t", "filter")
        if ":OUTPUT DROP" not in v4 or f"-d {broker_ip}/32 -p tcp -m tcp --dport {broker_port}" not in v4:
            raise RuntimeError("IPv4 worker egress rules failed readback")
        if ":OUTPUT DROP" not in v6 or "127.0.0.11" in v6:
            raise RuntimeError("IPv6 worker egress rules failed readback")
        return {"worker_pid": pid, "ipv4_output": v4, "ipv6_output": v6,
                "readback": True, "broker_ip": broker_ip, "broker_port": broker_port}

    def release_worker(self, container_id: str) -> None:
        path = "/run/scientist/readiness/ready"
        self._write_tmpfs_files(container_id, {path: (b"ready", 0o440)})
        raw = self._docker(
            "exec", "--user", "65532:65532", container_id,
            "/opt/python/bin/python3.14", "-c",
            "from pathlib import Path; import sys; sys.stdout.write(Path(sys.argv[1]).read_text())",
            path,
        )
        if raw != "ready":
            raise RuntimeError("worker readiness marker failed UID 65532 readback")

    def stop_worker(self, executor: ExecutorRef, grace_seconds: int) -> bool:
        return self._stop_exact(executor, grace_seconds)

    def find_worker(self, run_id: UUID, generation: int, executor_id: UUID,
                    incarnation: UUID, expected_engine_id: str) -> ExecutorRef | None:
        """Look up a launch interrupted between create and identity persistence."""
        engine_id = self.engine_id()
        if engine_id != expected_engine_id:
            raise RuntimeError("owned Docker engine incarnation changed")
        ids = self._docker("ps", "-aq", "--no-trunc", "--filter", f"label={_EXEC_LABEL}={executor_id}",
                           "--filter", f"label={_RUN_LABEL}={run_id}",
                           "--filter", f"label={_GEN_LABEL}={generation}")
        matches = [item for item in ids.splitlines() if item]
        if not matches:
            return None
        if len(matches) != 1 or not re.fullmatch(r"[a-f0-9]{64}", matches[0]):
            raise RuntimeError("worker physical identity is ambiguous")
        labels = self._docker("inspect", "--format",
            "{{index .Config.Labels \"scientist.platform/incarnation\"}}|{{.Id}}", matches[0])
        actual_incarnation, container_id = labels.split("|", 1)
        if actual_incarnation != str(incarnation) or container_id != matches[0]:
            raise RuntimeError("worker incarnation differs from durable identity")
        return ExecutorRef(executor_id, run_id, generation, "worker", None, incarnation,
                           engine_id, container_id)

    def _stop_exact(self, executor: ExecutorRef, grace_seconds: int) -> bool:
        try:
            current_engine = self.engine_id()
            if current_engine != executor.engine_id:
                return False
            present = self._docker("ps", "-aq", "--no-trunc", "--filter", f"id={executor.container_id}")
            if not present:
                # A stopped executor can be garbage-collected before recovery.
                # Require exact-ID absence and a fresh same-engine check.
                return self.engine_id() == executor.engine_id
            if present.splitlines() != [executor.container_id]:
                return False
            labels = self._docker("inspect", "--format",
                                  "{{index .Config.Labels \"scientist.platform/executor\"}}|{{.Id}}|{{.State.Running}}",
                                  executor.container_id)
            label, container_id, running = labels.split("|", 2)
            if label != str(executor.executor_id) or container_id != executor.container_id:
                return False
            if running == "true":
                self._docker("stop", "--time", str(grace_seconds), executor.container_id)
            # Remove the exact fenced identity; absence is checked on same engine.
            self._docker("rm", "--force", executor.container_id)
            exists = self._docker("ps", "-aq", "--no-trunc", "--filter", f"id={executor.container_id}")
            return not exists and self.engine_id() == executor.engine_id
        except Exception:
            return False


def _bootstrap_files(bootstrap: WorkerBootstrap, capability: str) -> tuple[dict[str, bytes], bytes]:
    context = RuntimeContextV1.model_validate_json(bootstrap.context)
    workspace = [item.model_dump(mode="json") for item in bootstrap.workspace]
    if [{"path": item.path, "sha256": item.sha256, "size": item.size}
            for item in bootstrap.workspace] != [entry.model_dump(mode="json") for entry in context.workspace_manifest]:
        raise ValueError("bootstrap workspace differs from validated context manifest")
    for item in bootstrap.workspace:
        item.decoded_data()
    files = {
        "context.json": bootstrap.context,
        "workspace.json": json.dumps(workspace, separators=(",", ":")).encode(),
        "metadata.json": bootstrap.metadata.model_dump_json().encode(),
    }
    return files, capability.encode("utf-8")


def _numeric_broker_url(template: str, broker_ip: str) -> str:
    parsed = urlsplit(template)
    if parsed.scheme != "http" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("broker URL must be a trusted internal HTTP endpoint")
    return urlunsplit((parsed.scheme, f"{broker_ip}:{_require_config().broker_port}",
                       parsed.path, parsed.query, ""))


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o440 if name.endswith("token") else 0o444
            info.uid = 0
            info.gid = 65532 if name.endswith("token") else 0
            info.uname = info.gname = "root"
            archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()
