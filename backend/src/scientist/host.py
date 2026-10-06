"""Single-process host: composes the trusted runtime and serves the owner API.

Run with `python -m scientist.host --config /abs/host.json`. Nothing here runs at import time.
Logs carry only {event, stage, run_id, error_type}; secrets and exception messages are never logged.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import Future, ThreadPoolExecutor
import hashlib
from io import BytesIO
import json
import logging
import os
import re
import signal
import stat
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
from secrets import token_urlsafe
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import uvicorn
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool

from scientist import broker, objects, peer_reconciliation_supervisor, settings, supervisor, profile_preparation, scientific_authority
from scientist import db as database
from scientist.app import create_app
from scientist.contracts import (
    ComputeProfilePin, CsvDescribeGrantV1, OperationRequest, PlanSpec, ScientificBindingV2,
)
from scientist.dispatch_runtime import (
    _SECRET_NAMES,
    DispatchServiceConfig,
    check_egress_network,
    DockerDispatchRuntime,
    _parse_template,
    _validate_bucket,
)
from scientist.domain import _event
from scientist.private_worker_api import RuntimePins, WorkerController
from scientist.runtime_contracts import (
    MAX_COMPUTE_ENVELOPE_BYTES, MAX_COMPUTE_OUTPUT_BYTES, ComputeLaunchSpec, ComputeOutputEntryV2,
    ComputeResultEnvelopeV2, RUNTIME_COMMIT, BootstrapMetadata, RuntimeContextV1,
    canonical_bytes, compute_input_manifest_sha256, operation_fingerprint,
)
from scientist.supervisor import DockerWorkerEngine, WorkerBootstrap
from scientist.web_assets import checked_web_root, create_web_router

_MAX_CONFIG_BYTES = 64 * 1024
_START_FAILURES = 3
_BROKER_PORT = 8123  # fixed by DispatchServiceConfig
_MAX_COMPUTE_STAGE_BYTES = 512 * 1024
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:+-]*@(sha256:[a-f0-9]{64})")
_compute_engine = None
_compute_image_digest: str | None = None
_compute_recipe_directory: Path | None = None
_compute_recipe_manifest: str | None = None
_compute_state_dir: Path | None = None
# Prompt guidance, not control: permissions, budget and unknown outcomes are enforced in the backend (ADR-015).
SYSTEM_PROMPT = (
    "You are a careful research assistant. Answer the owner's approved question using only evidence "
    "you can verify through the approved plan. State uncertainty plainly and never invent sources.\n"
    "- Reply in the language the user writes in, Thai or English.\n"
    "- Keep information taken from abstracts separate from information taken from full texts, "
    "and say which one a statement relies on.\n"
    "- Never say you searched, read or ran anything unless a tool result in the conversation confirms it.\n"
    "- Cite only evidence you can verify, and state the limitations of the evidence."
)

_destinations: dict[str, str] = {}  # provider_id -> origin; set by compose, read by bootstrap


class ConfigError(ValueError):
    def __init__(self, message: str, fields: list[str] | None = None):
        super().__init__(message)
        self.fields = fields or []  # field names only, never input values


class HostError(RuntimeError):
    pass


def _log(event: str, stage: str, run_id: UUID | None = None, error_type: str | None = None, **extra) -> None:
    line = {"event": event, "stage": stage, **({"run_id": str(run_id)} if run_id else {}),
            **({"error_type": error_type} if error_type else {}), **extra}
    print(json.dumps(line, sort_keys=True), file=sys.stderr, flush=True)


# --- configuration ---------------------------------------------------------------------------

def _private_dir(path: Path) -> Path:
    try:
        info = path.lstat()
    except OSError:
        raise ValueError("directory unavailable") from None
    if (not path.is_absolute() or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid()):
        raise ValueError("directory must be absolute, owned and mode 0700")
    return path


class HostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = Field(strict=True, ge=1, le=1)
    database_url: str
    listen_port: int = Field(strict=True, ge=1024, le=65535)
    expected_engine_id: str = Field(min_length=1, max_length=200)
    worker_image: str
    compute_image: str | None = None
    dispatch_image: str
    service_network: str
    egress_network: str | None = None
    skills_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    s3_endpoint: str
    bucket: str
    secrets_dir: Path
    state_dir: Path
    scientific_bundle_dir: Path | None = None
    web_dist_dir: Path | None = None
    max_active: int = Field(strict=True, ge=1, le=3)
    poll_seconds: float = Field(default=2.0, ge=0.2, le=30)
    provider_destinations: dict[str, str]
    peer_destinations: dict[str, str] = Field(default_factory=dict, validate_default=True)

    @field_validator("database_url")
    @classmethod
    def _database(cls, value: str) -> str:
        try:
            url = make_url(value)
        except ArgumentError:
            raise ValueError("invalid database url") from None
        # libpq lets the query host override the URL host and accepts comma-separated lists; every entry must be local.
        query_hosts = url.query.get("host", ())
        hosts = [url.host or "", *(h for v in ((query_hosts,) if isinstance(query_hosts, str) else query_hosts) for h in v.split(","))]
        local = all(not h or h.startswith("/") or h in {"localhost", "127.0.0.1", "::1"} for h in hosts)
        if (url.drivername != "postgresql+psycopg" or url.password is not None or url.username is not None
                or set(url.query) - {"host", "port", "dbname"} or not local):
            raise ValueError("database url must be a passwordless local postgresql+psycopg url")
        return value

    @field_validator("egress_network")
    @classmethod
    def _egress(cls, value: str | None) -> str | None:
        if value is not None:
            check_egress_network(value)
        return value

    @field_validator("worker_image", "dispatch_image")
    @classmethod
    def _pinned(cls, value: str) -> str:
        if not _IMAGE.fullmatch(value):
            raise ValueError("image must be pinned by digest")
        return value

    @field_validator("s3_endpoint")
    @classmethod
    def _loopback(cls, value: str) -> str:
        parts = urlsplit(value)
        if (parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1"} or parts.port is None
                or parts.username or parts.password or parts.path not in ("", "/") or parts.query or parts.fragment):
            raise ValueError("s3 endpoint must be a loopback http origin")
        return value

    @field_validator("bucket")
    @classmethod
    def _bucket(cls, value: str) -> str:
        return _validate_bucket(value)

    @field_validator("compute_image")
    @classmethod
    def _compute_image(cls, value: str | None) -> str | None:
        if value is not None and not _IMAGE.fullmatch(value):
            raise ValueError("compute image must be immutable")
        return value

    @field_validator("secrets_dir", "state_dir")
    @classmethod
    def _dir(cls, value: Path) -> Path:
        return _private_dir(value)

    @field_validator('scientific_bundle_dir')
    @classmethod
    def _scientific_bundle(cls, value: Path | None) -> Path | None:
        return _private_dir(value) if value is not None else None

    @field_validator("web_dist_dir")
    @classmethod
    def _web_dist(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        try:
            return checked_web_root(value)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("web distribution is unavailable") from exc

    @field_validator("provider_destinations")
    @classmethod
    def _providers(cls, value: dict[str, str]) -> dict[str, str]:
        if not value or any(str(UUID(k)) != k for k in value):
            raise ValueError("provider destinations must be non-empty with canonical UUID keys")
        # One map for the domain plan check, the broker and the dispatch template: refuse any drift.
        if value != dict(settings.provider_destinations()):
            raise ValueError("provider destinations differ from SCIENTIST_PROVIDER_DESTINATIONS")
        return value

    @field_validator("peer_destinations", mode="before")
    @classmethod
    def _peers(cls, value: object) -> dict[str, str]:
        parsed = settings.parse_peer_destinations(value)
        if parsed != value:
            raise ValueError("peer destinations must be canonical UUID origins")
        if parsed != settings.peer_destinations():
            raise ValueError("peer destinations differ SCIENTIST_PEER_DESTINATIONS")
        return parsed

    @model_validator(mode="after")
    def _secrets(self) -> "HostConfig":
        for name in _SECRET_NAMES:
            info = (self.secrets_dir / name).lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or not 0 < info.st_size <= 16 * 1024:
                raise ValueError("secret file must be a bounded regular file")
        return self


def _unique(pairs):
    out = dict(pairs)
    if len(out) != len(pairs):
        raise ValueError("duplicate key")
    return out


def load_config(path: Path | str) -> HostConfig:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise ValueError("too large")
        return HostConfig.model_validate(json.loads(raw, object_pairs_hook=_unique))
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in e["loc"]) or "model" for e in exc.errors(include_input=False)})
        raise ConfigError("invalid host configuration", fields) from None  # names only: values may be sensitive
    except (OSError, ValueError, TypeError):
        raise ConfigError("invalid host configuration") from None


# --- composition -----------------------------------------------------------------------------

def _secret(cfg: HostConfig, name: str) -> bytes:
    value = (cfg.secrets_dir / name).read_bytes().strip()
    if not value:
        raise HostError("secret_unavailable")
    return value


def _stage_compute_recipe(state_dir: Path) -> tuple[Path, str]:
    from scientist.compute_runtime import recipe_manifest_sha256

    source_root = Path(__file__).resolve().parents[3]
    sources = {
        "csv_describe.py": source_root / "runtime" / "compute_entrypoint.py",
        "cpu_recipes.py": source_root / "backend" / "src" / "scientist" / "cpu_recipes.py",
        "scientific_render.py": source_root / "backend" / "src" / "scientist" / "scientific_render.py",
    }
    root = state_dir / "compute-recipes"
    root.mkdir(mode=0o700, exist_ok=True)
    try:
        _private_dir(root)
    except ValueError as exc:
        raise HostError("compute_recipe_root") from exc
    scratch = Path(tempfile.mkdtemp(prefix=".recipe-", dir=root))
    try:
        for name, source in sources.items():
            if not source.is_file() or source.is_symlink():
                raise HostError("compute_recipe_unavailable")
            destination = scratch / name
            shutil.copyfile(source, destination)
            destination.chmod(0o444)
        scratch.chmod(0o555)
        digest = recipe_manifest_sha256(scratch)
        destination = root / digest
        if destination.exists():
            if recipe_manifest_sha256(destination) != digest:
                raise HostError("compute_recipe_identity")
            scratch.chmod(0o700)
            shutil.rmtree(scratch)
            return destination, digest
        os.replace(scratch, destination)
        return destination, digest
    except BaseException:
        try:
            scratch.chmod(0o700)
            for item in scratch.iterdir():
                item.chmod(0o600)
            shutil.rmtree(scratch)
        except OSError:
            pass
        raise


def _dispatch_template_payload(
    cfg: HostConfig, worker_image_digest: str, compute_image_digest: str | None
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "runtime_commit": RUNTIME_COMMIT,
        "image_digest": worker_image_digest,
        "compute_image_digest": compute_image_digest,
        "skills_digest": cfg.skills_digest,
        "environment_digest": cfg.environment_digest,
        "bucket": cfg.bucket,
        "provider_destinations": cfg.provider_destinations,
        "peer_destinations": cfg.peer_destinations,
        "secret_files": {name: name for name in sorted(_SECRET_NAMES)},
    }


def compose(cfg: HostConfig, *, engine=None, s3=None):
    global _compute_engine, _compute_image_digest, _compute_recipe_directory, _compute_recipe_manifest, _compute_state_dir
    """Verify every prerequisite, then install the trusted runtime. Mutates globals only after all checks pass."""
    import boto3
    from botocore.config import Config
    engine = engine or DockerWorkerEngine()
    try:
        if engine.engine_id() != cfg.expected_engine_id:
            raise HostError("engine_identity")
        capability_key = _secret(cfg, "broker_capability_key")
        if s3 is None:
            s3 = boto3.client(
                "s3", endpoint_url=cfg.s3_endpoint, region_name="us-east-1",
                config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 2}),
                aws_access_key_id=_secret(cfg, "s3_access_key").decode(),
                aws_secret_access_key=_secret(cfg, "s3_secret_key").decode())
        s3.head_bucket(Bucket=cfg.bucket)
    except HostError:
        raise
    except Exception:
        raise HostError("dependency_unavailable") from None
    pin = _IMAGE.fullmatch(cfg.worker_image).group(1)
    compute_pin = _IMAGE.fullmatch(cfg.compute_image).group(1) if cfg.compute_image else None
    _compute_engine = None
    _compute_image_digest = None
    _compute_recipe_directory = None
    _compute_recipe_manifest = None
    _compute_state_dir = cfg.state_dir
    if compute_pin is not None:
        try:
            _compute_engine = DockerWorkerEngine(context=engine.context)
            if _compute_engine.engine_id() != cfg.expected_engine_id:
                raise HostError("compute_engine_identity")
            _compute_engine._verified_image_id(cfg.compute_image, compute_pin)
            _compute_recipe_directory, _compute_recipe_manifest = _stage_compute_recipe(cfg.state_dir)
            _compute_image_digest = compute_pin
            scientific_authority.configure_compute_profile(compute_pin, _compute_recipe_manifest)
        except HostError:
            raise
        except Exception as exc:
            raise HostError("compute_environment_unavailable") from exc
    else:
        scientific_authority.configure_compute_profile(None, None)
    template = json.dumps(
        _dispatch_template_payload(cfg, pin, compute_pin), sort_keys=True
    ).encode()
    _parse_template(template)
    template_path = cfg.state_dir / "dispatch-template.json"
    scratch = cfg.state_dir / f".template-{uuid4().hex}"
    fd = os.open(scratch, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(template)
    scratch.chmod(0o444)
    os.replace(scratch, template_path)
    launcher = cfg.state_dir / "launches"
    launcher.mkdir(mode=0o700, exist_ok=True)
    try:
        _private_dir(launcher)
    except ValueError:
        raise HostError("launcher_dir") from None
    dispatch = DockerDispatchRuntime(DispatchServiceConfig(
        image=cfg.dispatch_image, image_digest=_IMAGE.fullmatch(cfg.dispatch_image).group(1),
        service_network=cfg.service_network, egress_network=cfg.egress_network, config_path="/run/scientist/dispatch/config.json",
        secrets_dir="/run/scientist/secrets", host_config_file=str(template_path),
        host_secrets_dir=str(cfg.secrets_dir), launcher_dir=str(launcher)), engine=engine)

    objects.configure(s3, bucket=cfg.bucket)
    broker.configure(
        capability_key=capability_key,
        provider_destinations=dict(cfg.provider_destinations),
        peer_destinations=dict(cfg.peer_destinations),
    )
    scientific_authority.configure_bundle(cfg.scientific_bundle_dir)
    from scientist.profile_evidence import accepted_image_builder
    from scientist.profile_preparation import BuildFailure
    expected_images = {"prof.worker-base@py3.14.7": pin}
    receipt_dirs = {"prof.worker-base@py3.14.7": cfg.state_dir / "profiles"}
    if compute_pin is not None:
        expected_images["prof.csv-stdlib@py3.14.7"] = compute_pin
        receipt_dirs["prof.csv-stdlib@py3.14.7"] = cfg.state_dir / "compute-profiles"
    accepted = accepted_image_builder(receipt_dirs, key=capability_key, expected_image_digests=expected_images)
    def prepare_profile(profile, job_id):
        # Recheck physical availability in the same owned engine before reusing accepted evidence.
        try:
            profile_engine = _compute_engine if profile.profile_id == "prof.csv-stdlib@py3.14.7" else engine
            profile_image = cfg.compute_image if profile.profile_id == "prof.csv-stdlib@py3.14.7" else cfg.worker_image
            expected = expected_images[profile.profile_id]
            if profile_engine is None or profile_engine.engine_id() != cfg.expected_engine_id:
                raise BuildFailure('environment_unavailable')
            profile_engine._verified_image_id(profile_image, expected)
        except Exception as exc:
            raise BuildFailure('environment_unavailable') from exc
        return accepted(profile, job_id)
    profile_preparation.configure_builder(
        prepare_profile, expected_image_digests=expected_images, evidence_key=capability_key
    )
    os.environ["SCIENTIST_MASTER_KEY_FILE"] = str(cfg.secrets_dir / "master_key")
    global _destinations
    _destinations = dict(cfg.provider_destinations)
    supervisor.configure(
        image=cfg.worker_image, image_digest=pin, broker_url=f"http://127.0.0.1:{_BROKER_PORT}",
        broker_ip="127.0.0.1",  # placeholder: start() uses the run network's own broker IP
        broker_port=_BROKER_PORT, runtime_commit=RUNTIME_COMMIT, skills_digest=cfg.skills_digest,
        environment_digest=cfg.environment_digest, bootstrap_factory=bootstrap,
        capability_factory=lambda db, run, generation: broker.issue_capability(db, run, generation, 300),
        dispatch=dispatch, engine=engine, compute_engine=_compute_engine,
        compute_image_digest=_compute_image_digest)
    return engine


def bootstrap(db, run_id: UUID, generation: int) -> WorkerBootstrap:
    """Fresh context only when the run has no checkpoint AND no operation; otherwise fail-closed continuation."""
    row = db.execute(text("""
        SELECT r.project_id, r.revision, r.plan_digest, s.digest AS snapshot, s.manifest, p.plan,
               (SELECT count(*) FROM checkpoints WHERE run_id = r.id) + (SELECT count(*) FROM operations WHERE run_id = r.id) AS prior
        FROM runs r JOIN input_snapshots s ON s.run_id = r.id AND s.project_id = r.project_id
        JOIN plan_revisions p ON p.run_id = r.id AND p.revision = r.revision
        WHERE r.id = :run AND r.generation = :generation
    """), {"run": run_id, "generation": generation}).mappings().one()
    plan = PlanSpec.model_validate(row["plan"])
    endpoint = _destinations.get(str(plan.provider_id))
    if endpoint is None:
        raise RuntimeError("provider destination is not configured")
    cfg = supervisor._require_config()
    pins = {"image_digest": cfg.image_digest, "skills_digest": cfg.skills_digest, "environment_digest": cfg.environment_digest}
    trusted_runtime_pins = RuntimePins(runtime_commit=cfg.runtime_commit, **pins)
    if plan.scientific is not None:
        scientific_authority.validate_runtime_binding(
            db, run_id, plan.scientific,
            expected_image_digest=cfg.image_digest,
            trusted_runtime_pins=trusted_runtime_pins,
        )
    if row["prior"]:
        controller = WorkerController(pins=RuntimePins(runtime_commit=cfg.runtime_commit, **pins),
                                     provider_destinations={plan.provider_id: endpoint},
        scientific_validator=lambda database, run, binding:
        scientific_authority.validate_runtime_binding(
            database, run, binding,
            expected_image_digest=cfg.image_digest,
            trusted_runtime_pins=trusted_runtime_pins))
        return supervisor.continuation_bootstrap(db, run_id, generation, controller)
    stamp = time.time()
    context = RuntimeContextV1.model_validate({
        "schema_version": 1, "run_id": str(run_id), "project_id": str(row["project_id"]), "generation": generation,
        "revision": row["revision"], "input_snapshot_digest": row["snapshot"].strip(),
        "plan_digest": row["plan_digest"].strip(), "runtime_commit": cfg.runtime_commit, **pins,
        "provider_id": str(plan.provider_id), "provider_endpoint": endpoint, "model": plan.model,
        "plan": plan.model_dump(mode="json"), "turn_id": str(uuid4()), "system_prompt": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": row["manifest"]["question"]}],
        "native_message_metadata": [{"message_index": 0, "timestamp": stamp}],
        "current_turn_user_index": 0, "native_turn_timestamp": stamp,
        "todo": {"todos": [], "revision": 0}, "compacted_context": None, "boundary": "before_model",
        "pending_assistant": None, "operation_mappings": [], "operation_sequence": 0, "workspace_manifest": []})
    return WorkerBootstrap(context=context.model_dump_json().encode(), workspace=[],
                           metadata=BootstrapMetadata(schema_version=1, checkpoint_revision=0))


# --- host-owned CSV compute ------------------------------------------------------------------

def _compute_authority(db, request: OperationRequest):
    """Load and revalidate the current approved binding from PostgreSQL."""
    row = db.execute(text("""
        SELECT r.project_id, r.revision, r.generation, r.state,
               s.digest AS snapshot_digest, p.plan,
               o.state AS operation_state, o.generation AS operation_generation,
               o.payload_hash, o.result AS operation_result
        FROM runs r
        JOIN input_snapshots s ON s.run_id=r.id AND s.project_id=r.project_id
        JOIN plan_revisions p ON p.run_id=r.id AND p.revision=r.revision
        JOIN operations o ON o.run_id=r.id AND o.operation_id=:operation
        WHERE r.id=:run
    """), {"run": request.run_id, "operation": request.operation_id}).mappings().one_or_none()
    if row is None or (
        row["state"] != "running" or row["generation"] != request.generation
        or row["operation_state"] != "reserved"
        or row["operation_generation"] != request.generation
        or row["payload_hash"].strip() != broker._fingerprint(request)
    ):
        raise RuntimeError("compute authorization is no longer current")
    try:
        persisted_request = row["operation_result"]["request"]
    except (KeyError, TypeError):
        raise RuntimeError("compute request journal is invalid") from None
    if persisted_request != request.model_dump(mode="json"):
        raise RuntimeError("compute request differs from journal")

    plan = PlanSpec.model_validate(row["plan"])
    binding = plan.scientific
    if not isinstance(binding, ScientificBindingV2):
        raise RuntimeError("compute request has no approved V2 binding")
    grant_id = request.payload.get("grant_id")
    grant = binding.csv_describe_grants.get(grant_id) if isinstance(grant_id, str) else None
    profile_pin, approved_recipe_manifest = scientific_authority.trusted_compute_profile()
    if (
        not isinstance(grant, CsvDescribeGrantV1)
        or request.payload != {"grant_id": grant_id, "grant": grant.model_dump(mode="json")}
        or grant.profile_id != profile_pin.profile_id
        or grant.profile_version != profile_pin.version
        or grant.image_digest != profile_pin.image_digest
        or grant.recipe_manifest_sha256 != approved_recipe_manifest
        or grant.recipe_manifest_sha256 != _compute_recipe_manifest
    ):
        raise RuntimeError("compute request differs from approved grant")

    cfg = supervisor._require_config()
    pins = scientific_authority._current_runtime_pins()
    scientific_authority.validate_runtime_binding(
        db, request.run_id, binding,
        expected_image_digest=cfg.image_digest,
        trusted_runtime_pins=pins,
    )
    return row, binding, grant_id, grant


def _mark_compute_executor_unknown(db, ref) -> None:
    db.execute(text("""
        UPDATE runtime_executors SET state='unknown', updated_at=now()
        WHERE id=:id AND run_id=:run AND generation=:generation AND kind='compute'
          AND compute_operation_id=:operation AND process_incarnation=:incarnation
          AND (container_id IS NULL OR container_id=:container)
          AND state IN ('starting','active','unknown')
    """), {
        "id": ref.executor_id, "run": ref.run_id, "generation": ref.generation,
        "operation": ref.operation_id, "incarnation": ref.process_incarnation,
        "container": ref.container_id,
    })
    db.commit()


def _write_private_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _stage_compute_outputs(request, project_id, binding, grant_id, grant, output_bytes):
    """Commit bounded verified guest bytes before the first object-store write."""
    from scientist.contracts import ObjectRef
    from scientist import compute_runtime

    expected_names = tuple(compute_runtime._OUTPUT_TYPES)
    if set(output_bytes) != set(expected_names):
        raise RuntimeError("compute result file set differs from the approved recipe")
    total = sum(len(output_bytes[name]) for name in expected_names)
    if total > MAX_COMPUTE_OUTPUT_BYTES:
        raise RuntimeError("compute output exceeds approved byte limit")
    entries = []
    for index, name in enumerate(expected_names):
        content_type = compute_runtime._OUTPUT_TYPES[name]
        data = output_bytes[name]
        digest = hashlib.sha256(data).hexdigest()
        output_ref = ObjectRef(
            project_id=project_id, key=f"{project_id}/{digest}", sha256=digest,
            size=len(data), content_type="application/octet-stream",
        )
        entries.append(ComputeOutputEntryV2(
            index=index, name=name, content_type=content_type, object_ref=output_ref,
            data_base64=base64.b64encode(data).decode("ascii"),
        ))
    envelope = ComputeResultEnvelopeV2(
        schema_version=2,
        binding_sha256=hashlib.sha256(canonical_bytes(binding.model_dump(mode="json"))).hexdigest(),
        grant_id=grant_id,
        operation_id=request.operation_id,
        operation_fingerprint=operation_fingerprint(request),
        recipe_id=grant.recipe_id,
        recipe_version=grant.recipe_version,
        recipe_manifest_sha256=grant.recipe_manifest_sha256,
        profile_id=grant.profile_id,
        profile_version=grant.profile_version,
        image_digest=grant.image_digest,
        input_manifest_sha256=compute_input_manifest_sha256(grant),
        input_ref=grant.input_ref,
        input_sha256=grant.input_sha256,
        outputs=entries,
    )
    envelope_bytes = canonical_bytes(envelope.model_dump(mode="json"))
    stage = {
        "schema_version": 1,
        "operation_id": request.operation_id,
        "operation_fingerprint": operation_fingerprint(request),
        "binding_sha256": envelope.binding_sha256,
        "output_total_bytes": total,
        "envelope_sha256": hashlib.sha256(envelope_bytes).hexdigest(),
        "envelope_base64": base64.b64encode(envelope_bytes).decode("ascii"),
    }
    if len(canonical_bytes(stage)) > _MAX_COMPUTE_STAGE_BYTES:
        raise RuntimeError("durable compute stage exceeds its bounded journal limit")
    with database.session() as db:
        row = db.execute(text("""
            SELECT id, result FROM operations
            WHERE run_id=:run AND operation_id=:operation AND state='reserved' FOR UPDATE
        """), {"run": request.run_id, "operation": request.operation_id}).mappings().one_or_none()
        if row is None:
            raise RuntimeError("compute operation is no longer reserved")
        pending = dict(row["result"] or {})
        existing = pending.get("compute_stage")
        if existing is not None and existing != stage:
            raise RuntimeError("compute operation already has different staged output")
        pending["compute_stage"] = stage
        db.execute(text("""
            UPDATE operations SET result=CAST(:result AS jsonb)
            WHERE id=:id AND state='reserved'
        """), {"result": json.dumps(pending, separators=(",", ":")), "id": row["id"]})
        db.commit()
    return pending


def _resume_compute_stage(request, project_id, binding, grant_id, grant, operation_result) -> bool:
    """Idempotently store/read back staged bytes, stop exact guest, then publish."""
    from scientist.contracts import ObjectRef
    from scientist import compute_runtime

    stage = operation_result.get("compute_stage") if isinstance(operation_result, dict) else None
    if not isinstance(stage, dict):
        return False
    if len(canonical_bytes(stage)) > _MAX_COMPUTE_STAGE_BYTES:
        raise RuntimeError("durable compute stage exceeds its bounded journal limit")
    raw = base64.b64decode(stage.get("envelope_base64", ""), validate=True)
    if (
        stage.get("schema_version") != 1
        or stage.get("operation_id") != request.operation_id
        or stage.get("operation_fingerprint") != operation_fingerprint(request)
        or len(raw) > MAX_COMPUTE_ENVELOPE_BYTES
        or hashlib.sha256(raw).hexdigest() != stage.get("envelope_sha256")
    ):
        raise RuntimeError("staged compute bytes have invalid journal identity")
    envelope = ComputeResultEnvelopeV2.model_validate_json(raw)
    binding_sha = hashlib.sha256(canonical_bytes(binding.model_dump(mode="json"))).hexdigest()
    if (
        raw != canonical_bytes(envelope.model_dump(mode="json"))
        or stage.get("binding_sha256") != binding_sha
        or envelope.binding_sha256 != binding_sha
        or any(entry.object_ref.project_id != project_id for entry in envelope.outputs)
        or envelope.grant_id != grant_id
        or envelope.operation_id != request.operation_id
        or envelope.operation_fingerprint != operation_fingerprint(request)
        or envelope.recipe_manifest_sha256 != grant.recipe_manifest_sha256
        or envelope.profile_id != grant.profile_id
        or envelope.profile_version != grant.profile_version
        or envelope.image_digest != grant.image_digest
        or envelope.input_manifest_sha256 != compute_input_manifest_sha256(grant)
        or envelope.input_ref != grant.input_ref
        or envelope.input_sha256 != grant.input_sha256
    ):
        raise RuntimeError("staged compute envelope differs from current authority")
    total = sum(entry.object_ref.size for entry in envelope.outputs)
    if total != stage.get("output_total_bytes") or total > MAX_COMPUTE_OUTPUT_BYTES:
        raise RuntimeError("staged compute output limit differs from its journal")

    for entry in envelope.outputs:
        data = base64.b64decode(entry.data_base64, validate=True)
        if (
            len(data) != entry.object_ref.size
            or hashlib.sha256(data).hexdigest() != entry.object_ref.sha256.lower()
        ):
            raise RuntimeError("staged compute output failed hash verification")
        with database.session() as db:
            stored_ref = objects.put(db, project_id, BytesIO(data), "application/octet-stream")
            if stored_ref != entry.object_ref:
                raise RuntimeError("compute output object identity changed")
            db.commit()
        with objects.open_verified(entry.object_ref) as stream:
            if stream.read(MAX_COMPUTE_OUTPUT_BYTES + 1) != data:
                raise RuntimeError("compute output object readback differs")

    envelope_digest = hashlib.sha256(raw).hexdigest()
    envelope_ref = ObjectRef(
        project_id=project_id, key=f"{project_id}/{envelope_digest}",
        sha256=envelope_digest, size=len(raw), content_type="application/octet-stream",
    )
    with database.session() as db:
        stored_ref = objects.put(db, project_id, BytesIO(raw), "application/octet-stream")
        if stored_ref != envelope_ref:
            raise RuntimeError("compute envelope object identity changed")
        db.commit()
    with objects.open_verified(envelope_ref) as stream:
        if stream.read(2 * 1024 * 1024 + 1) != raw:
            raise RuntimeError("compute envelope object readback differs")

    with database.session() as db:
        journal = db.execute(text("""
            SELECT id, result FROM operations
            WHERE run_id=:run AND operation_id=:operation AND state='reserved' FOR UPDATE
        """), {"run": request.run_id, "operation": request.operation_id}).mappings().one_or_none()
        if journal is None:
            raise RuntimeError("compute operation is no longer reserved")
        pending = dict(journal["result"] or {})
        pending["compute_envelope_ref"] = envelope_ref.model_dump(mode="json")
        db.execute(text("""
            UPDATE operations SET result=CAST(:result AS jsonb)
            WHERE id=:id AND state='reserved'
        """), {"result": json.dumps(pending, separators=(",", ":")), "id": journal["id"]})
        db.commit()

    with database.session() as db:
        row = db.execute(text("""
            SELECT id, generation, process_incarnation, engine_id, container_id, state, proof
            FROM runtime_executors
            WHERE run_id=:run AND kind='compute' AND compute_operation_id=:operation
            ORDER BY created_at DESC LIMIT 1 FOR UPDATE
        """), {"run": request.run_id, "operation": request.operation_id}).mappings().one_or_none()
        if row is None or row["container_id"] is None or row["engine_id"] is None:
            return False
        proof = row["proof"] or {}
        stopped = (
            row["state"] == "inactive"
            and proof.get("source") == "owned-engine-exact-container"
            and proof.get("engine_id") == row["engine_id"]
            and proof.get("container_id") == row["container_id"]
        )
        ref = supervisor.ExecutorRef(
            executor_id=row["id"], run_id=request.run_id, generation=row["generation"],
            kind="compute", operation_id=request.operation_id,
            process_incarnation=row["process_incarnation"], engine_id=row["engine_id"],
            container_id=row["container_id"],
        )
    if not stopped:
        found = compute_runtime.find_compute(
            _compute_engine, ref, expected_image_digest=_compute_image_digest
        )
        if found is None:
            return False
        compute_runtime.stop_compute(
            _compute_engine, found, expected_image_digest=_compute_image_digest
        )
        with database.session() as db:
            supervisor.mark_compute_executor_inactive(db, found)
            db.commit()
    with database.session() as db:
        row, _binding, _grant_id, _grant = _compute_authority(db, request)
        broker.record_verified_completion(
            db, request, row["revision"], usage_tokens=0, result_ref=envelope_ref
        )
    return True


def _run_compute_operation(request: OperationRequest) -> None:
    """Execute one approved grant and journal a verified canonical envelope."""
    from scientist import compute_runtime

    if (
        request.kind != "compute" or _compute_engine is None
        or _compute_image_digest is None or _compute_recipe_directory is None
        or _compute_recipe_manifest is None or _compute_state_dir is None
    ):
        raise RuntimeError("compute environment unavailable")

    # Approval, generation, grant, and payload are reloaded from trusted storage.
    with database.session() as db:
        row, binding, grant_id, grant = _compute_authority(db, request)
        project_id = row["project_id"]
        binding_sha = hashlib.sha256(canonical_bytes(binding.model_dump(mode="json"))).hexdigest()
        input_manifest_sha = compute_input_manifest_sha256(grant)
        fingerprint = operation_fingerprint(request)
        operation_result = row["operation_result"]

    if _resume_compute_stage(request, project_id, binding, grant_id, grant, operation_result):
        return

    staging_root = _compute_state_dir / "compute-jobs"
    staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private_dir(staging_root)
    with tempfile.TemporaryDirectory(prefix="job-", dir=staging_root) as temp_name:
        scratch = Path(temp_name)
        scratch.chmod(0o700)
        input_dir, output_dir = scratch / "inputs", scratch / "outputs"
        input_dir.mkdir(mode=0o700)
        output_dir.mkdir(mode=0o700)
        with objects.open_verified(grant.input_ref) as stream:
            csv_bytes = stream.read(grant.max_input_bytes + 1)
        if len(csv_bytes) != grant.input_ref.size or hashlib.sha256(csv_bytes).hexdigest() != grant.input_sha256:
            raise RuntimeError("approved compute input integrity mismatch")
        _write_private_file(input_dir / "data.csv", csv_bytes)
        _write_private_file(input_dir / "params.json", canonical_bytes({"numeric_columns": grant.numeric_columns}))
        spec = ComputeLaunchSpec(
            profile_id=grant.profile_id, profile_version=grant.profile_version,
            image_digest=grant.image_digest,
            recipe_manifest_sha256=grant.recipe_manifest_sha256,
            recipe_directory=_compute_recipe_directory,
            input_directory=input_dir, output_directory=output_dir,
        )

        with database.session() as db:
            _compute_authority(db, request)
            ref, is_new = supervisor.reserve_compute_executor(
                db, request, engine_id=_compute_engine.engine_id()
            )
            if is_new:
                # Persist the attempt before Docker create. A retry can find by
                # labels, but cannot issue a second create for this operation.
                db.execute(text("""
                    UPDATE runtime_executors SET proof=CAST(:proof AS jsonb), updated_at=now()
                    WHERE id=:id AND run_id=:run AND generation=:generation
                      AND kind='compute' AND compute_operation_id=:operation AND state='starting'
                """), {
                    "proof": json.dumps({"source": "compute-create-attempted", "engine_id": ref.engine_id}),
                    "id": ref.executor_id, "run": ref.run_id, "generation": ref.generation,
                    "operation": ref.operation_id,
                })
                db.commit()

        if is_new:
            try:
                ref = compute_runtime.create_compute(_compute_engine, ref, spec)
                with database.session() as db:
                    supervisor.bind_compute_executor(db, ref)
                    db.commit()
            except Exception:
                with database.session() as db:
                    _mark_compute_executor_unknown(db, ref)
                return
        else:
            try:
                recovered = compute_runtime.find_compute(
                    _compute_engine, ref, expected_image_digest=_compute_image_digest
                )
            except Exception:
                with database.session() as db:
                    _mark_compute_executor_unknown(db, ref)
                return
            if recovered is None:
                with database.session() as db:
                    _mark_compute_executor_unknown(db, ref)
                return
            ref = recovered
            if ref.container_id is not None:
                with database.session() as db:
                    stored = db.execute(text("""
                        SELECT * FROM runtime_executors
                        WHERE id=:id AND run_id=:run AND generation=:generation
                          AND kind='compute' AND compute_operation_id=:operation FOR UPDATE
                    """), {
                        "id": ref.executor_id, "run": ref.run_id,
                        "generation": ref.generation, "operation": ref.operation_id,
                    }).mappings().one_or_none()
                    if stored is None:
                        raise RuntimeError("compute launch intent disappeared")
                    if (stored["process_incarnation"] != ref.process_incarnation
                            or stored["engine_id"] != ref.engine_id):
                        raise RuntimeError("compute launch identity changed")
                    if stored["state"] == "unknown" and not supervisor._requalify_unknown_executor(
                        db, stored, ref
                    ):
                        raise RuntimeError("compute launch intent could not be requalified")
                    if stored["state"] == "unknown" and stored["container_id"] is not None:
                        # Bound unknown intents need the requalification committed even
                        # when no CID bind follows; otherwise the next start CAS sees unknown.
                        db.commit()
                    if stored["container_id"] is None:
                        if stored["state"] not in {"starting", "unknown"}:
                            raise RuntimeError("compute launch intent is not bindable")
                        supervisor.bind_compute_executor(db, ref)
                        db.commit()
                    elif stored["container_id"] != ref.container_id:
                        raise RuntimeError("compute launch resolved to a different container")

        try:
            _labels, state = compute_runtime._verify_container(
                _compute_engine, ref, expected_image_digest=_compute_image_digest
            )
            if state.get("Status") == "created" and state.get("Running") is not True:
                with database.session() as db:
                    _compute_authority(db, request)
                    supervisor.start_compute_executor(
                        db, ref, request,
                        lambda: compute_runtime.start_compute(
                            _compute_engine, ref, expected_image_digest=_compute_image_digest
                        ),
                    )

            exit_code = None
            deadline = time.monotonic() + 32
            while time.monotonic() < deadline:
                exit_code = compute_runtime.poll_compute(
                    _compute_engine, ref, expected_image_digest=_compute_image_digest
                )
                if exit_code is not None:
                    break
                time.sleep(0.2)
            if exit_code != 0:
                raise RuntimeError("compute did not produce verified outputs")
            output_bytes = compute_runtime.read_compute_outputs(
                _compute_engine, ref, expected_image_digest=_compute_image_digest
            )
            staged_result = _stage_compute_outputs(
                request, project_id, binding, grant_id, grant, output_bytes
            )
            if not _resume_compute_stage(
                request, project_id, binding, grant_id, grant, staged_result
            ):
                return
        except Exception as exc:
            with database.session() as db:
                journal = db.execute(text("""
                    SELECT result FROM operations
                    WHERE run_id=:run AND operation_id=:operation AND state='reserved'
                """), {"run": request.run_id, "operation": request.operation_id}).mappings().one_or_none()
                if journal is not None and isinstance(journal["result"], dict) and journal["result"].get("compute_stage"):
                    # Verified bytes are durable. Retry content-addressed writes without changing the outcome.
                    return
                try:
                    compute_runtime.stop_compute(
                        _compute_engine, ref, expected_image_digest=_compute_image_digest
                    )
                    supervisor.mark_compute_executor_inactive(db, ref)
                    db.commit()
                except Exception:
                    db.rollback()
                    _mark_compute_executor_unknown(db, ref)
            with database.session() as db:
                _mark_compute_executor_unknown(db, ref)
                try:
                    row, _binding, _grant_id, _grant = _compute_authority(db, request)
                except Exception:
                    return
                broker._record_unknown(db, request, row["revision"], usage_tokens=None, result_ref=None)


# --- supervision loop ------------------------------------------------------------------------

class Host:
    """Serial supervision: reap dead runs, claim approved runs, start them. Never touches waiting/terminal runs."""

    def __init__(self, engine, max_active: int = 3, poll_seconds: float = 2.0, lock=None, on_lost=None):
        self._compute_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scientist-compute")
        self._compute_futures: dict[tuple[UUID, str], Future] = {}
        self.engine, self.max_active, self.poll_seconds = engine, max_active, poll_seconds
        self.lock, self.on_lost, self.lost = lock, on_lost, False
        # ponytail: start-failure counter is in-memory; bounded at 3 generations per run per host start;
        # make durable via worker-launch-not-attempted executors if restarts become frequent.
        self.failures: dict[UUID, int] = {}

    def _run_compute_operation(self, request: OperationRequest) -> None:
        _run_compute_operation(request)

    def _compute_tick(self) -> None:
        for key, future in tuple(self._compute_futures.items()):
            if future.done():
                try:
                    future.result()
                except Exception as exc:
                    _log("host.error", "compute", key[0], type(exc).__name__)
                self._compute_futures.pop(key, None)
        if _compute_engine is None or _compute_image_digest is None:
            return
        try:
            with database.session() as db:
                rows = db.execute(text("""
                    SELECT o.run_id, o.operation_id, o.generation, o.result
                    FROM operations o JOIN runs r ON r.id=o.run_id
                    WHERE o.kind='compute' AND o.state='reserved'
                      AND r.state='running' AND r.generation=o.generation
                    ORDER BY o.created_at, o.operation_id
                """)).mappings().all()
            for row in rows:
                key = (row["run_id"], row["operation_id"])
                if key in self._compute_futures:
                    continue
                try:
                    request = OperationRequest.model_validate(row["result"]["request"])
                    if request.run_id != key[0] or request.operation_id != key[1] or request.generation != row["generation"]:
                        raise ValueError("compute journal identity changed")
                except (KeyError, TypeError, ValueError) as exc:
                    _log("host.error", "compute_request", key[0], type(exc).__name__)
                    continue
                self._compute_futures[key] = self._compute_pool.submit(self._run_compute_operation, request)
        except Exception as exc:
            _log("host.error", "compute_poll", None, type(exc).__name__)

    def close(self) -> None:
        self._compute_pool.shutdown(wait=True, cancel_futures=True)

    def _recover(self, run_id: UUID, stage: str, expect: tuple[str, int]) -> None:
        """Recover only if the run is still in the state and generation the caller observed (checked under the claim lock)."""
        try:
            with database.session() as db:
                try:
                    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('scientist.supervisor.claim'))"))
                    now = db.execute(text("SELECT state, generation FROM runs WHERE id = :r FOR UPDATE"), {"r": run_id}).one_or_none()
                    if now is None or (now.state, now.generation) != expect:
                        db.rollback()  # a broker, REST stop or owner action got there first
                        return
                    supervisor.recover(db, run_id)
                except Exception:
                    db.rollback()
                    raise
        except Exception as exc:
            _log("host.error", stage, run_id, type(exc).__name__)

    def startup_recover(self) -> None:
        self._reconcile_startup_compute()
        with database.session() as db:
            runs = db.execute(text("SELECT id, state, generation FROM runs WHERE state IN ('running','recovering','stopping') ORDER BY id")).all()
        for run in runs:
            self._recover(run.id, "startup_recover", (run.state, run.generation))
        self._peer_reconciliation_tick(startup=True)

    def _reconcile_startup_compute(self) -> None:
        """Read live tmpfs output before ordinary recovery stops its guest."""
        try:
            with database.session() as db:
                rows = db.execute(text("""
                    SELECT DISTINCT o.run_id, o.operation_id, o.generation, o.result
                    FROM operations o
                    JOIN runs r ON r.id=o.run_id AND r.generation=o.generation
                    JOIN runtime_executors e ON e.run_id=o.run_id
                      AND e.generation=o.generation AND e.kind='compute'
                      AND e.compute_operation_id=o.operation_id
                    WHERE o.kind='compute' AND o.state='reserved' AND r.state='running'
                    ORDER BY o.run_id, o.operation_id
                """)).mappings().all()
            for row in rows:
                try:
                    request = OperationRequest.model_validate(row["result"]["request"])
                    if (request.run_id, request.operation_id, request.generation) == (
                        row["run_id"], row["operation_id"], row["generation"]
                    ):
                        self._run_compute_operation(request)
                except Exception as exc:
                    _log("host.error", "compute_startup_recovery", row["run_id"], type(exc).__name__)
        except Exception as exc:
            _log("host.error", "compute_startup_recovery", None, type(exc).__name__)

    def _peer_reconciliation_tick(self, *, startup: bool = False) -> None:
        try:
            with database.session() as db:
                peer_reconciliation_supervisor.tick(
                    db, max_active=self.max_active, startup=startup
                )
        except Exception as exc:
            _log("host.error", "peer_reconciliation", None, type(exc).__name__)

    def reap(self) -> None:
        with database.session() as db:
            runs = db.execute(text("""
                SELECT r.id, r.state, r.generation, (r.lease_expires_at IS NULL OR r.lease_expires_at <= now()) AS expired,
                       (SELECT e.container_id FROM runtime_executors e WHERE e.run_id = r.id AND e.generation = r.generation
                          AND e.kind = 'worker' AND e.state = 'active') AS container
                FROM runs r WHERE r.state = 'running' OR (r.state = 'stopping' AND r.cancel_requested) ORDER BY r.id""")).all()
        if not runs:
            self._peer_reconciliation_tick()
            return
        try:
            alive = self.engine.running_worker_containers()
        except Exception as exc:
            alive = None  # liveness unknown: a valid lease is never guessed dead
            _log("host.error", "list_workers", None, type(exc).__name__)
        for run in runs:
            # 'stopping' is a stop that died after committing: recover fences and cancels it.
            if run.state == "stopping" or run.expired or (alive is not None and run.container not in alive):
                self._recover(run.id, "reap", (run.state, run.generation))
        self._peer_reconciliation_tick()

    def _queued(self) -> int:
        with database.session() as db:
            return db.execute(text("SELECT count(*) FROM runs WHERE state = 'queued'")).scalar_one()

    def claim_and_start(self) -> None:
        while True:
            before = self._queued()
            try:
                with database.session() as db:
                    try:
                        claimed = supervisor.claim(db, self.max_active)
                    except Exception:
                        db.rollback()
                        raise
            except Exception as exc:
                _log("host.error", "claim", None, type(exc).__name__)
                return
            if claimed is None:
                if self._queued() != before:
                    continue  # claim just parked a budget-wait run; look at the next one now
                return
            run_id, generation = claimed
            try:
                with database.session() as db:
                    try:
                        supervisor.start(db, run_id, generation)
                    except Exception:
                        db.rollback()
                        raise
            except Exception as exc:
                _log("host.error", "start", run_id, type(exc).__name__)
                self._start_failed(run_id)
                return  # retry on the next poll, not in a hot loop
            self.failures.pop(run_id, None)

    def _start_failed(self, run_id: UUID) -> None:
        self.failures[run_id] = self.failures.get(run_id, 0) + 1
        if self.failures[run_id] < _START_FAILURES:
            return
        self.failures.pop(run_id)
        try:
            with database.session() as db:
                row = db.execute(text("""UPDATE runs SET state='waiting_input', waiting_reason='runtime_start_failed'
                                         WHERE id=:r AND state='queued' AND cancel_requested=false RETURNING revision"""),
                                 {"r": run_id}).one_or_none()
                if row is not None:
                    _event(db, run_id, row.revision, "run.state", {"state": "waiting_input"})
                db.commit()
        except Exception as exc:
            _log("host.error", "park", run_id, type(exc).__name__)

    def _lock_alive(self) -> bool:
        if self.lock is None:
            return True
        try:
            self.lock.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            self.lost = True
            _log("host.lock_lost", "ping", None, type(exc).__name__)
            if self.on_lost:
                self.on_lost()
            return False

    def tick(self) -> None:
        if self.lost or not self._lock_alive():
            return  # never act without the singleton lock
        for step in (self._compute_tick, self.reap, self.prepare_environments, self.claim_and_start):
            try:
                step()
            except Exception as exc:
                _log("host.error", step.__name__, None, type(exc).__name__)

    def prepare_environments(self) -> None:
        with database.session() as db:
            profile_preparation.process_next_job(db)

    def run_loop(self, stop: threading.Event) -> None:
        while not stop.wait(self.poll_seconds) and not self.lost:
            self.tick()


# --- process wiring --------------------------------------------------------------------------

def acquire_singleton():
    """Hold a session advisory lock on a dedicated connection for the life of the caller."""
    conn = create_engine(database.DATABASE_URL, poolclass=NullPool).connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        if not conn.execute(text("SELECT pg_try_advisory_lock(hashtext('scientist.host'))")).scalar_one():
            raise HostError("already_running")
    except BaseException:
        conn.close()
        raise
    return conn


def publish_bootstrap(state_dir: Path, port: int, token: str) -> Path:
    path = state_dir / "owner-bootstrap.url"
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(f"http://127.0.0.1:{port}/#bootstrap={token}\n")
    _log("host.ready", "listen", port=port, path=str(path))
    return path


class _TypeOnly(logging.Filter):
    """Unhandled ASGI exceptions carry request data in their tracebacks; keep only the exception type."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and record.exc_info[0] is not None:
            record.msg, record.args = json.dumps({"event": "host.error", "stage": "asgi", "error_type": record.exc_info[0].__name__}), ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


