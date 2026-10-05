from __future__ import annotations

import json
import os
import stat
import socketserver
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest
from hashlib import sha256
from uuid import uuid4

from scientist import broker, private_dispatch_entrypoint
from scientist.contracts import ObjectRef, Principal
from scientist.dispatch_runtime import DispatchIdentity
from scientist.dispatch_runtime import DockerDispatchRuntime, DispatchServiceConfig, parse_dispatch_config
from scientist.domain import submit_run
from scientist.private_dispatch_entrypoint import create_dispatch_app
from scientist.runtime_contracts import RUNTIME_COMMIT
from scientist.supervisor import ExecutorRef
from fastapi.testclient import TestClient


def _config():
    digest = "sha256:" + "a" * 64
    return DispatchServiceConfig(
        image=f"registry.invalid/scientist-dispatch@{digest}",
        image_digest=digest,
        service_network="scientist-b5-services-test",
        config_path="/run/scientist/dispatch/config.json",
        secrets_dir="/run/scientist/secrets",
        port=8123,
        host_config_file="/tmp/dispatch-config.json",
        host_secrets_dir="/tmp/dispatch-secrets",
        launcher_dir="/tmp/dispatch-launches",
    )


class _ReadyResult:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row


class _ReadyDB:
    def __init__(self, row):
        self.row = row

    def execute(self, statement, params):
        return _ReadyResult(self.row)


def _active_dispatch_row(ref):
    return {
        "id": ref.executor_id,
        "run_id": ref.run_id,
        "generation": ref.generation,
        "kind": ref.kind,
        "process_incarnation": ref.process_incarnation,
        "engine_id": ref.engine_id,
        "container_id": ref.container_id,
        "state": "active",
    }


def _mock_physical_dispatch(monkeypatch, runtime, ref):
    image_id = "sha256:" + "f" * 64
    monkeypatch.setattr(runtime, "_verified_image_id", lambda: image_id)
    fields = (
        ref.container_id, "true", str(ref.run_id), str(ref.generation), str(ref.executor_id),
        "dispatch", str(ref.process_incarnation), image_id, runtime.config.image,
    )
    monkeypatch.setattr(runtime, "_docker", lambda *args: "|".join(fields))


def test_dispatch_readiness_is_identity_bound_and_uses_container_probe(monkeypatch):
    runtime = DockerDispatchRuntime(_config())
    run_id, executor_id, incarnation = uuid4(), uuid4(), uuid4()
    ref = ExecutorRef(executor_id, run_id, 2, "dispatch", None, incarnation, "engine-1", "a" * 64)
    calls = []
    monkeypatch.setattr(runtime, "_engine_id_before", lambda deadline: "engine-1")
    monkeypatch.setattr(runtime, "_assert_physical_identity", lambda executor, deadline=None: None)
    monkeypatch.setattr(
        runtime,
        "_probe_dispatch_container",
        lambda executor, address, deadline: calls.append((executor.container_id, address, deadline)),
    )

    assert runtime.ready(_ReadyDB(_active_dispatch_row(ref)), ref, "172.29.42.2", 8123, timeout_seconds=1)
    assert len(calls) == 1
    assert calls[0][0:2] == (ref.container_id, "172.29.42.2")


def test_dispatch_readiness_does_not_trust_stale_caller_database_session(monkeypatch):
    import scientist.dispatch_runtime as dispatch_runtime

    runtime = DockerDispatchRuntime(_config())
    ref = ExecutorRef(uuid4(), uuid4(), 2, "dispatch", None, uuid4(), "engine-1", "a" * 64)
    stale = _active_dispatch_row(ref)
    stale["state"] = "inactive"
    calls = []
    monkeypatch.setattr(runtime, "_engine_id_before", lambda deadline: "engine-1")
    monkeypatch.setattr(runtime, "_assert_physical_identity", lambda executor, deadline=None: None)

    def guest_probe(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"READY\n", b"")

    monkeypatch.setattr(dispatch_runtime.subprocess, "run", guest_probe)
    assert runtime.ready(_ReadyDB(stale), ref, "172.29.42.2", 8123, timeout_seconds=1)
    assert calls[0][13:] == [
        str(ref.executor_id),
        str(ref.run_id),
        str(ref.generation),
        str(ref.process_incarnation),
        ref.engine_id,
        ref.container_id,
    ]


