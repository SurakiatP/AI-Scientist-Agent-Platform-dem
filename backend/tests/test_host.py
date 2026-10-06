"""H1: the single-process host that composes and runs the platform (fakes only; no Docker)."""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from scientist import broker, checkpoints, db as database, host, objects, settings, supervisor
from scientist import profile_preparation, scientific_authority
from scientist.app import create_app
from scientist.contracts import OperationRequest, PlanSpec, Principal
from scientist.db import create_project, create_session, migrate, session
from scientist.dispatch_runtime import _SECRET_NAMES, _parse_template
from scientist.domain import approve_run, revise_plan, submit_run
from scientist.runtime_contracts import RUNTIME_COMMIT, RuntimeContextV1

PROVIDER = "11111111-1111-4111-8111-111111111111"
ORIGIN = "https://research.example"
WORKER_DIGEST = "sha256:" + "a" * 64
DISPATCH_DIGEST = "sha256:" + "c" * 64
QUESTION = "What does the approved evidence say?"


class FakeEngine:
    def __init__(self, engine_id="engine-1", running=(), fail=False):
        self._id, self.running, self.fail = engine_id, set(running), fail

    def engine_id(self):
        return self._id

    def running_worker_containers(self):
        if self.fail:
            raise RuntimeError("listing unavailable")
        return set(self.running)


class FakeS3:
    def __init__(self):
        self.heads = []

    def head_bucket(self, Bucket):
        self.heads.append(Bucket)


@pytest.fixture
def iso(monkeypatch):
    """Per-test schema, so global claim/reap queries only see this test's runs."""
    schema = f"host_{uuid4().hex}"
    parent = database.engine()
    with parent.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(parent.url, connect_args={"options": f"-csearch_path={schema}"}, poolclass=NullPool)
    migrate(engine)
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(database, "_sessions", database.sessionmaker(engine, expire_on_commit=False))
    yield engine
    engine.dispose()
    with parent.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.fixture(autouse=True)
def restore_globals(monkeypatch):
    for module, names in ((supervisor, ["_config"]), (checkpoints, ["_trusted_pins"]),
                          (profile_preparation, ["_builder", "_evidence_key", "_expected_image_digest"]),
                          (scientific_authority, ["_bundle_root"]),
                          (objects, ["_configured_client", "BUCKET"]),
                          (broker, ["_capability_key", "_provider_destinations", "_resolver", "_transport",
                                    "_persist_result", "_peer_destinations", "_dispatch_is_inactive"])):
        for name in names:
            monkeypatch.setattr(module, name, getattr(module, name))
    monkeypatch.setattr(supervisor, "_config", None)
    monkeypatch.setattr(host, "_destinations", {})
    monkeypatch.setenv("SCIENTIST_MASTER_KEY_FILE", "unset")
    monkeypatch.setattr(settings, "provider_destinations", lambda: {PROVIDER: ORIGIN})


def _config_dict(tmp_path):
    secrets_dir, state_dir = tmp_path / "secrets", tmp_path / "state"
    for directory in (secrets_dir, state_dir):
        directory.mkdir()
        directory.chmod(0o700)
    for name in _SECRET_NAMES:
        value = b"test-only-capability-key-32-bytes!" if name == 'broker_capability_key' else f"value-of-{name}".encode()
        (secrets_dir / name).write_bytes(value + b"\n")
        (secrets_dir / name).chmod(0o600)
    return {
        "schema_version": 1, "database_url": "postgresql+psycopg:///scientist_test_h1?host=/tmp&port=54329",
        "listen_port": 18080, "expected_engine_id": "engine-1",
        "worker_image": f"registry.local/worker@{WORKER_DIGEST}", "dispatch_image": f"registry.local/dispatch@{DISPATCH_DIGEST}",
        "service_network": "scientist-platform-services", "skills_digest": "b" * 64, "environment_digest": "d" * 64,
        "s3_endpoint": "http://127.0.0.1:9000", "bucket": "scientist",
        "secrets_dir": str(secrets_dir), "state_dir": str(state_dir),
        "max_active": 3, "poll_seconds": 0.2, "provider_destinations": {PROVIDER: ORIGIN},
    }


