"""Private-only HTTP composition for a trusted per-run dispatch container."""
from __future__ import annotations

import ipaddress
import os
import socket
import stat
import time
from io import BytesIO
from pathlib import Path
import boto3
from botocore.config import Config

import uvicorn
from sqlalchemy import text
from sqlalchemy.engine import make_url

from scientist import broker, objects
from scientist import checkpoints
from scientist import db as database
from scientist.db import session
from scientist.dispatch_runtime import DispatchIdentity, parse_dispatch_config
from scientist.dispatch_authority import BoundDispatchTransport
from scientist.private_worker_api import RuntimePins, WorkerController, create_private_app

_CONFIG = Path("/run/scientist/dispatch/config.json")
_SECRETS = Path("/run/scientist/secrets")
_SERVICE_PORT = 8123


def _read_secret(name: str) -> bytes:
    path = _SECRETS / name
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > 16 * 1024:
            raise ValueError("invalid private secret file")
        value = path.read_bytes().strip()
    except OSError as exc:
        raise RuntimeError("private dispatch prerequisites unavailable") from exc
    if not value:
        raise RuntimeError("private dispatch prerequisites unavailable")
    return value


def _resolve_inside(host: str, port: int, subnet: ipaddress.IPv4Network, resolver) -> str:
    """Resolve once; every answer must lie in the services subnet, so no name can leave it."""
    try:
        addresses = [ipaddress.ip_address(item.split("%", 1)[0]) for item in resolver(host, port)]
    except (OSError, ValueError) as exc:
        raise RuntimeError("service host resolution failed") from exc
    if not addresses or any(address not in subnet for address in addresses):
        raise RuntimeError("service host resolves outside the services network")
    return str(addresses[0])


def _pin_database_url(service_subnet: str, database_url: str, require_auth: str, *, resolver=None) -> str:
    """Database url with hostaddr + require_auth from one in-subnet resolution (shared with the readiness probe)."""
    resolver = resolver or (lambda host, port: [item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)])
    subnet = ipaddress.ip_network(service_subnet)
    url = make_url(database_url)
    query_host = url.query.get("host", "")
    hosts = ([url.host] if url.host else []) + (query_host.split(",") if isinstance(query_host, str) and query_host else [])
    if (len(hosts) != 1 or not isinstance(query_host, str) or "," in hosts[0] or hosts[0].startswith("/")
            or "hostaddr" in url.query or "require_auth" in url.query):
        raise RuntimeError("database url must name exactly one TCP service host without hostaddr or require_auth")
    db_ip = _resolve_inside(hosts[0], url.port or 5432, subnet, resolver)
    pinned = url.update_query_dict({"hostaddr": db_ip, "require_auth": require_auth}).render_as_string(hide_password=False)
    return pinned


def _pin_service_hosts(service_subnet: str, database_url: str, s3_host: str, s3_port: int, require_auth: str, *, resolver=None) -> tuple[str, str]:
    """Return (pinned database url, S3 endpoint) built from IPs resolved exactly once."""
    resolver = resolver or (lambda host, port: [item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)])
    pinned = _pin_database_url(service_subnet, database_url, require_auth, resolver=resolver)
    subnet = ipaddress.ip_network(service_subnet)
    return pinned, f"http://{_resolve_inside(s3_host, s3_port, subnet, resolver)}:{s3_port}"


