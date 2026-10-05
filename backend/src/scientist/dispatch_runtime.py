"""Docker-backed private dispatch service for one fenced run generation."""
from __future__ import annotations

import ipaddress
import json
import time
import re
import os
import stat
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_serializer, field_validator
import subprocess
from sqlalchemy import text
from sqlalchemy.orm import Session

from scientist.dispatch_authority import dispatch_is_inactive
from scientist.runtime_contracts import RUNTIME_COMMIT
from scientist.supervisor import DockerWorkerEngine, ExecutorRef

_MAX_CONFIG_BYTES = 64 * 1024
_RUN_LABEL = "scientist.platform/run"
_GEN_LABEL = "scientist.platform/generation"
_EXEC_LABEL = "scientist.platform/executor"
_KIND_LABEL = "scientist.platform/kind"
_INC_LABEL = "scientist.platform/incarnation"
_SECRET_NAMES = {"database_url", "broker_capability_key", "master_key", "s3_access_key", "s3_secret_key"}

# Fixed, trusted code sent only to the verified dispatch image via `docker exec`.
# It uses raw numeric IPv4 sockets so macOS never tries to route a Colima bridge IP,
# and recomputes its absolute time budget before every blocking socket operation.
_READINESS_SOCKET_CODE = r'''def remaining():
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("readiness deadline")
    return value
def receive(sock, limit):
    sock.settimeout(remaining())
    chunk = sock.recv(limit)
    if not chunk:
        raise OSError("incomplete readiness response")
    return chunk
def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate readiness key")
        result[key] = value
    return result
def probe_socket():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(remaining())
        sock.connect((host, port))
        sock.settimeout(remaining())
        sock.sendall(b"GET /ready HTTP/1.1\r\nHost: dispatch\r\nConnection: close\r\n\r\n")
        data = bytearray()
        marker = b"\r\n\r\n"
        while marker not in data:
            if len(data) >= 8192:
                raise ValueError("oversize readiness headers")
            data.extend(receive(sock, min(2048, 8192 - len(data))))
        split = data.index(marker)
        header = bytes(data[:split]).decode("ascii", "strict").split("\r\n")
        if not header or header[0] not in ("HTTP/1.0 200 OK", "HTTP/1.1 200 OK"):
            raise ValueError("invalid readiness status")
        fields = {}
        for line in header[1:]:
            name, separator, value = line.partition(":")
            name = name.strip().lower()
            if not separator or not name or name in fields:
                raise ValueError("invalid readiness headers")
            fields[name] = value.strip()
        if "transfer-encoding" in fields or "content-length" not in fields:
            raise ValueError("unbounded readiness body")
        length_text = fields["content-length"]
        if not length_text.isascii() or not length_text.isdecimal():
            raise ValueError("invalid readiness length")
        length = int(length_text)
        if length > 4096:
            raise ValueError("oversize readiness body")
        body = bytearray(data[split + len(marker):])
        if len(body) > length:
            raise ValueError("trailing readiness bytes")
        while len(body) < length:
            body.extend(receive(sock, length - len(body)))
        value = json.loads(bytes(body), object_pairs_hook=unique_pairs)
        if type(value) is not dict or set(value) != {"ready"} or type(value["ready"]) is not bool or value["ready"] is not True:
            raise ValueError("readiness payload mismatch")
'''
_READINESS_PROBE = r'''import asyncio, json, psycopg, socket, sys, time
host, port = sys.argv[1], int(sys.argv[2])
budget = float(sys.argv[3])
executor_id, run_id, generation, incarnation, engine_id, container_id = sys.argv[4:10]
deadline = time.monotonic() + budget
''' + _READINESS_SOCKET_CODE + r'''async def main():
    url = open("/run/scientist/secrets/database_url", encoding="utf-8").read().strip()
    launch = json.load(open("/run/scientist/dispatch/config.json", encoding="utf-8"))
    if launch.get("service_subnet"):  # egress attached: same single in-subnet resolution + SCRAM as the entrypoint
        from scientist.private_dispatch_entrypoint import _pin_database_url
        url = _pin_database_url(launch["service_subnet"], url, launch.get("db_require_auth", "scram-sha-256"))
    if url.startswith("postgresql+psycopg://"):
        url = "postgresql://" + url[len("postgresql+psycopg://"):]
    if not url.startswith(("postgresql://", "postgres://")):
        raise ValueError("database URL unavailable")
    connection = await asyncio.wait_for(
        psycopg.AsyncConnection.connect(url, connect_timeout=1), remaining())
    try:
        expected = (executor_id, run_id, generation, "dispatch", incarnation, engine_id, container_id, "active")
        async def prove_active():
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    await asyncio.wait_for(cursor.execute("SET TRANSACTION READ ONLY"), remaining())
                    await asyncio.wait_for(cursor.execute("SELECT set_config('statement_timeout', %s, true)",
                                                         (str(max(1, int(remaining() * 1000))),)), remaining())
                    await asyncio.wait_for(cursor.execute(
                        "SELECT id, run_id, generation, kind, process_incarnation, engine_id, container_id, state "
                        "FROM runtime_executors WHERE id=%s", (executor_id,)), remaining())
                    row = await asyncio.wait_for(cursor.fetchone(), remaining())
                    if row is None or tuple(str(value) for value in row) != expected:
                        raise ValueError("dispatch authority identity mismatch")
        await prove_active()
        while True:
            try:
                probe_socket()
                break
            except (OSError, TimeoutError):
                delay = deadline - time.monotonic()
                if delay <= 0:
                    raise TimeoutError("readiness deadline")
                await asyncio.sleep(min(0.05, delay))
        await prove_active()
    finally:
        await asyncio.wait_for(connection.close(), remaining())
try:
    asyncio.run(main())
    print("READY")
except BaseException:
    raise SystemExit(1)
'''

class DispatchIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = Field(strict=True, ge=1, le=1)
    run_id: UUID
    generation: int = Field(strict=True, ge=1)
    executor_id: UUID
    process_incarnation: UUID
    engine_id: str = Field(min_length=1, max_length=200)
    runtime_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    skills_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_destinations: Mapping[UUID, str] = Field(max_length=32)
    secret_files: dict[str, str]
    # Launch-only (not in the static template): services subnet the entrypoint must pin its clients inside.
    service_subnet: str | None = None
    db_require_auth: str = Field(default="scram-sha-256", pattern=r"^scram-sha-256$")

    @field_validator("service_subnet")
    @classmethod
    def validate_subnet(cls, value: str | None) -> str | None:
        if value is not None:
            net = ipaddress.ip_network(value, strict=True)
            if net.version != 4 or not net.is_private or net.prefixlen < 16:
                raise ValueError("service subnet must be a private IPv4 network")
        return value

    @field_validator("provider_destinations")
    @classmethod
    def validate_destinations(cls, values: Mapping[UUID, str]) -> Mapping[UUID, str]:
        from urllib.parse import urlsplit

        for url in values.values():
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.port not in (None, 443) or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
                raise ValueError("provider destinations must be fixed HTTPS origins")
        return MappingProxyType(dict(values))

    @field_validator("secret_files")
    @classmethod
    def validate_secret_files(cls, values: dict[str, str]) -> dict[str, str]:
        if set(values) != _SECRET_NAMES or any(value != key for key, value in values.items()):
            raise ValueError("secret file names must match the fixed private secret set")
        return values

    @field_serializer("provider_destinations")
    def serialize_provider_destinations(self, values: Mapping[UUID, str]) -> dict[str, str]:
        return {str(key): value for key, value in values.items()}

    @field_validator("runtime_commit")
    @classmethod
    def pinned_runtime(cls, value: str) -> str:
        if value != RUNTIME_COMMIT:
            raise ValueError("dispatch runtime commit is not the reviewed source")
        return value