def test_dispatch_readiness_checks_fresh_physical_labels_and_image(monkeypatch):
    runtime = DockerDispatchRuntime(_config())
    run_id, executor_id, incarnation = uuid4(), uuid4(), uuid4()
    ref = ExecutorRef(executor_id, run_id, 2, "dispatch", None, incarnation, "engine-1", "a" * 64)
    monkeypatch.setattr(runtime, "_verified_image_id", lambda deadline=None: _config().image_digest)
    monkeypatch.setattr(runtime, "_docker", lambda *args: "|".join((
        ref.container_id, "2", str(run_id), str(ref.generation), str(executor_id), "dispatch",
        str(incarnation), _config().image_digest, runtime.config.image,
    )))
    with pytest.raises(RuntimeError, match="physical identity"):
        runtime._assert_physical_identity(ref)


def test_dispatch_find_rejects_mismatched_identity_labels(monkeypatch):
    runtime = DockerDispatchRuntime(_config())
    run_id, executor_id, incarnation = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(runtime, "engine_id", lambda: "engine-1")
    monkeypatch.setattr(runtime, "_verified_image_id", lambda: _config().image_digest)
    responses = iter(("a" * 64, "wrong-incarnation|" + "a" * 64))
    monkeypatch.setattr(runtime, "_docker", lambda *args: next(responses))

    with pytest.raises(RuntimeError, match="physical identity"):
        runtime.find(object(), run_id, 2, executor_id, None, incarnation)


def test_dispatch_find_rejects_container_with_wrong_immutable_image(monkeypatch):
    runtime = DockerDispatchRuntime(_config())
    run_id, executor_id, incarnation = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(runtime, "engine_id", lambda: "engine-1")
    monkeypatch.setattr(runtime, "_verified_image_id", lambda: "sha256:" + "f" * 64)

    def docker(*args):
        if args[0] == "ps":
            return "a" * 64
        return "|".join((str(run_id), "2", str(executor_id), str(incarnation), "dispatch", "a" * 64, "sha256:" + "e" * 64, runtime.config.image))

    monkeypatch.setattr(runtime, "_docker", docker)
    with pytest.raises(RuntimeError, match="physical identity"):
        runtime.find(object(), run_id, 2, executor_id, None, incarnation)


def test_dispatch_inactivity_fails_closed_on_unknown_inspection(monkeypatch):
    import scientist.dispatch_runtime as dispatch_runtime

    runtime = DockerDispatchRuntime(_config())
    ref = ExecutorRef(uuid4(), uuid4(), 2, "dispatch", None, uuid4(), "engine-1", "a" * 64)
    monkeypatch.setattr(runtime, "engine_id", lambda: "engine-1")
    monkeypatch.setattr(runtime, "_docker", lambda *args: (_ for _ in ()).throw(RuntimeError("unavailable")))
    monkeypatch.setattr(dispatch_runtime, "dispatch_is_inactive", lambda db, run_id, operation_id, generation, *, probe: probe(ref.process_incarnation, ref.engine_id, ref.container_id))

    assert runtime.inactive(object(), ref, "operation-1") is False


def test_dispatch_stop_identity_race_does_not_remove_replacement(monkeypatch):
    runtime = DockerDispatchRuntime(_config())
    ref = ExecutorRef(uuid4(), uuid4(), 2, "dispatch", None, uuid4(), "engine-1", "a" * 64)
    calls = []
    monkeypatch.setattr(runtime, "engine_id", lambda: "engine-1")

    def docker(*args):
        calls.append(args)
        if args[0] == "ps":
            return "a" * 64
        if args[:2] == ("inspect", "--format"):
            return "{}|" + "b" * 64 + "|true"
        return ""

    monkeypatch.setattr(runtime, "_docker", docker)
    assert runtime.stop(object(), ref, 0) is False
    assert not any(call[0] in {"stop", "rm"} for call in calls)