def _wait_for_active_executor(identity: DispatchIdentity, container_id: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with session() as db:
                row = db.execute(text("""
                    SELECT state, container_id, engine_id, kind, process_incarnation, run_id, generation
                    FROM runtime_executors WHERE id=:executor
                """), {"executor": identity.executor_id}).one_or_none()
                if row is not None:
                    expected = ("active", identity.run_id, identity.generation, "dispatch", identity.process_incarnation)
                    observed = (row.state, row.run_id, row.generation, row.kind, row.process_incarnation)
                    if row.container_id and container_id and not row.container_id.startswith(container_id):
                        raise RuntimeError("dispatch database identity differs from container")
                    if observed == expected and row.container_id and row.engine_id == identity.engine_id:
                        return
                    if observed[1:] != expected[1:] or (row.engine_id and row.engine_id != identity.engine_id):
                        raise RuntimeError("dispatch database identity differs from trusted config")
        except RuntimeError:
            raise
        except Exception:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("dispatch executor activation timed out")
        time.sleep(0.2)


def create_dispatch_app(identity: DispatchIdentity):
    """Build only the worker effects and checkpoint/result API surface."""
    pins = RuntimePins(
        runtime_commit=identity.runtime_commit,
        image_digest=identity.image_digest,
        skills_digest=identity.skills_digest,
        environment_digest=identity.environment_digest,
    )
    checkpoints.configure_trusted_pins(
        runtime_commit=pins.runtime_commit, image_digest=pins.image_digest,
        skills_digest=pins.skills_digest, environment_digest=pins.environment_digest,
    )

    def persist_result(db, project_id, content: bytes, content_type: str):
        return objects.put(db, project_id, BytesIO(content), content_type)

    def read_result(ref):
        with objects.open_verified(ref) as source:
            return source.read()

    executor_id = identity.executor_id
    incarnation = identity.process_incarnation
    broker.configure(
        transport=BoundDispatchTransport(executor_id, incarnation, broker.http_transport),
        persist_result=persist_result,
        capability_key=_read_secret("broker_capability_key"),
        provider_destinations={str(key): value for key, value in identity.provider_destinations.items()},
    )
    controller = WorkerController(
        pins=pins,
        provider_destinations=identity.provider_destinations,
        capture=checkpoints.capture,
        result_reader=read_result,
    )
    app = create_private_app(controller)

    @app.get("/ready")
    def ready():
        return {"ready": True}

    return app


def main() -> None:
    config = parse_dispatch_config(_CONFIG.read_bytes())
    secret_values = {name: _read_secret(name).decode("utf-8") for name in config.secret_files}
    database_url, s3_endpoint = secret_values["database_url"], "http://scientist-minio:9000"
    if config.service_subnet is not None:  # egress is attached: bind to IPs resolved once inside the services subnet
        database_url, s3_endpoint = _pin_service_hosts(config.service_subnet, database_url, "scientist-minio", 9000, config.db_require_auth)
    # Runtime modules use these only inside this trusted private process.
    os.environ["SCIENTIST_DATABASE_URL"] = database_url
    database.DATABASE_URL = database_url
    os.environ["SCIENTIST_MASTER_KEY_FILE"] = str(_SECRETS / "master_key")
    os.environ["SCIENTIST_S3_ACCESS_KEY"] = secret_values["s3_access_key"]
    os.environ["SCIENTIST_S3_SECRET_KEY"] = secret_values["s3_secret_key"]
    os.environ["SCIENTIST_BROKER_CAPABILITY_KEY"] = secret_values["broker_capability_key"]
    os.environ["SCIENTIST_S3_ENDPOINT"] = s3_endpoint
    os.environ["SCIENTIST_OBJECT_BUCKET"] = "scientist-b5"
    objects.configure(boto3.client(
        "s3", endpoint_url=s3_endpoint, config=Config(s3={"addressing_style": "path"}),
        aws_access_key_id=secret_values["s3_access_key"],
        aws_secret_access_key=secret_values["s3_secret_key"], region_name="us-east-1",
    ), bucket="scientist-b5")
    container_id = Path("/etc/hostname").read_text(encoding="ascii").strip().lower()
    if len(container_id) < 12:
        raise RuntimeError("dispatch container identity unavailable")
    _wait_for_active_executor(config, container_id)
    app = create_dispatch_app(config)
    uvicorn.run(app, host="0.0.0.0", port=_SERVICE_PORT, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
