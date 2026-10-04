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