_TYPE_ONLY = _TypeOnly()


def make_server(app, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, server_header=False,
                                           proxy_headers=False, log_level="warning"))
    logging.getLogger("uvicorn.error").addFilter(_TYPE_ONLY)  # after Config: it reconfigures uvicorn logging
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scientist.host")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    stage, lock, thread, stop, loop = "config", None, None, threading.Event(), None
    previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        cfg = load_config(args.config)
        os.environ["SCIENTIST_DATABASE_URL"] = cfg.database_url
        database.DATABASE_URL = cfg.database_url
        stage = "lock"
        lock = acquire_singleton()
        # uvicorn re-raises captured signals to whatever handler is installed; without ours the default SIGTERM
        # action would kill the process (also during startup recovery) before the finally block below.
        server = None
        for number in previous:
            signal.signal(number, lambda *_: (setattr(server, "should_exit", True) if server else None) or stop.set())
        stage = "migrate"
        database.migrate()
        stage = "compose"
        engine = compose(cfg)
        stage = "serve"
        token = token_urlsafe(32)
        app = create_app(bootstrap_token=token, bootstrap_expires_at=datetime.now(timezone.utc) + timedelta(minutes=30))
        web_dist_dir = getattr(cfg, "web_dist_dir", None)
        if web_dist_dir is not None:
            app.include_router(create_web_router(web_dist_dir))
        server = make_server(app, cfg.listen_port)
        stage = "bind"  # take the port before recovering or launching anything; uvicorn exits via SystemExit if taken
        try:
            sock = server.config.bind_socket()
        except SystemExit as exc:
            raise RuntimeError from exc
        stage = "serve"
        loop = Host(engine, cfg.max_active, cfg.poll_seconds, lock=lock, on_lost=lambda: setattr(server, "should_exit", True))
        loop.startup_recover()
        if stop.is_set():  # a signal arrived during startup: do not start work or advertise the URL
            return 0
        thread = threading.Thread(target=loop.run_loop, args=(stop,), name="scientist-host-loop")
        thread.start()
        publish_bootstrap(cfg.state_dir, cfg.listen_port, token)  # advertise the URL only once the port is ours
        server.run(sockets=[sock])
        return 3 if loop.lost else 0
    except Exception as exc:
        name = type(exc.__cause__).__name__ if stage == "bind" and exc.__cause__ else type(exc).__name__
        _log("host.exit", stage, None, name, **({"fields": exc.fields} if isinstance(exc, ConfigError) else {}))
        return 2
    finally:
        stop.set()
        if thread is not None:
            thread.join(120)  # workers keep running; the next startup fences or continues them
        if loop is not None:
            loop.close()
        if lock is not None:
            lock.close()
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    sys.exit(main())