class DispatchTemplate(BaseModel):
    """Static, trusted deployment pins; per-launch identity is added locally."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = Field(strict=True, ge=1, le=1)
    runtime_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    skills_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_destinations: Mapping[UUID, str] = Field(max_length=32)
    secret_files: dict[str, str]

    @field_validator("runtime_commit")
    @classmethod
    def pinned_runtime(cls, value: str) -> str:
        if value != RUNTIME_COMMIT:
            raise ValueError("dispatch template runtime commit is not reviewed")
        return value

    @field_validator("provider_destinations")
    @classmethod
    def validate_destinations(cls, values: Mapping[UUID, str]) -> Mapping[UUID, str]:
        return DispatchIdentity.validate_destinations(values)

    @field_validator("secret_files")
    @classmethod
    def validate_secret_files(cls, values: dict[str, str]) -> dict[str, str]:
        return DispatchIdentity.validate_secret_files(values)

    @field_serializer("provider_destinations")
    def serialize_provider_destinations(self, values: Mapping[UUID, str]) -> dict[str, str]:
        return {str(key): value for key, value in values.items()}


def parse_dispatch_config(data: bytes, *, maximum_bytes: int = _MAX_CONFIG_BYTES) -> DispatchIdentity:
    if len(data) > maximum_bytes:
        raise ValueError("dispatch config exceeds size limit")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate dispatch config key")
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=unique_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        return DispatchIdentity.model_validate(value)
    except (UnicodeError, json.JSONDecodeError, ValidationError, TypeError, RecursionError) as exc:
        raise ValueError("invalid trusted dispatch config") from exc


def _parse_template(data: bytes) -> DispatchTemplate:
    if len(data) > _MAX_CONFIG_BYTES:
        raise ValueError("dispatch template exceeds size limit")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate dispatch template key")
            result[key] = value
        return result

    try:
        return DispatchTemplate.model_validate(json.loads(data, object_pairs_hook=unique_pairs))
    except (UnicodeError, json.JSONDecodeError, ValidationError, TypeError, RecursionError) as exc:
        raise ValueError("invalid trusted dispatch template") from exc


def check_egress_network(name: str | None) -> None:
    if name is not None and name not in {"scientist-b5-egress-test", "scientist-platform-egress"} \
            and not re.fullmatch(r"scientist-platform-egress-[a-z0-9-]{1,40}", name):
        raise ValueError("dispatch may join only an owned egress network")


@dataclass(frozen=True)
class DispatchServiceConfig:
    image: str
    image_digest: str
    service_network: str
    config_path: str
    secrets_dir: str
    port: int = 8123
    host_config_file: str = "/run/host-scientist/dispatch/config.json"
    host_secrets_dir: str = "/run/host-scientist/secrets"
    launcher_dir: str = "/run/host-scientist/dispatch-launches"
    egress_network: str | None = None  # absent: dispatch has no internet path and search fails closed

    def __post_init__(self) -> None:
        check_egress_network(self.egress_network)
        if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", self.image) or not self.image.endswith("@" + self.image_digest):
            raise ValueError("dispatch image must be pinned by its configured digest")
        if self.service_network not in {"scientist-b5-services-test", "scientist-platform-services"} and not re.fullmatch(r"scientist-platform-services-[a-z0-9-]{1,40}", self.service_network):
            raise ValueError("dispatch may join only an owned service network")
        if self.config_path != "/run/scientist/dispatch/config.json" or self.secrets_dir != "/run/scientist/secrets":
            raise ValueError("dispatch private mount destinations are fixed")
        if type(self.port) is not int or self.port != 8123:
            raise ValueError("private dispatch port must be 8123")
        if not all(Path(value).is_absolute() for value in (self.host_config_file, self.host_secrets_dir, self.launcher_dir)):
            raise ValueError("dispatch source mounts must be absolute paths")


class DockerDispatchRuntime:
    """Reconcile one private broker container on the owned Docker engine."""

    def __init__(self, config: DispatchServiceConfig, engine: DockerWorkerEngine | None = None):
        self.config = config
        self.engine = engine or DockerWorkerEngine()

    def _docker(self, *args: str) -> str:
        return self.engine._docker(*args)

    def engine_id(self) -> str:
        return self.engine.engine_id()

    def _ref(self, run_id: UUID, generation: int, executor_id: UUID, incarnation: UUID, container_id: str, engine_id: str) -> ExecutorRef:
        return ExecutorRef(executor_id, run_id, generation, "dispatch", None, incarnation, engine_id, container_id)

    def _run_docker_before(self, deadline: float, *args: str) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("dispatch readiness timed out")
        try:
            result = subprocess.run(
                ["docker", "--context", self.engine.context, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("dispatch readiness timed out") from exc
        if time.monotonic() >= deadline:
            raise RuntimeError("dispatch readiness timed out")
        if result.returncode != 0:
            raise RuntimeError("owned Docker readiness operation failed")
        return result.stdout.decode("utf-8", "replace").strip()

    def _verified_image_id(self, deadline: float | None = None) -> str:
        if deadline is None:
            return self.engine._verified_image_id(self.config.image, self.config.image_digest)
        raw = self._run_docker_before(
            deadline,
            "image",
            "inspect",
            "--format",
            "{{.Id}}|{{json .RepoDigests}}",
            self.config.image,
        )
        try:
            image_id, repo_digests_json = raw.split("|", 1)
            repo_digests = json.loads(repo_digests_json)
        except (ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("dispatch image inspection is invalid") from exc
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id) or image_id != self.config.image_digest:
            raise RuntimeError("dispatch image does not match configured immutable digest")
        if "@" in self.config.image and (
            not isinstance(repo_digests, list) or self.config.image not in repo_digests
        ):
            raise RuntimeError("dispatch image does not match configured immutable digest")
        return image_id

    def _engine_id_before(self, deadline: float) -> str:
        engine_id = self._run_docker_before(deadline, "info", "--format", "{{.ID}}")
        if not engine_id or len(engine_id) > 200:
            raise RuntimeError("owned Docker engine identity unavailable")
        version = self._run_docker_before(deadline, "version", "--format", "{{.Server.Version}}")
        if version != "29.8.2":
            raise RuntimeError("owned Docker engine version changed")
        return engine_id

    def _materialize_launch_config(self, run_id: UUID, generation: int, executor_id: UUID, incarnation: UUID, engine_id: str, *, service_subnet: str | None = None) -> Path:
        template_path = Path(self.config.host_config_file)
        try:
            template_stat = template_path.lstat()
            if (not stat.S_ISREG(template_stat.st_mode) or template_stat.st_uid != os.geteuid()
                    or template_stat.st_mode & 0o022 or template_stat.st_size > _MAX_CONFIG_BYTES):
                raise RuntimeError("dispatch template must be a bounded trusted regular file")
            template = _parse_template(template_path.read_bytes())
        except OSError as exc:
            raise RuntimeError("trusted dispatch template is unavailable") from exc

        launcher = Path(self.config.launcher_dir)
        launcher.mkdir(mode=0o700, exist_ok=True)
        try:
            launcher_stat = launcher.lstat()
        except OSError as exc:
            raise RuntimeError("private dispatch launcher directory is unavailable") from exc
        if (not stat.S_ISDIR(launcher_stat.st_mode) or stat.S_IMODE(launcher_stat.st_mode) != 0o700
                or launcher_stat.st_uid != os.geteuid()):
            raise RuntimeError("private dispatch launcher directory must be owned mode 0700")

        launch = DispatchIdentity.model_validate({
            **template.model_dump(mode="python"),
            "run_id": run_id,
            "generation": generation,
            "executor_id": executor_id,
            "process_incarnation": incarnation,
            "engine_id": engine_id,
            **({"service_subnet": service_subnet} if service_subnet is not None else {}),
        })
        path = launcher / f"dispatch-{run_id.hex}-g{generation}-{executor_id.hex}-{incarnation.hex}.json"
        content = launch.model_dump_json().encode("utf-8")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            existing = path.lstat()
            if (not stat.S_ISREG(existing.st_mode) or stat.S_IMODE(existing.st_mode) != 0o444
                    or path.read_bytes() != content):
                raise RuntimeError("existing per-launch config differs from trusted identity")
            return path
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            path.chmod(0o444)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path

    def _secret_mounts(self) -> list[str]:
        source_dir = Path(self.config.host_secrets_dir)
        try:
            parent = source_dir.lstat()
        except OSError as exc:
            raise RuntimeError("trusted dispatch secrets directory is unavailable") from exc
        if (not stat.S_ISDIR(parent.st_mode) or stat.S_IMODE(parent.st_mode) != 0o700
                or parent.st_uid != os.geteuid()):
            raise RuntimeError("dispatch secrets directory must be owned mode 0700")
        mounts: list[str] = []
        for name in sorted(_SECRET_NAMES):
            source = source_dir / name
            try:
                info = source.lstat()
            except OSError as exc:
                raise RuntimeError("trusted dispatch secret file is unavailable") from exc
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > 16 * 1024:
                raise RuntimeError("dispatch secret source must be a bounded regular file")
            mounts.extend(("--mount", f"type=bind,src={source},dst={self.config.secrets_dir}/{name},readonly"))
        return mounts

    def find(self, db: Session, run_id: UUID, generation: int, executor_id: UUID, operation_id: str | None, process_incarnation: UUID) -> ExecutorRef | None:
        current_engine = self.engine_id()
        image_id = self._verified_image_id()
        ids = self._docker("ps", "-aq", "--no-trunc", "--filter", f"label={_EXEC_LABEL}={executor_id}", "--filter", f"label={_RUN_LABEL}={run_id}", "--filter", f"label={_GEN_LABEL}={generation}")
        matches = [item for item in ids.splitlines() if item]
        if not matches:
            return None
        if len(matches) != 1 or not re.fullmatch(r"[a-f0-9]{64}", matches[0]):
            raise RuntimeError("dispatch physical identity is ambiguous")
        fields = self._docker("inspect", "--format", "{{index .Config.Labels \"scientist.platform/run\"}}|{{index .Config.Labels \"scientist.platform/generation\"}}|{{index .Config.Labels \"scientist.platform/executor\"}}|{{index .Config.Labels \"scientist.platform/incarnation\"}}|{{index .Config.Labels \"scientist.platform/kind\"}}|{{.Id}}|{{.Image}}|{{.Config.Image}}", matches[0]).split("|")
        if fields != [str(run_id), str(generation), str(executor_id), str(process_incarnation), "dispatch", matches[0], image_id, self.config.image]:
            raise RuntimeError("dispatch physical identity differs from durable identity")
        return self._ref(run_id, generation, executor_id, process_incarnation, matches[0], current_engine)

    def start(self, db: Session, run_id: UUID, generation: int, network: str, broker_ip: str, executor_id: UUID, process_incarnation: UUID, *, before_mutation: Callable[[str, str | None], None]) -> ExecutorRef:
        if not re.fullmatch(r"scientist-run-[a-f0-9]{12}-g[1-9][0-9]*", network):
            raise RuntimeError("dispatch may join only a generated per-run bridge")
        ip = ipaddress.ip_address(broker_ip)
        if ip.version != 4 or not ip.is_private or tuple(map(int, broker_ip.split(".")))[:2] != (172, 29) or int(broker_ip.split(".")[-1]) != 2:
            raise RuntimeError("dispatch requires its reserved private bridge address")
        engine_id = self.engine_id()
        image_id = self._verified_image_id()
        subnet = self._checked_egress_and_subnet()
        existing = self.find(db, run_id, generation, executor_id, None, process_incarnation)
        if existing is not None:
            if (existing.executor_id, existing.run_id, existing.generation, existing.kind,
                    existing.operation_id, existing.process_incarnation, existing.engine_id) != (
                    executor_id, run_id, generation, "dispatch", None, process_incarnation, engine_id):
                raise RuntimeError("dispatch engine incarnation changed")
            self._verify_container_image(existing.container_id, image_id)
            before_mutation(engine_id, existing.container_id)
            self._sync_egress_attachment(existing.container_id)
            self._docker("start", existing.container_id)
            self._ensure_run_network(network, existing.container_id, broker_ip)
            return existing
        labels = ["--label", f"{_RUN_LABEL}={run_id}", "--label", f"{_GEN_LABEL}={generation}", "--label", f"{_EXEC_LABEL}={executor_id}", "--label", f"{_KIND_LABEL}=dispatch", "--label", f"{_INC_LABEL}={process_incarnation}"]
        launch_config = self._materialize_launch_config(run_id, generation, executor_id, process_incarnation, engine_id, **({"service_subnet": subnet} if subnet else {}))
        mounts = ["--mount", f"type=bind,src={launch_config},dst={self.config.config_path},readonly", *self._secret_mounts()]
        before_mutation(engine_id, None)
        container_id = self._docker(
            "create", "--name", f"scientist-dispatch-{run_id.hex[:12]}-g{generation}",
            "--network", self.config.service_network, "--read-only", "--user", "65532:65532",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--cpus", "1",
            "--memory", "1073741824", "--memory-swap", "1073741824", "--pids-limit", "128",
            "--restart", "no", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16777216,mode=1777",
            "--entrypoint", "/opt/python/bin/python3.13", *labels, *mounts, self.config.image,
            "-m", "scientist.private_dispatch_entrypoint",
        )
        if not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise RuntimeError("Docker returned an invalid dispatch container identity")
        self._ensure_run_network(network, container_id, broker_ip)
        if self.config.egress_network is not None:
            self._docker("network", "connect", self.config.egress_network, container_id)
        self._docker("start", container_id)
        details = self._docker("inspect", "--format", "{{.Id}}|{{.State.Running}}|{{index .Config.Labels \"scientist.platform/run\"}}|{{index .Config.Labels \"scientist.platform/generation\"}}|{{index .Config.Labels \"scientist.platform/executor\"}}|{{index .Config.Labels \"scientist.platform/kind\"}}|{{index .Config.Labels \"scientist.platform/incarnation\"}}|{{.Image}}|{{.Config.Image}}", container_id).split("|")
        if details != [container_id, "true", str(run_id), str(generation), str(executor_id), "dispatch", str(process_incarnation), image_id, self.config.image]:
            raise RuntimeError("dispatch start identity changed")
        return self._ref(run_id, generation, executor_id, process_incarnation, container_id, engine_id)

    def _checked_egress_and_subnet(self) -> str | None:
        """Fail closed unless the egress network is the owned, ICC-disabled bridge; return the services subnet to pin."""
        if self.config.egress_network is None:
            return None
        try:
            fields = self._docker("network", "inspect", "--format",
                                  "{{.Driver}}|{{.Internal}}|{{index .Labels \"scientist.platform/egress\"}}|{{index .Options \"com.docker.network.bridge.enable_icc\"}}",
                                  self.config.egress_network).split("|")
            subnets = self._docker("network", "inspect", "--format", "{{range .IPAM.Config}}{{.Subnet}} {{end}}", self.config.service_network).split()
        except Exception as exc:
            raise RuntimeError("dispatch egress or services network inspection failed") from exc
        if len(fields) != 4 or fields[0] != "bridge" or fields[1] != "false" or not fields[2] or fields[3] != "false":
            raise RuntimeError("dispatch egress network is not the owned ICC-disabled bridge")
        v4 = [item for item in subnets if ":" not in item]
        if len(v4) != 1:
            raise RuntimeError("services network must expose exactly one IPv4 subnet")
        return v4[0]

    def _sync_egress_attachment(self, container_id: str) -> None:
        try:
            attached = set(json.loads(self._docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", container_id)))
        except Exception as exc:
            raise RuntimeError("dispatch network attachment inspection failed") from exc
        wanted = self.config.egress_network
        for name in sorted(attached):
            if name != wanted:
                try:
                    check_egress_network(name)
                except ValueError:
                    continue
                self._docker("network", "disconnect", name, container_id)
        if wanted is not None and wanted not in attached:
            self._docker("network", "connect", wanted, container_id)

    def _verify_container_image(self, container_id: str, image_id: str) -> None:
        details = self._docker("inspect", "--format", "{{.Image}}|{{.Config.Image}}", container_id).split("|")
        if len(details) != 2 or details != [image_id, self.config.image]:
            raise RuntimeError("dispatch container image differs from the inspected immutable image")

    def _ensure_run_network(self, network: str, container_id: str, broker_ip: str) -> None:
        try:
            containers = json.loads(self._docker("network", "inspect", "--format", "{{json .Containers}}", network))
        except Exception as exc:
            raise RuntimeError("private run network inspection failed") from exc
        if not isinstance(containers, dict):
            raise RuntimeError("private run network inspection was invalid")
        attached = containers.get(container_id)
        if attached is not None:
            address = str(attached.get("IPv4Address", "")).split("/", 1)[0] if isinstance(attached, dict) else ""
            if address != broker_ip:
                raise RuntimeError("dispatch is attached to a run network with the wrong address")
            return
        if any(isinstance(item, dict) and item.get("IPv4Address", "").split("/", 1)[0] == broker_ip for item in containers.values()):
            raise RuntimeError("private broker bridge address is already occupied")
        self._docker("network", "connect", "--ip", broker_ip, network, container_id)

    def inactive(self, db: Session, executor: ExecutorRef, operation_id: str) -> bool:
        if executor.kind != "dispatch":
            return False
        return dispatch_is_inactive(db, executor.run_id, operation_id, executor.generation, probe=self._probe_inactive)

    def _assert_physical_identity(self, executor: ExecutorRef, deadline: float | None = None) -> None:
        image_id = self._verified_image_id(deadline)
        fmt = "{{.Id}}|{{.State.Running}}|{{index .Config.Labels \"scientist.platform/run\"}}|{{index .Config.Labels \"scientist.platform/generation\"}}|{{index .Config.Labels \"scientist.platform/executor\"}}|{{index .Config.Labels \"scientist.platform/kind\"}}|{{index .Config.Labels \"scientist.platform/incarnation\"}}|{{.Image}}|{{.Config.Image}}"
        inspect = self._run_docker_before if deadline is not None else self._docker
        actual = (
            inspect(deadline, "inspect", "--format", fmt, executor.container_id)
            if deadline is not None
            else inspect("inspect", "--format", fmt, executor.container_id)
        ).split("|")
        expected = [
            executor.container_id, "true", str(executor.run_id), str(executor.generation),
            str(executor.executor_id), "dispatch", str(executor.process_incarnation),
            image_id, self.config.image,
        ]
        if len(actual) != len(expected) or actual != expected:
            raise RuntimeError("dispatch readiness physical identity is not active")

    def _probe_dispatch_container(self, executor: ExecutorRef, address: str, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("dispatch readiness timed out")
        try:
            result = subprocess.run(
                [
                    "docker",
                    "--context",
                    self.engine.context,
                    "exec",
                    "--user",
                    "65532:65532",
                    executor.container_id,
                    "python3",
                    "-c",
                    _READINESS_PROBE,
                    address,
                    str(self.config.port),
                    str(remaining),
                    str(executor.executor_id),
                    str(executor.run_id),
                    str(executor.generation),
                    str(executor.process_incarnation),
                    executor.engine_id,
                    executor.container_id,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("dispatch readiness timed out") from exc
        if time.monotonic() >= deadline:
            raise RuntimeError("dispatch readiness timed out")
        if result.returncode != 0 or result.stdout.strip() != b"READY":
            raise RuntimeError("dispatch readiness endpoint returned an invalid response")

    def ready(
        self,
        db: Session,
        executor: ExecutorRef,
        broker_ip: str,
        broker_port: int,
        timeout_seconds: float = 10.0,
    ) -> bool:
        """Wait within one absolute deadline using a socket probe inside the owned VM."""
        try:
            address = ipaddress.ip_address(broker_ip)
        except ValueError as exc:
            raise RuntimeError("dispatch readiness requires a numeric broker address") from exc
        if (
            address.version != 4
            or tuple(map(int, broker_ip.split(".")))[:2] != (172, 29)
            or type(broker_port) is not int
            or broker_port != self.config.port
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60
            or executor.kind != "dispatch"
        ):
            raise RuntimeError("dispatch readiness target or executor identity is invalid")
        deadline = time.monotonic() + timeout_seconds

        def check_deadline() -> None:
            if time.monotonic() >= deadline:
                raise RuntimeError("dispatch readiness timed out")

        if self._engine_id_before(deadline) != executor.engine_id:
            raise RuntimeError("dispatch readiness engine identity changed")
        # `db` remains in the signature for compatibility. Its caller-owned
        # session is deliberately not used here: guest_probe reads the current
        # authority row through the mounted database URL under this deadline.
        self._assert_physical_identity(executor, deadline)
        check_deadline()
        self._probe_dispatch_container(executor, address.compressed, deadline)
        check_deadline()
        self._assert_physical_identity(executor, deadline)
        check_deadline()
        if self._engine_id_before(deadline) != executor.engine_id:
            raise RuntimeError("dispatch readiness engine identity changed")
        check_deadline()
        return True

    def _probe_inactive(self, incarnation: UUID, engine_id: str, container_id: str) -> bool:
        try:
            if self.engine_id() != engine_id:
                return False
            found = self._docker("ps", "-aq", "--no-trunc", "--filter", f"id={container_id}").splitlines()
            if not found:
                return self.engine_id() == engine_id
            if found != [container_id]:
                return False
            details = self._docker("inspect", "--format", "{{index .Config.Labels \"scientist.platform/incarnation\"}}|{{.State.Running}}|{{.Id}}", container_id).split("|")
            return details == [str(incarnation), "false", container_id] and self.engine_id() == engine_id
        except Exception:
            return False

    def stop(self, db: Session, executor: ExecutorRef, grace_seconds: int) -> bool:
        if (executor.kind != "dispatch" or type(grace_seconds) is not int or not 0 <= grace_seconds <= 120):
            return False
        try:
            if self.engine_id() != executor.engine_id:
                return False
            if not re.fullmatch(r"[a-f0-9]{64}", executor.container_id):
                return False
            listed = self._docker("ps", "-aq", "--no-trunc", "--filter", f"id={executor.container_id}").splitlines()
            if not listed:
                return self.engine_id() == executor.engine_id
            if listed != [executor.container_id]:
                return False
            inspect = "{{index .Config.Labels \"scientist.platform/executor\"}}|{{index .Config.Labels \"scientist.platform/run\"}}|{{index .Config.Labels \"scientist.platform/generation\"}}|{{index .Config.Labels \"scientist.platform/kind\"}}|{{index .Config.Labels \"scientist.platform/incarnation\"}}|{{.Id}}|{{.State.Running}}"
            try:
                details = self._docker("inspect", "--format", inspect, executor.container_id).split("|")
            except Exception:
                if self.engine_id() != executor.engine_id:
                    return False
                remaining = self._docker("ps", "-aq", "--no-trunc", "--filter", f"id={executor.container_id}").splitlines()
                return not remaining and self.engine_id() == executor.engine_id
            expected = [str(executor.executor_id), str(executor.run_id), str(executor.generation), "dispatch", str(executor.process_incarnation), executor.container_id]
            if len(details) != 7 or details[:6] != expected or details[6] not in {"true", "false"}:
                return False
            if details[6] == "true":
                self._docker("stop", "--time", str(grace_seconds), executor.container_id)
            if self.engine_id() != executor.engine_id:
                return False
            remaining = self._docker("ps", "-aq", "--no-trunc", "--filter", f"id={executor.container_id}").splitlines()
            if not remaining:
                return self.engine_id() == executor.engine_id
            if remaining != [executor.container_id]:
                return False
            try:
                details = self._docker("inspect", "--format", inspect, executor.container_id).split("|")
            except Exception:
                return False
            if len(details) != 7 or details[:6] != expected or details[6] != "false":
                return False
            self._docker("rm", "--force", executor.container_id)
            remaining = self._docker("ps", "-aq", "--no-trunc", "--filter", f"id={executor.container_id}").splitlines()
            return not remaining and self.engine_id() == executor.engine_id
        except Exception:
            return False