def _write(tmp_path, data, name="host.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


def _compose(tmp_path, engine=None):
    cfg = host.load_config(_write(tmp_path, _config_dict(tmp_path)))
    s3 = FakeS3()
    return cfg, s3, host.compose(cfg, engine=engine or FakeEngine(), s3=s3)


def _approved_run(token_limit=1000):
    with session() as db:
        owner = Principal(identity=uuid4(), kind="owner")
        project_id = create_project(db, "host test")
        chat = create_session(db, project_id, "host test")
        run = submit_run(db, owner, project_id, chat, uuid4().hex, QUESTION, [], PROVIDER, "fixture")
        snapshot = db.execute(text("SELECT digest FROM input_snapshots WHERE run_id=:r"), {"r": run.run_id}).scalar_one().strip()
        plan = PlanSpec(input_snapshot_digest=snapshot, provider_id=PROVIDER, model="fixture", stages=["search"],
                        allowed_ops=["llm"], data_recipients=[ORIGIN], packages=[],
                        token_limit=token_limit, elapsed_limit_ms=60000)
        run = revise_plan(db, owner, run.run_id, run.revision, plan)
        approve_run(db, owner, run.run_id, run.revision, run.plan_digest)
        db.commit()
        return run.run_id


def _unapproved_run():
    with session() as db:
        owner = Principal(identity=uuid4(), kind="owner")
        project_id = create_project(db, "host test")
        run = submit_run(db, owner, project_id, create_session(db, project_id, "s"), uuid4().hex, QUESTION, [], PROVIDER, "fixture")
        db.commit()
        return run.run_id, owner


def _sql(sql, **params):
    with session() as db:
        rows = db.execute(text(sql), params)
        result = rows.mappings().all() if rows.returns_rows else None
        db.commit()
        return result


def _state(run_id):
    return _sql("SELECT state, waiting_reason, reserved_tokens FROM runs WHERE id=:r", r=run_id)[0]


@pytest.fixture
def recorders(monkeypatch):
    starts, recovers = [], []
    monkeypatch.setattr(supervisor, "start", lambda db, run, gen: starts.append((run, gen)) or "container")
    monkeypatch.setattr(supervisor, "recover", lambda db, run, **kw: recovers.append(run))
    return starts, recovers


def _make_running(run_id, container, *, expired=False):
    _sql("UPDATE runs SET state='running', generation=1, lease_expires_at=now() + (:d * interval '1 minute') WHERE id=:r",
         r=run_id, d=-5 if expired else 5)
    if container:
        _sql("""INSERT INTO runtime_executors (id, run_id, generation, kind, process_incarnation, container_id, engine_id, state)
                VALUES (:i, :r, 1, 'worker', :p, :c, 'engine-1', 'active')""",
             i=uuid4(), r=run_id, p=uuid4(), c=container)


# 1
def test_compute_tick_schedules_only_current_reserved_compute_operations(monkeypatch, iso):
    run_id = _approved_run()
    operation_id = uuid4().hex
    request = OperationRequest(
        run_id=run_id,
        generation=1,
        operation_id=operation_id,
        kind="compute",
        payload={"grant_id": "g", "grant": {}},
        reserve_tokens=0,
    )
    with database.session() as db:
        db.execute(text("UPDATE runs SET state='running', generation=1 WHERE id=:run"), {"run": run_id})
        db.execute(text("""
            INSERT INTO operations (id, run_id, operation_id, generation, kind, payload_hash, state,
                                    reserve_tokens, usage_tokens, result)
            VALUES (:id, :run, :operation, 1, 'compute', :hash, 'reserved', 0, 0,
                    CAST(:result AS jsonb))
        """), {
            "id": uuid4(), "run": run_id, "operation": operation_id,
            "hash": "a" * 64,
            "result": json.dumps({"request": request.model_dump(mode="json")}),
        })
        db.commit()
    monkeypatch.setattr(host, "_compute_engine", object())
    monkeypatch.setattr(host, "_compute_image_digest", "sha256:" + "d" * 64)
    scheduled = []
    runner = host.Host(FakeEngine())
    monkeypatch.setattr(runner, "_run_compute_operation", lambda value: scheduled.append(value.operation_id), raising=False)

    runner._compute_tick()
    runner._compute_futures[(run_id, operation_id)].result(timeout=2)

    assert scheduled == [operation_id]
    runner.close()


def test_config_rejects_unsafe_values(tmp_path):
    good = _config_dict(tmp_path)
    assert host.load_config(_write(tmp_path, good)).listen_port == 18080

    def bad(**change):
        data = {**good, **change}
        with pytest.raises(host.ConfigError):
            host.load_config(_write(tmp_path, {k: v for k, v in data.items() if v is not ...}, "bad.json"))

    bad(worker_image="registry.local/worker:latest")
    bad(dispatch_image="registry.local/dispatch:latest")
    bad(database_url="postgresql+psycopg://user:secret@localhost/scientist")
    bad(unknown_key=1)
    bad(max_active=4)
    bad(s3_endpoint="http://10.0.0.5:9000")
    bad(s3_endpoint="https://127.0.0.1:9000")
    bad(listen_port=80)
    bad(database_url="postgresql+psycopg:///scientist?host=/tmp&password=secret")
    bad(database_url="postgresql+psycopg://db.example/scientist")
    bad(database_url="postgresql+psycopg://localhost/scientist?host=db.example.com")  # query host wins over URL host
    bad(database_url="postgresql+psycopg:///scientist?host=/tmp,db.example.com")  # libpq host list
    bad(database_url="not a url")
    bad(provider_destinations={})
    bad(provider_destinations={PROVIDER: "https://other.example"})
    bad(provider_destinations={str(uuid4()): ORIGIN})
    Path = type(tmp_path)
    secrets_dir = Path(good["secrets_dir"])
    secrets_dir.chmod(0o755)
    bad()
    secrets_dir.chmod(0o700)
    (secrets_dir / "master_key").unlink()
    bad()
    with pytest.raises(host.ConfigError) as caught:
        host.load_config(_write(tmp_path, {**good, "provider_destinations": {PROVIDER: "https://leaked-value.example"}}, "x.json"))
    assert caught.value.fields == ["provider_destinations"] and "leaked-value" not in repr(caught.value.__dict__)
    big = tmp_path / "big.json"
    big.write_text(" " * (64 * 1024 + 1))
    with pytest.raises(host.ConfigError):
        host.load_config(big)


# 2
def test_compose_configures_runtime(tmp_path):
    cfg, s3, engine = _compose(tmp_path)
    assert profile_preparation._expected_image_digest is None
    assert profile_preparation._expected_image_digests == {
        "prof.worker-base@py3.14.7": WORKER_DIGEST,
    }
    assert supervisor._config is not None and supervisor._config.engine is engine
    assert supervisor._config.image == f"registry.local/worker@{WORKER_DIGEST}"
    assert broker._dispatch_inactivity_proof is not None
    assert broker._capability_key == b"test-only-capability-key-32-bytes!"
    assert broker._provider_destinations == {PROVIDER: ORIGIN}
    assert objects._configured_client is s3 and s3.heads == ["scientist"]
    assert os.environ["SCIENTIST_MASTER_KEY_FILE"] == str(cfg.secrets_dir / "master_key")
    template = cfg.state_dir / "dispatch-template.json"
    assert stat.S_IMODE(template.stat().st_mode) == 0o444
    parsed = _parse_template(template.read_bytes())
    assert parsed.image_digest == WORKER_DIGEST and dict(parsed.provider_destinations) == {__import__("uuid").UUID(PROVIDER): ORIGIN}
    assert supervisor._config.dispatch.config.launcher_dir == str(cfg.state_dir / "launches")
    assert host._destinations == {PROVIDER: ORIGIN}


def test_compose_preserves_configured_bucket_in_dispatch_template(tmp_path):
    values = _config_dict(tmp_path)
    values["bucket"] = "tenant-results"
    cfg = host.load_config(_write(tmp_path, values))

    host.compose(cfg, engine=FakeEngine(), s3=FakeS3())

    template = _parse_template((cfg.state_dir / "dispatch-template.json").read_bytes())
    assert template.bucket == "tenant-results"


@pytest.mark.parametrize("bucket", ["", "has/slash", "x" * 64, 42, None])
def test_host_rejects_invalid_bucket(tmp_path, bucket):
    values = _config_dict(tmp_path)
    values["bucket"] = bucket
    with pytest.raises(ValidationError):
        host.HostConfig.model_validate(values)


def test_compose_refuses_engine_mismatch(tmp_path, monkeypatch):
    cfg = host.load_config(_write(tmp_path, _config_dict(tmp_path)))
    with pytest.raises(host.HostError):
        host.compose(cfg, engine=FakeEngine(engine_id="other"), s3=FakeS3())
    assert supervisor._config is None
    assert not (cfg.state_dir / "dispatch-template.json").exists()


# 3
def test_rest_stop_served_over_real_http(iso, tmp_path):
    _compose(tmp_path)
    run_id, _ = _unapproved_run()
    token = "t" * 40
    server = host.make_server(create_app(bootstrap_token=token), 0)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        address = server.servers[0].sockets[0].getsockname()
        assert address[0] == "127.0.0.1"
        base = f"http://127.0.0.1:{address[1]}"
        with httpx.Client(base_url=base, headers={"Origin": base}) as client:
            boot = client.post("/api/v1/bootstrap", json={"token": token})
            assert boot.status_code == 200
            client.headers["x-csrf-token"] = boot.json()["csrf_token"]
            stopped = client.post(f"/api/v1/runs/{run_id}/stop")
            assert stopped.status_code == 200 and stopped.json()["state"] == "canceled"
            supervisor._config = None  # not composed: the same call is refused
            refused = client.post(f"/api/v1/runs/{run_id}/stop")
            assert refused.status_code == 503 and refused.json()["code"] == "runtime_unavailable"
    finally:
        server.should_exit = True
        thread.join(10)


# 4
def test_tick_repolls_after_budget_wait(iso, recorders):
    starts, _ = recorders
    run_a, run_b = _approved_run(), _approved_run()
    _sql("UPDATE runs SET token_limit=0 WHERE id=:r", r=run_a)
    host.Host(FakeEngine(), max_active=3).tick()
    assert [run for run, _ in starts] == [run_b]
    waiting = _state(run_a)
    assert (waiting["state"], waiting["waiting_reason"]) == ("waiting_input", "budget_exhausted")
    assert _sql("SELECT budget_decision_id FROM runs WHERE id=:r", r=run_a)[0]["budget_decision_id"] is not None


# 5
def test_tick_respects_max_active(iso, recorders):
    starts, recovers = recorders
    runs = [_approved_run() for _ in range(3)]
    host.Host(FakeEngine(), max_active=2).tick()
    assert [run for run, _ in starts] == runs[:2] and recovers == []
    assert _state(runs[2])["state"] == "queued"
    host.Host(FakeEngine(running={"x"}), max_active=2).tick()  # both active runs hold the slots
    assert len(starts) == 2
    assert set(recovers) == set(runs[:2])  # no worker row: reaped, never a third start


# 6
def test_startup_recovers_only_active_runs(iso, recorders):
    _, recovers = recorders
    states = ["running", "recovering", "stopping", "queued", "waiting_input", "completed", "awaiting_approval"]
    ids = {}
    for state in states:
        ids[state] = _approved_run()
        _sql("UPDATE runs SET state=:s WHERE id=:r", s=state, r=ids[state])
    host.Host(FakeEngine(), max_active=3).startup_recover()
    assert set(recovers) == {ids["running"], ids["recovering"], ids["stopping"]}


# 7
def test_host_ticks_peer_reconciliation_at_startup_and_periodically(iso, monkeypatch):
    calls = []
    monkeypatch.setattr(
        host.peer_reconciliation_supervisor,
        "tick",
        lambda db, *, max_active, startup=False: calls.append((max_active, startup)) or 0,
    )
    loop = host.Host(FakeEngine(), max_active=2)

    loop.startup_recover()
    loop.reap()

    assert calls == [(2, True), (2, False)]


def test_reap_recovers_exited_or_expired_only(iso, recorders):
    starts, recovers = recorders
    alive, exited, expired = _approved_run(), _approved_run(), _approved_run()
    _make_running(alive, "a" * 64)
    _make_running(exited, "b" * 64)
    _make_running(expired, "c" * 64, expired=True)
    engine = FakeEngine(running={"a" * 64, "c" * 64})
    host.Host(engine, max_active=3).tick()
    assert sorted(recovers, key=str) == sorted([exited, expired], key=str) and starts == []
    recovers.clear()
    engine.fail = True  # liveness unknown: never guess on a valid lease
    host.Host(engine, max_active=3).tick()
    assert recovers == [expired]


# 8
def test_tick_never_resolves_unknown_or_resends(iso, recorders):
    starts, recovers = recorders
    run_id = _approved_run()
    decision = uuid4()
    _sql("UPDATE runs SET state='waiting_input', waiting_reason='unknown_outcome', generation=1, reserved_tokens=5 WHERE id=:r", r=run_id)
    _sql("""INSERT INTO operations (id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens)
            VALUES (:i, :r, 'op-1', 1, 'llm', :h, 'unknown', 5)""", i=uuid4(), r=run_id, h="0" * 64)
    _sql("""INSERT INTO owner_decisions (decision_id, run_id, operation_id, reason) VALUES (:d, :r, 'op-1', 'unknown_outcome')""",
         d=decision, r=run_id)
    tick = host.Host(FakeEngine(), max_active=3)
    for _ in range(3):
        tick.tick()
    assert starts == [] and recovers == []
    assert _sql("SELECT state FROM operations WHERE run_id=:r", r=run_id)[0]["state"] == "unknown"
    assert _state(run_id)["reserved_tokens"] == 5
    assert _sql("SELECT state FROM owner_decisions WHERE decision_id=:d", d=decision)[0]["state"] == "pending"


# 9
def test_repeated_start_failure_parks_run(iso, monkeypatch):
    calls = []

    def failing_start(db, run, gen):
        calls.append(run)
        db.execute(text("UPDATE runs SET state='queued' WHERE id=:r"), {"r": run})  # what recover() does after a failed launch
        db.commit()
        raise RuntimeError("launch failed")

    monkeypatch.setattr(supervisor, "start", failing_start)
    monkeypatch.setattr(supervisor, "recover", lambda db, run, **kw: None)
    run_id = _approved_run()
    ticker = host.Host(FakeEngine(), max_active=3)
    for _ in range(5):
        ticker.tick()
    assert calls == [run_id] * 3
    parked = _state(run_id)
    assert (parked["state"], parked["waiting_reason"]) == ("waiting_input", "runtime_start_failed")
    kinds = _sql("SELECT kind, payload FROM events WHERE run_id=:r ORDER BY sequence DESC LIMIT 1", r=run_id)[0]
    assert kinds["kind"] == "run.state" and kinds["payload"]["state"] == "waiting_input"


# 10
def test_second_host_refused():
    first = host.acquire_singleton()
    try:
        with pytest.raises(host.HostError):
            host.acquire_singleton()
    finally:
        first.close()
    host.acquire_singleton().close()


# 11
def test_bootstrap_token_private_not_logged(tmp_path, capsys):
    token = "secret-token-" + "x" * 30
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    (state_dir / "owner-bootstrap.url").write_text("stale")
    path = host.publish_bootstrap(state_dir, 18080, token)
    assert path == state_dir / "owner-bootstrap.url"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text().strip() == f"http://127.0.0.1:18080/#bootstrap={token}"
    out = capsys.readouterr()
    assert token not in out.out + out.err and "host.ready" in out.err and "18080" in out.err


# 12
def test_fresh_bootstrap_only_without_prior_state(iso, monkeypatch):
    run_id = _approved_run()
    _sql("UPDATE runs SET state='running', generation=1 WHERE id=:r", r=run_id)
    monkeypatch.setattr(supervisor, "_config", SimpleNamespace(
        image_digest=WORKER_DIGEST, skills_digest="b" * 64, environment_digest="d" * 64, runtime_commit=RUNTIME_COMMIT))
    monkeypatch.setattr(host, "_destinations", {PROVIDER: ORIGIN})
    with session() as db:
        boot = host.bootstrap(db, run_id, 1)
    context = RuntimeContextV1.model_validate_json(boot.context)
    assert [m.content for m in context.messages] == [QUESTION]
    assert context.provider_endpoint == ORIGIN and str(context.provider_id) == PROVIDER
    assert (context.image_digest, context.skills_digest, context.environment_digest) == (WORKER_DIGEST, "b" * 64, "d" * 64)
    assert context.system_prompt == host.SYSTEM_PROMPT and boot.metadata.checkpoint_revision == 0 and not list(boot.workspace)
    assert context.system_prompt.startswith(
        "You are a careful research assistant. Answer the owner's approved question using only evidence "
        "you can verify through the approved plan. State uncertainty plainly and never invent sources.")
    for phrase in ("Thai", "abstracts", "full texts", "tool result", "limitations"):
        assert phrase in context.system_prompt

    _sql("""INSERT INTO operations (id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens)
            VALUES (:i, :r, 'op-1', 1, 'llm', :h, 'committed', 1)""", i=uuid4(), r=run_id, h="0" * 64)
    seen = []
    monkeypatch.setattr(supervisor, "continuation_bootstrap", lambda db, run, gen, controller: seen.append((run, gen, controller)) or "continued")
    with session() as db:
        assert host.bootstrap(db, run_id, 1) == "continued"  # generation 1 alone is not "fresh"
    assert seen[0][:2] == (run_id, 1) and seen[0][2].pins.image_digest == WORKER_DIGEST

    def refuse(*args):
        raise RuntimeError("checkpoint integrity")

    monkeypatch.setattr(supervisor, "continuation_bootstrap", refuse)
    _sql("UPDATE runs SET generation=2 WHERE id=:r", r=run_id)
    with session() as db, pytest.raises(RuntimeError, match="checkpoint integrity"):
        host.bootstrap(db, run_id, 2)


def _stopping_or_waiting(run_id, state, reason=None, cancel=False):
    _sql("UPDATE runs SET state=:s, waiting_reason=:w, cancel_requested=:c, generation=1, lease_expires_at=now() - interval '5 minutes' WHERE id=:r",
         s=state, w=reason, c=cancel, r=run_id)


# review 2: the snapshot can go stale between the read and recover()
def test_reap_rechecks_state_under_lock(iso, recorders):
    _, recovers = recorders
    run_id = _approved_run()
    _make_running(run_id, "a" * 64, expired=True)

    class Racing(FakeEngine):
        def running_worker_containers(self):
            _stopping_or_waiting(run_id, "waiting_input", "unknown_outcome")  # broker parked the run after the snapshot
            return set()

    host.Host(Racing(), max_active=3).tick()
    assert recovers == []
    assert _state(run_id)["state"] == "waiting_input"


# review 3: a stop that died after committing 'stopping' must not hold a slot forever
def test_reap_recovers_stuck_stopping_run(iso, recorders):
    _, recovers = recorders
    stuck, plain = _approved_run(), _approved_run()
    _stopping_or_waiting(stuck, "stopping", cancel=True)
    _sql("UPDATE runs SET state='stopping', cancel_requested=false, generation=1 WHERE id=:r", r=plain)
    host.Host(FakeEngine(), max_active=3).tick()
    assert recovers == [stuck]


# review 8: losing the singleton lock stops the host
def test_lock_loss_stops_work(iso, recorders):
    starts, recovers = recorders
    _approved_run()
    lost = []

    class DeadLock:
        def execute(self, *args):
            raise RuntimeError("connection closed")

    h = host.Host(FakeEngine(), max_active=3, lock=DeadLock(), on_lost=lambda: lost.append(1))
    h.tick()
    h.tick()
    assert h.lost and lost == [1] and starts == [] and recovers == []


# review 4: launcher directory must be private
def test_compose_refuses_open_launcher_dir(tmp_path):
    cfg = host.load_config(_write(tmp_path, _config_dict(tmp_path)))
    (cfg.state_dir / "launches").mkdir(mode=0o755)
    (cfg.state_dir / "launches").chmod(0o755)
    with pytest.raises(host.HostError):
        host.compose(cfg, engine=FakeEngine(), s3=FakeS3())
    assert supervisor._config is None


# review 9: unhandled ASGI exceptions log only the exception type
def test_server_logs_exception_type_only(monkeypatch):
    import logging
    host.make_server(object(), 0)
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = Capture()
    logger = logging.getLogger("uvicorn.error")
    logger.addHandler(handler)
    try:
        try:
            raise ValueError("secret-sql-param")
        except ValueError:
            logger.error("Exception in ASGI application\n", exc_info=True)
    finally:
        logger.removeHandler(handler)
    assert records and "ValueError" in records[0] and "secret-sql-param" not in records[0] and "Traceback" not in records[0]


class _FakeServer:
    def __init__(self, order, on_run):
        self.order, self.on_run, self.should_exit = order, on_run, False
        self.config = SimpleNamespace(bind_socket=lambda: order.append("bind") or "sock")

    def run(self, sockets=None):
        assert sockets == ["sock"]
        self.order.append("run")
        self.on_run(self)


def _main_fakes(monkeypatch, tmp_path, on_run, *, compose=None):
    order, closed = [], []
    cfg = SimpleNamespace(database_url="postgresql+psycopg:///x", state_dir=tmp_path, listen_port=18080, max_active=3, poll_seconds=0.2)
    lock = SimpleNamespace(close=lambda: closed.append(1), execute=lambda *a: None)
    monkeypatch.setattr(host, "load_config", lambda path: cfg)
    monkeypatch.setattr(host, "acquire_singleton", lambda: lock)
    monkeypatch.setattr(database, "migrate", lambda *a: None)
    monkeypatch.setattr(host, "compose", compose or (lambda cfg: FakeEngine()))
    monkeypatch.setattr(host, "make_server", lambda app, port: _FakeServer(order, on_run))
    monkeypatch.setattr(host, "publish_bootstrap", lambda *a: order.append("publish"))
    monkeypatch.setattr(host.Host, "startup_recover", lambda self: order.append("recover"))
    monkeypatch.setattr(database, "DATABASE_URL", database.DATABASE_URL)
    monkeypatch.setenv("SCIENTIST_DATABASE_URL", os.environ["SCIENTIST_DATABASE_URL"])
    return order, closed


# review 1 + 7
def test_main_handles_sigterm_and_serves_after_bind(iso, tmp_path, monkeypatch):
    import signal
    saved = {n: signal.getsignal(n) for n in (signal.SIGTERM, signal.SIGINT)}
    seen = []

    def serve(server):
        seen.append(signal.getsignal(signal.SIGTERM))
        signal.raise_signal(signal.SIGTERM)  # run() must be able to return and let main's finally run

    order, closed = _main_fakes(monkeypatch, tmp_path, serve)
    try:
        assert host.main(["--config", "x"]) == 0
    finally:
        for number, handler in saved.items():
            signal.signal(number, handler)
    assert seen[0] not in (signal.SIG_DFL, signal.SIG_IGN, None)
    assert order == ["bind", "recover", "publish", "run"] and closed == [1]
    assert [t for t in threading.enumerate() if t.name == "scientist-host-loop"] == []


def test_main_exits_nonzero_when_lock_lost(iso, tmp_path, monkeypatch):
    def serve(server):
        deadline = time.monotonic() + 5
        while not server.should_exit and time.monotonic() < deadline:
            time.sleep(0.02)

    order, closed = _main_fakes(monkeypatch, tmp_path, serve)
    monkeypatch.setattr(host, "acquire_singleton", lambda: SimpleNamespace(
        close=lambda: closed.append(1), execute=lambda *a: (_ for _ in ()).throw(RuntimeError("gone"))))
    assert host.main(["--config", "x"]) == 3
    assert closed == [1]


def test_main_compose_failure_logs_stage_and_type_only(iso, tmp_path, monkeypatch, capsys):
    def broken(cfg):
        raise RuntimeError("password=hunter2")

    order, closed = _main_fakes(monkeypatch, tmp_path, lambda s: None, compose=broken)
    assert host.main(["--config", "x"]) == 2
    err = capsys.readouterr().err
    assert json.loads(err.strip().splitlines()[-1]) == {"event": "host.exit", "stage": "compose", "error_type": "RuntimeError"}
    assert "hunter2" not in err and closed == [1] and order == []


def test_main_bind_failure_exits_before_recovery(iso, tmp_path, monkeypatch, capsys):
    order, closed = _main_fakes(monkeypatch, tmp_path, lambda s: None)

    def taken(order=order):
        raise SystemExit(1)  # uvicorn's bind_socket exits when the port is in use

    monkeypatch.setattr(host, "make_server", lambda app, port: _bind_fails(order, taken))
    assert host.main(["--config", "x"]) == 2
    assert order == [] and closed == [1]
    assert json.loads(capsys.readouterr().err.strip().splitlines()[-1]) == {"event": "host.exit", "stage": "bind", "error_type": "SystemExit"}


def _bind_fails(order, taken):
    server = _FakeServer(order, lambda s: None)
    server.config = SimpleNamespace(bind_socket=taken)
    return server


def test_main_signal_handlers_cover_startup_recovery(iso, tmp_path, monkeypatch):
    import signal
    saved = {n: signal.getsignal(n) for n in (signal.SIGTERM, signal.SIGINT)}
    seen = []
    order, closed = _main_fakes(monkeypatch, tmp_path, lambda s: None)
    monkeypatch.setattr(host.Host, "startup_recover", lambda self: seen.append(signal.getsignal(signal.SIGTERM)))
    try:
        assert host.main(["--config", "x"]) == 0
    finally:
        for number, handler in saved.items():
            signal.signal(number, handler)
    assert seen and seen[0] not in (signal.SIG_DFL, signal.SIG_IGN, None)


def test_main_signal_during_startup_recovery_skips_work_and_url(iso, tmp_path, monkeypatch):
    import signal
    saved = {n: signal.getsignal(n) for n in (signal.SIGTERM, signal.SIGINT)}
    order, closed = _main_fakes(monkeypatch, tmp_path, lambda s: order.append("never"))
    monkeypatch.setattr(host.Host, "startup_recover", lambda self: signal.raise_signal(signal.SIGTERM))
    try:
        assert host.main(["--config", "x"]) == 0
    finally:
        for number, handler in saved.items():
            signal.signal(number, handler)
    assert order == ["bind"] and closed == [1]
    assert [t for t in threading.enumerate() if t.name == "scientist-host-loop"] == []


def test_config_rejects_non_allowlisted_egress_network(tmp_path):
    good = _config_dict(tmp_path)
    for name in ("bridge", "host", "none", "scientist-run-x", "scientist-platform-services"):
        with pytest.raises(host.ConfigError):
            host.load_config(_write(tmp_path, {**good, "egress_network": name}, "bad.json"))


def test_compose_passes_egress_network_to_dispatch(tmp_path):
    base = _config_dict(tmp_path)
    cfg = host.load_config(_write(tmp_path, {**base, "egress_network": "scientist-platform-egress"}))
    assert cfg.egress_network == "scientist-platform-egress"
    host.compose(cfg, engine=FakeEngine(), s3=FakeS3())
    assert supervisor._config.dispatch.config.egress_network == "scientist-platform-egress"
    assert host.load_config(_write(tmp_path, base, "none.json")).egress_network is None