def test_stop_accepts_exact_identity_and_removes_only_that_container(monkeypatch):
    runtime = DockerDispatchRuntime(_config())
    ref = ExecutorRef(uuid4(), uuid4(), 2, "dispatch", None, uuid4(), "engine-1", "a" * 64)
    calls = []
    monkeypatch.setattr(runtime, "engine_id", lambda: "engine-1")

    ps_checks = 0
    inspect_checks = 0
    def docker(*args):
        nonlocal ps_checks, inspect_checks
        calls.append(args)
        if args[0] == "ps":
            ps_checks += 1
            return ref.container_id if ps_checks < 3 else ""
        if args[:2] == ("inspect", "--format"):
            inspect_checks += 1
            state = "true" if inspect_checks == 1 else "false"
            return "|".join((str(ref.executor_id), str(ref.run_id), str(ref.generation), "dispatch", str(ref.process_incarnation), ref.container_id, state))
        return ""

    monkeypatch.setattr(runtime, "_docker", docker)
    assert runtime.stop(object(), ref, 3) is True
    assert ("stop", "--time", "3", ref.container_id) in calls
    assert ("rm", "--force", ref.container_id) in calls


def test_stop_fails_closed_on_malformed_identity_inspection(monkeypatch):
    runtime = DockerDispatchRuntime(_config())
    ref = ExecutorRef(uuid4(), uuid4(), 2, "dispatch", None, uuid4(), "engine-1", "a" * 64)
    calls = []
    monkeypatch.setattr(runtime, "engine_id", lambda: "engine-1")
    monkeypatch.setattr(runtime, "_docker", lambda *args: calls.append(args) or "too|few")

    assert runtime.stop(object(), ref, 0) is False
    assert not any(call[0] in {"stop", "rm"} for call in calls)


@pytest.mark.parametrize("running", ["", "unknown", "TRUE", "yes"])
def test_stop_rejects_noncanonical_running_tokens(monkeypatch, running):
    runtime = DockerDispatchRuntime(_config())
    ref = ExecutorRef(uuid4(), uuid4(), 2, "dispatch", None, uuid4(), "engine-1", "a" * 64)
    calls = []
    monkeypatch.setattr(runtime, "engine_id", lambda: "engine-1")

    def docker(*args):
        calls.append(args)
        if args[0] == "ps":
            return ref.container_id
        return "|".join((str(ref.executor_id), str(ref.run_id), str(ref.generation), "dispatch", str(ref.process_incarnation), ref.container_id, running))

    monkeypatch.setattr(runtime, "_docker", docker)
    assert runtime.stop(object(), ref, 0) is False
    assert not any(call[0] in {"stop", "rm"} for call in calls)


def test_stop_treats_exact_container_absence_on_same_engine_as_inactive(monkeypatch):
    runtime = DockerDispatchRuntime(_config())
    ref = ExecutorRef(uuid4(), uuid4(), 2, "dispatch", None, uuid4(), "engine-1", "a" * 64)
    calls = []
    monkeypatch.setattr(runtime, "engine_id", lambda: "engine-1")
    monkeypatch.setattr(runtime, "_docker", lambda *args: calls.append(args) or "")

    assert runtime.stop(object(), ref, 0) is True
    assert calls == [("ps", "-aq", "--no-trunc", "--filter", f"id={ref.container_id}")]


class _ReadySocketServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, chunks, delay):
        self.chunks = chunks
        self.delay = delay
        self.chunks_sent = 0
        super().__init__(("127.0.0.1", 0), _ReadySocketHandler)


class _ReadySocketHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(1)
        request = bytearray()
        try:
            while b"\r\n\r\n" not in request:
                request.extend(self.request.recv(1024))
            payload = b"".join(self.server.chunks)
            self.request.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(payload)).encode()
                + b"\r\nConnection: close\r\n\r\n"
            )
            for chunk in self.server.chunks:
                if self.server.delay:
                    time.sleep(self.server.delay)
                self.request.sendall(chunk)
                self.server.chunks_sent += 1
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return


