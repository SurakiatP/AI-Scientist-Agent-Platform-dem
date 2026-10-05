"""Single-process host: composes the trusted runtime and serves the owner API.

Run with `python -m scientist.host --config /abs/host.json`. Nothing here runs at import time.
Logs carry only {event, stage, run_id, error_type}; secrets and exception messages are never logged.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import stat
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import uvicorn
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool

from scientist import broker, objects, settings, supervisor
from scientist import db as database
from scientist.app import create_app
from scientist.contracts import PlanSpec
from scientist.dispatch_runtime import _SECRET_NAMES, DispatchServiceConfig, check_egress_network, DockerDispatchRuntime, _parse_template
from scientist.domain import _event
from scientist.private_worker_api import RuntimePins, WorkerController
from scientist.runtime_contracts import RUNTIME_COMMIT, BootstrapMetadata, RuntimeContextV1
from scientist.supervisor import DockerWorkerEngine, WorkerBootstrap

_MAX_CONFIG_BYTES = 64 * 1024
_START_FAILURES = 3
_BROKER_PORT = 8123  # fixed by DispatchServiceConfig
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:+-]*@(sha256:[a-f0-9]{64})")
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
    dispatch_image: str
    service_network: str
    egress_network: str | None = None
    skills_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    s3_endpoint: str
    bucket: str
    secrets_dir: Path
    state_dir: Path
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
        if not value or len(value) > 63 or not value.replace("-", "").isalnum():
            raise ValueError("invalid bucket")
        return value

    @field_validator("secrets_dir", "state_dir")
    @classmethod
    def _dir(cls, value: Path) -> Path:
        return _private_dir(value)

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


def compose(cfg: HostConfig, *, engine=None, s3=None):
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
    template = json.dumps({
        "schema_version": 1, "runtime_commit": RUNTIME_COMMIT, "image_digest": pin,
        "skills_digest": cfg.skills_digest, "environment_digest": cfg.environment_digest,
            "provider_destinations": cfg.provider_destinations, "peer_destinations": cfg.peer_destinations,
            "secret_files": {n: n for n in sorted(_SECRET_NAMES)},
    }, sort_keys=True).encode()
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
    os.environ["SCIENTIST_MASTER_KEY_FILE"] = str(cfg.secrets_dir / "master_key")
    global _destinations
    _destinations = dict(cfg.provider_destinations)
    supervisor.configure(
        image=cfg.worker_image, image_digest=pin, broker_url=f"http://127.0.0.1:{_BROKER_PORT}",
        broker_ip="127.0.0.1",  # placeholder: start() uses the run network's own broker IP
        broker_port=_BROKER_PORT, runtime_commit=RUNTIME_COMMIT, skills_digest=cfg.skills_digest,
        environment_digest=cfg.environment_digest, bootstrap_factory=bootstrap,
        capability_factory=lambda db, run, generation: broker.issue_capability(db, run, generation, 300),
        dispatch=dispatch, engine=engine)
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
    if row["prior"]:
        controller = WorkerController(pins=RuntimePins(runtime_commit=cfg.runtime_commit, **pins),
                                      provider_destinations={plan.provider_id: endpoint})
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


# --- supervision loop ------------------------------------------------------------------------

class Host:
    """Serial supervision: reap dead runs, claim approved runs, start them. Never touches waiting/terminal runs."""

    def __init__(self, engine, max_active: int = 3, poll_seconds: float = 2.0, lock=None, on_lost=None):
        self.engine, self.max_active, self.poll_seconds = engine, max_active, poll_seconds
        self.lock, self.on_lost, self.lost = lock, on_lost, False
        # ponytail: start-failure counter is in-memory; bounded at 3 generations per run per host start;
        # make durable via worker-launch-not-attempted executors if restarts become frequent.
        self.failures: dict[UUID, int] = {}

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
        with database.session() as db:
            runs = db.execute(text("SELECT id, state, generation FROM runs WHERE state IN ('running','recovering','stopping') ORDER BY id")).all()
        for run in runs:
            self._recover(run.id, "startup_recover", (run.state, run.generation))

    def reap(self) -> None:
        with database.session() as db:
            runs = db.execute(text("""
                SELECT r.id, r.state, r.generation, (r.lease_expires_at IS NULL OR r.lease_expires_at <= now()) AS expired,
                       (SELECT e.container_id FROM runtime_executors e WHERE e.run_id = r.id AND e.generation = r.generation
                          AND e.kind = 'worker' AND e.state = 'active') AS container
                FROM runs r WHERE r.state = 'running' OR (r.state = 'stopping' AND r.cancel_requested) ORDER BY r.id""")).all()
        if not runs:
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
        for step in (self.reap, self.claim_and_start):
            try:
                step()
            except Exception as exc:
                _log("host.error", step.__name__, None, type(exc).__name__)

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
        if lock is not None:
            lock.close()
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    sys.exit(main())