def _run_readiness_probe(payload, *, timeout=1.0, chunks=None, delay=0):
    from scientist.dispatch_runtime import _READINESS_PROBE

    if chunks is None:
        chunks = [payload]
    server = _ReadySocketServer(chunks, delay)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    identities = [str(uuid4()), str(uuid4()), "2", str(uuid4()), "engine-test", "a" * 64]
    prelude = r"""import asyncio, builtins, io, psycopg, sys
values = sys.argv[4:10]
class Cursor:
    def __init__(self): self.row = None
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def execute(self, query, params=None):
        if query.startswith("SELECT id, run_id, generation, kind, process_incarnation, engine_id, container_id, state"):
            if params != (values[0],): raise ValueError("identity bind mismatch")
            self.row = (values[0], values[1], int(values[2]), "dispatch", values[3], values[4], values[5], "active")
        elif query.startswith("SET TRANSACTION READ ONLY") or "set_config('statement_timeout'" in query:
            return
        else:
            raise ValueError("unexpected authority query")
    async def fetchone(self): return self.row
class Transaction:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
class Connection:
    def transaction(self): return Transaction()
    def cursor(self): return Cursor()
    async def close(self): return None
async def connect(url, **kwargs):
    if url != "postgresql://synthetic@127.0.0.1/test" or kwargs != {"connect_timeout": 1}:
        raise ValueError("unexpected synthetic database connection")
    return Connection()
psycopg.AsyncConnection.connect = staticmethod(connect)
_real_open = builtins.open
def _open(path, *args, **kwargs):
    if path == "/run/scientist/secrets/database_url":
        return io.StringIO("postgresql+psycopg://synthetic@127.0.0.1/test")
    if path == "/run/scientist/dispatch/config.json":
        return io.StringIO("{}")  # no service_subnet: no egress, unpinned as before
    return _real_open(path, *args, **kwargs)
builtins.open = _open
"""
    started = time.monotonic()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                prelude + _READINESS_PROBE,
                "127.0.0.1",
                str(server.server_address[1]),
                str(timeout),
                *identities,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout + 1,
        )
        elapsed = time.monotonic() - started
        return result, elapsed, server.chunks_sent
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize(
    "payload",
    [b'{"ready":1}', b'{"ready":true,"extra":false}', b'{"extra":false}', b'{}', b'{"ready":false}'],
)
def test_dispatch_readiness_probe_requires_exact_literal_true_object(payload):
    result, _, _ = _run_readiness_probe(payload)
    assert result.returncode != 0


def test_dispatch_readiness_probe_accepts_literal_true_object():
    result, _, _ = _run_readiness_probe(b'{"ready":true}')
    assert result.returncode == 0
    assert result.stdout.strip() == b"READY"


def test_dispatch_readiness_probe_has_one_absolute_deadline_for_trickled_body():
    payload = b'{"ready":true}'
    result, elapsed, chunks_sent = _run_readiness_probe(
        payload,
        timeout=0.3,
        chunks=[payload[index:index + 1] for index in range(len(payload))],
        delay=0.1,
    )
    assert result.returncode != 0
    assert elapsed < 0.9
    assert chunks_sent < len(payload)


def test_dispatch_readiness_probe_runs_inside_the_exact_container_with_budget(monkeypatch):
    import scientist.dispatch_runtime as dispatch_runtime

    runtime = DockerDispatchRuntime(_config())
    ref = ExecutorRef(uuid4(), uuid4(), 2, "dispatch", None, uuid4(), "engine-1", "a" * 64)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, b"READY\n", b"")

    monkeypatch.setattr(dispatch_runtime.subprocess, "run", run)
    runtime._probe_dispatch_container(ref, "172.29.42.2", time.monotonic() + 1)
    args, kwargs = calls[0]
    assert args[:5] == ["docker", "--context", runtime.engine.context, "exec", "--user"]
    assert args[5:7] == ["65532:65532", ref.container_id]
    assert args[7:10] == ["python3", "-c", dispatch_runtime._READINESS_PROBE]
    assert args[10:12] == ["172.29.42.2", "8123"]
    assert args[13:] == [
        str(ref.executor_id),
        str(ref.run_id),
        str(ref.generation),
        str(ref.process_incarnation),
        ref.engine_id,
        ref.container_id,
    ]
    assert "/run/scientist/secrets/database_url" in dispatch_runtime._READINESS_PROBE
    assert "FROM runtime_executors WHERE id=%s" in dispatch_runtime._READINESS_PROBE
    assert kwargs["timeout"] <= 1


def test_dispatch_readiness_does_not_block_on_caller_database_session(monkeypatch):
    import scientist.dispatch_runtime as dispatch_runtime

    runtime = DockerDispatchRuntime(_config())
    ref = ExecutorRef(uuid4(), uuid4(), 2, "dispatch", None, uuid4(), "engine-1", "a" * 64)
    database_calls = []
    real_run = subprocess.run

    class StalledDB:
        def execute(self, *args, **kwargs):
            database_calls.append(True)
            time.sleep(0.25)
            return _ReadyDB(_active_dispatch_row(ref))

    def slow_guest_probe(args, **kwargs):
        return real_run(
            [sys.executable, "-c", "import time; time.sleep(0.25)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=kwargs["timeout"],
        )

    monkeypatch.setattr(runtime, "_engine_id_before", lambda deadline: "engine-1")
    monkeypatch.setattr(runtime, "_assert_physical_identity", lambda executor, deadline=None: None)
    monkeypatch.setattr(dispatch_runtime.subprocess, "run", slow_guest_probe)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        runtime.ready(StalledDB(), ref, "172.29.42.2", 8123, timeout_seconds=0.03)
    elapsed = time.monotonic() - started
    assert database_calls == []
    assert elapsed < 0.18


def test_dispatch_readiness_times_out_if_post_probe_physical_proof_overruns(monkeypatch):
    import scientist.dispatch_runtime as dispatch_runtime

    runtime = DockerDispatchRuntime(_config())
    ref = ExecutorRef(uuid4(), uuid4(), 2, "dispatch", None, uuid4(), "engine-1", "a" * 64)
    now = [0.0]
    proofs = [0]
    monkeypatch.setattr(dispatch_runtime.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(runtime, "_engine_id_before", lambda deadline: "engine-1")
    monkeypatch.setattr(runtime, "_probe_dispatch_container", lambda executor, address, deadline: None)

    def physical(executor, deadline=None):
        proofs[0] += 1
        if proofs[0] == 2:
            now[0] = 2.0

    monkeypatch.setattr(runtime, "_assert_physical_identity", physical)
    with pytest.raises(RuntimeError, match="timed out"):
        runtime.ready(_ReadyDB(_active_dispatch_row(ref)), ref, "172.29.42.2", 8123, timeout_seconds=1)


def test_dispatch_result_persistence_receives_project_id(
    db,
    project_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, session_id = project_session
    owner = Principal(identity=uuid4(), kind="owner")
    run = submit_run(
        db,
        owner,
        project_id,
        session_id,
        uuid4().hex,
        "Fixture result persistence",
        [],
        uuid4(),
        "fixture",
    )
    for name in (
        "_transport",
        "_persist_result",
        "_capability_key",
        "_resolver",
        "_peer_destinations",
        "_provider_destinations",
        "_dispatch_is_inactive",
    ):
        monkeypatch.setattr(broker, name, getattr(broker, name))
    content = b"synthetic completion"
    calls = []

    def put(db_arg, project_arg, stream, content_type):
        calls.append((db_arg, project_arg, stream.read(), content_type))
        digest = sha256(content).hexdigest()
        return ObjectRef(
            project_id=project_arg,
            key=f"{project_arg}/{digest}",
            sha256=digest,
            size=len(content),
            content_type="application/octet-stream",
        )

    monkeypatch.setattr(private_dispatch_entrypoint, "_read_secret", lambda _name: b"x" * 32)
    monkeypatch.setattr(private_dispatch_entrypoint.objects, "put", put)
    monkeypatch.setattr(
        private_dispatch_entrypoint.checkpoints,
        "configure_trusted_pins",
        lambda **_pins: None,
    )
    identity = DispatchIdentity(
        schema_version=1,
        run_id=run.run_id,
        generation=1,
        executor_id=uuid4(),
        process_incarnation=uuid4(),
        engine_id="fixture-engine",
        runtime_commit=RUNTIME_COMMIT,
        image_digest="sha256:" + "a" * 64,
        skills_digest="b" * 64,
        environment_digest="c" * 64,
        provider_destinations={},
        secret_files={
            name: name
            for name in (
                "database_url",
                "broker_capability_key",
                "master_key",
                "s3_access_key",
                "s3_secret_key",
            )
        },
    )

    create_dispatch_app(identity)
    result = broker._persist(db, run.run_id, content, "application/octet-stream")

    assert result.project_id == project_id
    assert calls == [(db, project_id, content, "application/octet-stream")]


_EGRESS = "scientist-platform-egress"


def _egress_config(egress):
    from dataclasses import replace
    return replace(_config(), egress_network=egress)


@pytest.mark.parametrize("name", ["bridge", "host", "none", "scientist-run-x", "scientist-platform-services", "scientist-platform-egress-UP", "scientist-platform-egress-" + "a" * 41])
def test_dispatch_config_rejects_non_allowlisted_egress_network(name):
    with pytest.raises(ValueError):
        _egress_config(name)


@pytest.mark.parametrize("name", ["scientist-b5-egress-test", _EGRESS, _EGRESS + "-abc-1"])
def test_dispatch_config_accepts_owned_egress_network(name):
    assert _egress_config(name).egress_network == name


_SERVICES = "scientist-b5-services-test"


def _fake_docker(calls, *, egress=("bridge|false|b5|false"), attached=(_SERVICES,), subnet="172.30.0.0/24", created="c" * 64):
    def docker(*args):
        calls.append(args)
        if args[0] == "create":
            return created
        if args[:2] == ("network", "inspect"):
            if args[-1] == _SERVICES:
                return subnet + " "
            return egress
        if args[0] == "inspect" and "NetworkSettings" in args[2]:
            return json.dumps({name: {} for name in attached})
        return ""
    return docker


def _start_calls(monkeypatch, config, **fake):
    runtime = DockerDispatchRuntime(config)
    calls = []
    monkeypatch.setattr(runtime, "engine_id", lambda: "engine-1")
    monkeypatch.setattr(runtime, "_verified_image_id", lambda: "sha256:" + "b" * 64)
    monkeypatch.setattr(runtime, "_verify_container_image", lambda *a: None)
    monkeypatch.setattr(runtime, "_materialize_launch_config", lambda *a, **k: calls.append(("materialize", k)) or "/tmp/launch.json")
    monkeypatch.setattr(runtime, "_secret_mounts", lambda: [])
    monkeypatch.setattr(runtime, "_ensure_run_network", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "_ref", lambda *a, **k: "ref")
    fake.pop("existing", None)
    monkeypatch.setattr(runtime, "find", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "_docker", _fake_docker(calls, **fake))
    return runtime, calls


def _run_start(runtime, existing=None):
    try:
        runtime.start(None, uuid4(), 1, "scientist-run-aaaaaaaaaaaa-g1", "172.29.42.2", uuid4(), uuid4(), before_mutation=lambda *a: None)
    except RuntimeError:
        pass  # post-start identity inspection is not the subject here


def test_dispatch_create_attaches_egress_network_only_when_configured(monkeypatch):
    runtime, calls = _start_calls(monkeypatch, _egress_config(_EGRESS))
    _run_start(runtime)
    create = next(c for c in calls if c[0] == "create")
    assert create.count("--network") == 1 and create[create.index("--network") + 1] == _SERVICES
    assert not any(_EGRESS in part for part in create)
    connect = ("network", "connect", _EGRESS, "c" * 64)
    order = [c[0] for c in calls]
    assert order.index("create") < calls.index(connect) < order.index("start")


@pytest.mark.parametrize("egress", ["overlay|false|b5|false", "bridge|true|b5|false", "bridge|false||false", "bridge|false|b5|true", "bridge|false|b5|"])
def test_dispatch_refuses_unsafe_egress_network_before_any_mutation(monkeypatch, egress):
    runtime, calls = _start_calls(monkeypatch, _egress_config(_EGRESS), egress=egress)
    _run_start(runtime)
    assert not any(c[0] in {"create", "start"} or c[:2] == ("network", "connect") for c in calls)


def test_dispatch_launch_config_receives_service_subnet(monkeypatch):
    runtime, calls = _start_calls(monkeypatch, _egress_config(_EGRESS))
    _run_start(runtime)
    assert next(c for c in calls if c[0] == "materialize")[1]["service_subnet"] == "172.30.0.0/24"


def _existing_ref():
    return ExecutorRef(uuid4(), uuid4(), 1, "dispatch", None, uuid4(), "engine-1", "d" * 64)


def _restart(monkeypatch, config, attached):
    ref = _existing_ref()
    runtime, calls = _start_calls(monkeypatch, config, existing=ref, attached=attached)
    monkeypatch.setattr(runtime, "find", lambda *a, **k: ref)
    try:
        runtime.start(None, ref.run_id, 1, "scientist-run-aaaaaaaaaaaa-g1", "172.29.42.2", ref.executor_id, ref.process_incarnation, before_mutation=lambda *a: None)
    except RuntimeError:
        pass
    return ref, calls


def test_dispatch_restart_connects_missing_egress_before_start(monkeypatch):
    ref, calls = _restart(monkeypatch, _egress_config(_EGRESS), (_SERVICES,))
    connect = ("network", "connect", _EGRESS, ref.container_id)
    assert calls.index(connect) < calls.index(("start", ref.container_id))


def test_dispatch_restart_disconnects_unconfigured_egress_before_start(monkeypatch):
    ref, calls = _restart(monkeypatch, _config(), (_SERVICES, _EGRESS))
    disconnect = ("network", "disconnect", _EGRESS, ref.container_id)
    assert calls.index(disconnect) < calls.index(("start", ref.container_id))


def test_dispatch_restart_with_matching_egress_changes_nothing(monkeypatch):
    ref, calls = _restart(monkeypatch, _egress_config(_EGRESS), (_SERVICES, _EGRESS))
    assert not any(c[:2] in {("network", "connect"), ("network", "disconnect")} for c in calls)


def test_worker_create_has_single_run_network_and_no_egress(monkeypatch):
    from types import SimpleNamespace
    from scientist import supervisor
    engine = supervisor.DockerWorkerEngine()
    image_id = "sha256:" + "a" * 64
    commands = []
    monkeypatch.setattr(supervisor, "_require_config", lambda: SimpleNamespace(image=image_id, image_digest=image_id))
    monkeypatch.setattr(engine, "engine_id", lambda: "owned-engine")

    def docker(*args, **kwargs):
        commands.append(args)
        if args[:2] == ("image", "inspect"):
            return image_id + "|[]"
        if args[0] == "create":
            return "b" * 64
        return image_id
    monkeypatch.setattr(engine, "_docker", docker)
    try:
        engine.create_worker(image_id, uuid4(), 1, uuid4(), uuid4(), "scientist-run-aaaaaaaaaaaa-g1", "http://172.29.32.2:8123")
    except Exception:
        pass
    create = next(c for c in commands if c[0] == "create")
    assert create.count("--network") == 1
    assert create[create.index("--network") + 1].startswith("scientist-run-")
    assert not any("egress" in part for part in create)


def test_dispatch_without_egress_network_has_no_egress(monkeypatch):
    runtime, calls = _start_calls(monkeypatch, _config())
    _run_start(runtime)
    assert not any("egress" in str(part) for call in calls for part in call)
    assert any(c[0] == "start" for c in calls) and not any(c[:2] == ("network", "inspect") for c in calls)


def test_launch_config_carries_service_subnet_and_scram_default():
    base = dict(schema_version=1, run_id=uuid4(), generation=1, executor_id=uuid4(), process_incarnation=uuid4(), engine_id="e",
                runtime_commit=RUNTIME_COMMIT, image_digest="sha256:" + "a" * 64, skills_digest="b" * 64, environment_digest="c" * 64,
                provider_destinations={}, secret_files={n: n for n in __import__("scientist.dispatch_runtime", fromlist=["x"])._SECRET_NAMES})
    identity = DispatchIdentity(**base, service_subnet="172.30.0.0/24")
    assert identity.service_subnet == "172.30.0.0/24" and identity.db_require_auth == "scram-sha-256"
    for bad in ("0.0.0.0/0", "fd00::/64", "not-a-net", "172.30.0.5/24"):
        with pytest.raises(Exception):
            DispatchIdentity(**base, service_subnet=bad)


def _pin(db_url, resolved, subnet="172.30.0.0/24"):
    return private_dispatch_entrypoint._pin_service_hosts(
        subnet, db_url, "scientist-minio", 9000, "scram-sha-256", resolver=lambda host, port: resolved[host])


def test_service_hosts_resolving_inside_subnet_are_pinned_to_ips():
    resolved = {"scientist-minio": ["172.30.0.5"], "scientist-postgres": ["172.30.0.6"]}
    url, endpoint = _pin("postgresql+psycopg://scientist-postgres/scientist", resolved)
    assert endpoint == "http://172.30.0.5:9000"
    assert "hostaddr=172.30.0.6" in url and "//scientist-postgres/" in url and "require_auth=scram-sha-256" in url


@pytest.mark.parametrize("minio,postgres", [("8.8.8.8", "172.30.0.6"), ("172.30.0.5", "8.8.8.8"), ("172.30.1.5", "172.30.0.6")])
def test_service_hosts_outside_subnet_refuse_startup(minio, postgres):
    with pytest.raises(RuntimeError):
        _pin("postgresql+psycopg://scientist-postgres/scientist", {"scientist-minio": [minio], "scientist-postgres": [postgres]})


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://a,b/scientist", "postgresql+psycopg:///scientist?host=a,b", "postgresql+psycopg:///scientist?host=/var/run/postgresql",
    "postgresql+psycopg://scientist-postgres/scientist?hostaddr=8.8.8.8", "postgresql+psycopg:///scientist"])
def test_service_hosts_refuse_multi_host_socket_or_preset_hostaddr(url):
    with pytest.raises(RuntimeError):
        _pin(url, {"scientist-minio": ["172.30.0.5"], "scientist-postgres": ["172.30.0.6"], "a": ["172.30.0.7"], "b": ["172.30.0.8"]})


def test_service_host_with_any_outside_address_is_refused():
    with pytest.raises(RuntimeError):
        _pin("postgresql+psycopg://scientist-postgres/scientist", {"scientist-minio": ["172.30.0.5", "8.8.8.8"], "scientist-postgres": ["172.30.0.6"]})


def test_db_require_auth_is_only_scram():
    base = dict(schema_version=1, run_id=uuid4(), generation=1, executor_id=uuid4(), process_incarnation=uuid4(), engine_id="e",
                runtime_commit=RUNTIME_COMMIT, image_digest="sha256:" + "a" * 64, skills_digest="b" * 64, environment_digest="c" * 64,
                provider_destinations={}, secret_files={n: n for n in __import__("scientist.dispatch_runtime", fromlist=["x"])._SECRET_NAMES})
    for bad in ("md5", "password", "none", "gss"):
        with pytest.raises(Exception):
            DispatchIdentity(**base, db_require_auth=bad)


def _run_probe(monkeypatch, tmp_path, answer, subnet="172.30.0.0/24"):
    """Execute the real readiness-probe source against a stub psycopg and fake mounted files."""
    import builtins, socket as socket_module, types
    from scientist import dispatch_runtime
    connected = []

    class Connection:
        @staticmethod
        async def connect(url, **kwargs):
            connected.append(url)
            raise RuntimeError("stop after connect")

    stub = types.ModuleType("psycopg")
    stub.AsyncConnection = Connection
    monkeypatch.setitem(sys.modules, "psycopg", stub)
    files = {"/run/scientist/secrets/database_url": "postgresql+psycopg://scientist-postgres/scientist",
             "/run/scientist/dispatch/config.json": json.dumps({"service_subnet": subnet, "db_require_auth": "scram-sha-256"})}
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda path, *a, **k: __import__("io").StringIO(files[path]) if path in files else real_open(path, *a, **k))
    monkeypatch.setattr(socket_module, "getaddrinfo", lambda host, port, **k: [(0, 0, 0, "", (answer, port))])
    monkeypatch.setattr(sys, "argv", ["probe", "172.29.42.2", "8123", "5", "e", "r", "1", "i", "eng", "c"])
    with pytest.raises(SystemExit):
        exec(compile(dispatch_runtime._READINESS_PROBE, "probe", "exec"), {"__name__": "probe"})
    return connected


def test_readiness_probe_connects_with_pinned_in_subnet_url(monkeypatch, tmp_path):
    (url,) = _run_probe(monkeypatch, tmp_path, "172.30.0.6")
    assert "hostaddr=172.30.0.6" in url and "require_auth=scram-sha-256" in url and url.startswith("postgresql://")


def test_readiness_probe_refuses_outside_subnet_resolution(monkeypatch, tmp_path):
    assert _run_probe(monkeypatch, tmp_path, "8.8.8.8") == []
