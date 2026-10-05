from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Barrier, Event
from uuid import UUID, uuid4
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from scientist import broker, peer_receipt_runtime, peer_reconciliation_supervisor, supervisor
from scientist import db as database
from scientist.auth import DomainError
from scientist.peer_reconciliation_config import PeerReconciliationTarget
from scientist.contracts import PeerReleaseSpec, PlanSpec, Principal, canonical_peer_parameters_bytes
from scientist.dispatch_runtime import DockerDispatchRuntime, DispatchServiceConfig
from scientist.runtime_contracts import RUNTIME_COMMIT
from scientist.db import create_project, create_session, session as open_session
from scientist.domain import approve_run, revise_plan, submit_run

_PEER_ENDPOINT = "https://peer.example"


OWNER = Principal(identity=uuid4(), kind="owner")
PROVIDER = uuid4()


def _waiting_peer(db, project_session, *, allow_get_task=True, limit=2, operation_id="peer-op-reconcile"):
    project_id, session_id = project_session
    run = submit_run(db, OWNER, project_id, session_id, uuid4().hex, "Check one approved claim", [], PROVIDER, "fixture-model")
    snapshot = db.execute(text("SELECT digest FROM input_snapshots WHERE run_id=:run"), {"run": run.run_id}).scalar_one().strip()
    message_id = f"msg-{uuid4().hex}"
    parameters = {"message": {"messageId": message_id, "role": "ROLE_USER", "parts": [{"text": "Review selected result."}]}}
    release = PeerReleaseSpec(
        release_id=uuid4(), peer_id=uuid4(), endpoint_fingerprint=sha256(_PEER_ENDPOINT.encode()).hexdigest(),
        purpose="Review selected result.", input_snapshot_digest=snapshot, data_refs=[],
        approved_parameters=parameters,
        parameters_sha256=sha256(canonical_peer_parameters_bytes(parameters)).hexdigest(),
        message_id=message_id, method="SendMessage", allow_get_task=allow_get_task,
        request_bytes_limit=4096, timeout_ms=5000, reserved_tokens=100,
        reconciliation_limit=limit,
    )
    plan = PlanSpec(
        input_snapshot_digest=snapshot, provider_id=PROVIDER, model="fixture-model",
        stages=["peer review"], allowed_ops=["peer"], data_recipients=[], packages=[],
        token_limit=1000, elapsed_limit_ms=30000, peer_releases=[release],
    )
    revised = revise_plan(db, OWNER, run.run_id, run.revision, plan)
    approve_run(db, OWNER, run.run_id, revised.revision, revised.plan_digest)
    db.execute(text("""
        INSERT INTO delegations (id, project_id, owner_identity, peer_id, actions)
        VALUES (:id, :project, :owner, :peer, ARRAY['peer'])
    """), {
        "id": uuid4(), "project": project_id, "owner": OWNER.identity, "peer": release.peer_id,
    })
    db.execute(text("""
        INSERT INTO credentials (id, project_id, label, provider, encrypted_value)
        VALUES (:id, :project, 'peer fixture', :provider, :value)
    """), {
        "id": uuid4(), "project": project_id, "provider": f"peer:{release.peer_id}",
        "value": b"fixture-only-secret",
    })
    broker.configure(peer_destinations={str(release.peer_id): _PEER_ENDPOINT})
    db.execute(text("""INSERT INTO operations
        (id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens)
        VALUES (:id, :run, :operation, 0, 'peer', :hash, 'reserved', 100)"""), {
        "id": uuid4(), "run": run.run_id, "operation": operation_id,
        "hash": sha256(operation_id.encode()).hexdigest(),
    })
    db.execute(text("UPDATE runs SET reserved_tokens=100 WHERE id=:run"), {"run": run.run_id})
    db.commit()
    assert peer_receipt_runtime.prepare_submission(run.run_id, operation_id, release.release_id)
    peer_receipt_runtime.record_remote_identity(run.run_id, operation_id, "remote-task-known", None)
    db.execute(text("UPDATE operations SET state='unknown' WHERE run_id=:run AND operation_id=:operation"), {
        "run": run.run_id, "operation": operation_id,
    })
    db.execute(text("UPDATE operations SET generation=1 WHERE run_id=:run AND operation_id=:operation"), {
        "run": run.run_id, "operation": operation_id,
    })
    db.execute(text("UPDATE runs SET generation=1 WHERE id=:run"), {"run": run.run_id})
    original_executor = uuid4()
    original_container = "a" * 64
    original_incarnation = uuid4()
    db.execute(text("""
        INSERT INTO runtime_executors
            (id, run_id, generation, kind, operation_id, process_incarnation,
             engine_id, container_id, state, proof)
        VALUES (:id, :run, 1, 'dispatch', :operation, :incarnation,
                'fake-engine', :container, 'inactive', CAST(:proof AS jsonb))
    """), {
        "id": original_executor, "run": run.run_id, "operation": operation_id,
            "incarnation": original_incarnation, "container": original_container,
        "proof": json.dumps({
                "source": "owned-engine-exact-container", "engine_id": "fake-engine",
                "container_id": original_container,
                "executor_id": str(original_executor),
                "process_incarnation": str(original_incarnation),
        }),
    })
    db.execute(text("""
        INSERT INTO operation_executors (run_id, operation_id, generation, executor_id)
        VALUES (:run, :operation, 1, :executor)
    """), {"run": run.run_id, "operation": operation_id, "executor": original_executor})
    db.execute(text("UPDATE runs SET state='waiting_input', waiting_reason='unknown_outcome', lease_expires_at=NULL WHERE id=:run"), {
        "run": run.run_id,
    })
    db.commit()
    return run.run_id, operation_id, release


@pytest.fixture
def project_session(db):
    project_id = create_project(db, "peer reconciliation supervisor test")
    return project_id, create_session(db, project_id, "peer reconciliation")


def _fake_launches(db, project_session, monkeypatch, only_run_id):
    run_id = only_run_id
    peer_id = db.execute(text("""
        SELECT receipt.peer_id FROM peer_outbound_receipts AS receipt
        WHERE receipt.run_id=:run
    """), {"run": run_id}).scalar_one()
    broker.configure(peer_destinations={str(peer_id): _PEER_ENDPOINT})
    calls = []

    def launch(db, run_id, generation, executor_id, process_incarnation, target):
        calls.append((run_id, generation, executor_id, process_incarnation, target))
        db.execute(text("""UPDATE runtime_executors
            SET state='active', engine_id='fake-engine', container_id=:container
            WHERE id=:id AND run_id=:run AND generation=:generation"""), {
            "container": sha256(str(executor_id).encode()).hexdigest(), "id": executor_id,
            "run": run_id, "generation": generation,
        })
        db.commit()

    monkeypatch.setattr(supervisor, "start_peer_reconciliation", launch, raising=False)
    monkeypatch.setattr(supervisor, "_fence_generation", lambda db, run_id, generation, grace: True)
    monkeypatch.setattr(peer_reconciliation_supervisor, "_active_count", lambda db: len(calls))
    monkeypatch.setattr(peer_reconciliation_supervisor, "_reap_attempts", lambda db, startup: None)
    next_candidate = peer_reconciliation_supervisor._next_candidate

    def only_current_run(db, excluded=None):
        excluded = set() if excluded is None else excluded
        while True:
            candidate = next_candidate(db, excluded)
            if candidate is None or candidate["run_id"] == only_run_id:
                return candidate
            excluded.add((candidate["run_id"], candidate["operation_id"]))

    monkeypatch.setattr(peer_reconciliation_supervisor, "_next_candidate", only_current_run)
    return calls


def test_tick_consumes_one_attempt_and_creates_dispatch_only_executor(db, project_session, monkeypatch):
    run_id, operation_id, release = _waiting_peer(db, project_session)
    actual_active_count = peer_reconciliation_supervisor._active_count
    active_before = actual_active_count(db)
    calls = _fake_launches(db, project_session, monkeypatch, run_id)
    peer_reconciliation_supervisor.tick(db, max_active=1)

    assert len(calls) == 1
    assert calls[0][0] == run_id
    assert calls[0][4].operation_id == operation_id
    assert calls[0][4].attempt == 1
    run = db.execute(text("SELECT state, waiting_reason, generation FROM runs WHERE id=:run"), {"run": run_id}).one()
    assert (run.state, run.waiting_reason, run.generation) == ("waiting_input", "unknown_outcome", 2)
    receipt = db.execute(text("SELECT reconciliation_attempts FROM peer_outbound_receipts WHERE run_id=:run AND operation_id=:operation"), {
        "run": run_id, "operation": operation_id,
    }).scalar_one()
    assert receipt == 1
    executors = db.execute(text("SELECT kind, operation_id, peer_reconciliation_attempt, peer_reconciliation_started FROM runtime_executors WHERE run_id=:run"), {
        "run": run_id,
    }).all()
    assert len(executors) == 2
    assert all(executor.kind == "dispatch" for executor in executors)
    assert ("dispatch", operation_id, 1, False) in executors
    assert actual_active_count(db) == active_before + 1
    assert db.execute(text("SELECT generation, state FROM operations WHERE run_id=:run AND operation_id=:operation"), {
        "run": run_id, "operation": operation_id,
    }).one() == (1, "unknown")


def test_uncertain_prior_executor_fails_closed_without_consuming_attempt(db, project_session, monkeypatch):
    run_id, operation_id, _ = _waiting_peer(db, project_session)
    calls = _fake_launches(db, project_session, monkeypatch, run_id)
    monkeypatch.setattr(supervisor, "_fence_generation", lambda db, run_id, generation, grace: False)

    assert peer_reconciliation_supervisor.tick(db, max_active=1) == 0
    assert calls == []
    attempts = db.execute(text("""
        SELECT reconciliation_attempts FROM peer_outbound_receipts
        WHERE run_id=:run AND operation_id=:operation
    """), {"run": run_id, "operation": operation_id}).scalar_one()
    run = db.execute(text("SELECT generation FROM runs WHERE id=:run"), {"run": run_id}).scalar_one()
    assert attempts == 0
    assert run == 1


@pytest.mark.parametrize("mismatch", ["incarnation", "engine"])
def test_dispatch_start_rejects_durable_executor_identity_mismatch(db, project_session, monkeypatch, mismatch):
    run_id, operation_id, _ = _waiting_peer(db, project_session)
    executor_id, incarnation = uuid4(), uuid4()
    stored_engine = "other-engine" if mismatch == "engine" else None
    db.execute(text("UPDATE runs SET generation=2 WHERE id=:run"), {"run": run_id})
    db.execute(text("""
        INSERT INTO runtime_executors
            (id, run_id, generation, kind, operation_id, process_incarnation,
             engine_id, state, peer_reconciliation_attempt)
        VALUES (:id, :run, 2, 'dispatch', :operation, :incarnation,
                :engine, 'starting', 1)
    """), {
        "id": executor_id, "run": run_id, "operation": operation_id,
        "incarnation": incarnation, "engine": stored_engine,
    })
    db.commit()
    engine_calls = []
    cfg = SimpleNamespace(engine=SimpleNamespace(engine_id=lambda: engine_calls.append(True) or "fake-engine"))
    monkeypatch.setattr(supervisor, "_require_config", lambda: cfg)
    supplied_incarnation = uuid4() if mismatch == "incarnation" else incarnation

    with pytest.raises(DomainError) as error:
        supervisor.start_peer_reconciliation(
            db, run_id, 2, executor_id, supplied_incarnation,
            PeerReconciliationTarget(operation_id=operation_id, attempt=1),
        )

    assert error.value.code == "peer_reconciliation_conflict"
    assert len(engine_calls) == (1 if mismatch == "engine" else 0)


@pytest.mark.parametrize("trigger", ["cancel", "deadline", "foreign"])
def test_reaper_exact_stops_cancelled_or_expired_get_task_executor(db, project_session, monkeypatch, trigger):
    run_id, operation_id, _ = _waiting_peer(db, project_session)
    reap_attempts = peer_reconciliation_supervisor._reap_attempts
    calls = _fake_launches(db, project_session, monkeypatch, run_id)
    peer_reconciliation_supervisor.tick(db, max_active=1)
    assert len(calls) == 1
    executor = db.execute(text("""
        SELECT id, generation, operation_id, process_incarnation, engine_id, container_id
        FROM runtime_executors WHERE run_id=:run AND peer_reconciliation_attempt=1
    """), {"run": run_id}).mappings().one()
    ref = SimpleNamespace(
        executor_id=executor["id"], run_id=run_id, generation=executor["generation"],
        operation_id=operation_id, process_incarnation=executor["process_incarnation"],
        engine_id=executor["engine_id"],
        container_id="b" * 64 if trigger == "foreign" else executor["container_id"],
        kind="dispatch",
    )
    stopped = []

    class FakeDispatch:
        def find(self, *_args, **_kwargs):
            return ref

        def stop(self, _db, found, grace):
            stopped.append((found.executor_id, grace))

    monkeypatch.setattr(supervisor, "_require_config", lambda: SimpleNamespace(dispatch=FakeDispatch()))
    monkeypatch.setattr(supervisor, "_dispatch_operation_is_inactive", lambda *_args: True)
    if trigger == "cancel":
        db.execute(text("UPDATE runs SET cancel_requested=true WHERE id=:run"), {"run": run_id})
    elif trigger == "deadline":
        db.execute(text("UPDATE runs SET lease_expires_at=now() - interval '1 second' WHERE id=:run"), {"run": run_id})
    db.commit()

    reap_attempts(db, startup=trigger == "foreign")

    if trigger == "foreign":
        assert stopped == []
        assert db.execute(text("SELECT state FROM runtime_executors WHERE id=:id"), {
            "id": executor["id"],
        }).scalar_one() == "unknown"
        return

    assert stopped == [(executor["id"], 0)]
    row = db.execute(text("SELECT state, proof FROM runtime_executors WHERE id=:id"), {
        "id": executor["id"],
    }).mappings().one()
    assert row["state"] == "inactive"
    assert row["proof"]["source"] == "owned-engine-exact-container"
    assert row["proof"]["container_id"] == executor["container_id"]


def test_concurrent_ticks_claim_one_get_task_attempt(db, project_session, monkeypatch):
    run_id, operation_id, _ = _waiting_peer(db, project_session)
    calls = _fake_launches(db, project_session, monkeypatch, run_id)
    barrier = Barrier(2)

    def tick():
        with open_session() as session:
            barrier.wait(timeout=5)
            peer_reconciliation_supervisor.tick(session, max_active=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: tick(), range(2)))

    assert len(calls) == 1
    with open_session() as check:
        assert check.execute(text("SELECT reconciliation_attempts FROM peer_outbound_receipts WHERE run_id=:run AND operation_id=:operation"), {
            "run": run_id, "operation": operation_id,
        }).scalar_one() == 1
        assert check.execute(text("SELECT count(*) FROM runtime_executors WHERE run_id=:run AND peer_reconciliation_attempt IS NOT NULL"), {
            "run": run_id,
        }).scalar_one() == 1


def test_ordinary_reaper_preserves_active_attempt_before_started_marker(db, project_session, monkeypatch):
    run_id, operation_id, release = _waiting_peer(db, project_session, limit=2)
    _grant_peer(db, project_session, release)
    entered, release_network = Event(), Event()
    engine = _ReconciliationEngine()
    dispatch = _ReconciliationDispatch(ready_barrier=(entered, release_network))
    _configure_real_supervisor(
        monkeypatch, dispatch, engine,
        peer_destinations={str(release.peer_id): _PEER_ENDPOINT},
    )
    assert peer_reconciliation_supervisor._active_count(db) == 0

    def tick_in_fresh_session():
        with open_session() as local_db:
            return peer_reconciliation_supervisor.tick(local_db, max_active=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        launching = pool.submit(tick_in_fresh_session)
        if not entered.wait(timeout=10):
            pytest.fail(
                f"launch did not reach readiness; tick returned {launching.result(timeout=10)}, "
                f"active={peer_reconciliation_supervisor._active_count(db)}, "
                f"candidate={peer_reconciliation_supervisor._next_candidate(db) is not None}"
            )
        in_flight = db.execute(text("""
            SELECT state, peer_reconciliation_started FROM runtime_executors
            WHERE run_id=:run AND peer_reconciliation_attempt=1
        """), {"run": run_id}).one()
        assert in_flight == ("active", False)
        reaping = pool.submit(tick_in_fresh_session)
        assert reaping.result(timeout=10) == 0
        release_network.set()
        assert launching.result(timeout=10) == 1

    row = db.execute(text("""
        SELECT state, generation FROM runtime_executors
        WHERE run_id=:run AND peer_reconciliation_attempt=1
    """), {"run": run_id}).mappings().one()
    assert row["state"] == "active"
    assert row["generation"] == 2
    assert db.execute(text("""
        SELECT reconciliation_attempts FROM peer_outbound_receipts
        WHERE run_id=:run AND operation_id=:operation
    """), {"run": run_id, "operation": operation_id}).scalar_one() == 1
    assert db.execute(text("SELECT generation FROM runs WHERE id=:run"), {"run": run_id}).scalar_one() == 2


@pytest.fixture(autouse=True)
def _restore_runtime_globals(monkeypatch):
    monkeypatch.setattr(supervisor, "_config", supervisor._config)
    monkeypatch.setattr(broker, "_peer_destinations", broker._peer_destinations)


@pytest.fixture
def db(migrated_database, monkeypatch):
    # Lifecycle operations commit through several sessions. Rollback of the
    # ordinary test session cannot isolate those writes from later real ticks.
    # Use an owned database per case, without fabricating terminal/inactive rows.
    parent = database.engine()
    name = "scientist_test_peer" + uuid4().hex
    url = parent.url.difference_update_query(["dbname"]).set(database=name)
    with parent.connect().execution_options(isolation_level="AUTOCOMMIT") as admin:
        admin.exec_driver_sql(f'CREATE DATABASE "{name}"')
    isolated = create_engine(url, pool_pre_ping=True)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(database, "_engine", isolated)
            patch.setattr(database, "_sessions", sessionmaker(isolated, expire_on_commit=False))
            database.migrate(isolated)
            with open_session() as test_db:
                yield test_db
                test_db.rollback()
    finally:
        isolated.dispose()
        with parent.connect().execution_options(isolation_level="AUTOCOMMIT") as admin:
            admin.exec_driver_sql(f'DROP DATABASE "{name}"')


def _configure_real_supervisor(monkeypatch, dispatch, engine, *, peer_destinations=None):
    broker.configure(peer_destinations=peer_destinations or {})
    supervisor.configure(
        image="sha256:" + "1" * 64,
        image_digest="sha256:" + "1" * 64,
        broker_url="http://172.30.0.1:9000",
        broker_ip="172.30.0.1",
        broker_port=9000,
        runtime_commit=RUNTIME_COMMIT,
        skills_digest="2" * 64,
        environment_digest="3" * 64,
        bootstrap_factory=lambda *_args: (_ for _ in ()).throw(AssertionError("worker bootstrap called")),
        capability_factory=lambda *_args: (_ for _ in ()).throw(AssertionError("worker capability called")),
        dispatch=dispatch,
        engine=engine,
    )


class _ReconciliationEngine:
    def __init__(self, *, network_barrier=None):
        self.network_barrier = network_barrier
        self.network_calls = []

    def engine_id(self):
        return "fake-engine"

    def create_run_network(self, run_id):
        self.network_calls.append(run_id)
        if self.network_barrier is not None:
            entered, release = self.network_barrier
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("network barrier timed out")
        return "review-network"


class _ReconciliationDispatch:
    def __init__(self, *, ready_barrier=None):
        self.terminated = set()
        self.find_calls = []
        self.start_calls = []
        self.ready_barrier = ready_barrier

    def start(self, db, run_id, generation, _network, _broker_ip, executor_id, incarnation,
              *, before_mutation, peer_reconciliation):
        self.start_calls.append(peer_reconciliation)
        before_mutation("fake-engine", None)
        container_id = sha256(str(executor_id).encode()).hexdigest()
        before_mutation("fake-engine", container_id)
        return supervisor.ExecutorRef(
            executor_id, run_id, generation, "dispatch", peer_reconciliation.operation_id,
            incarnation, "fake-engine", container_id,
        )

    def find(self, db, run_id, generation, executor_id, operation_id, incarnation,
             *, peer_reconciliation=None):
        self.find_calls.append(peer_reconciliation)
        row = db.execute(text("""
            SELECT * FROM runtime_executors
            WHERE id=:id AND run_id=:run AND generation=:generation
        """), {"id": executor_id, "run": run_id, "generation": generation}).mappings().one()
        if row["state"] == "inactive" or row["container_id"] is None or row["id"] in self.terminated:
            return None
        return supervisor._executor_ref(row)

    def inactive(self, _db, _executor, _operation_id):
        return True

    def stop(self, _db, executor, _grace_seconds):
        self.terminated.add(executor.executor_id)

    def ready(self, *_args):
        if self.ready_barrier is not None:
            entered, release = self.ready_barrier
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("dispatch readiness barrier timed out")
        return True


def _grant_peer(db, project_session, release):
    broker.configure(peer_destinations={str(release.peer_id): _PEER_ENDPOINT})


def test_tick_does_not_consume_attempt_without_current_peer_admission(db, project_session, monkeypatch):
    run_id, operation_id, release = _waiting_peer(db, project_session, limit=1)
    db.execute(text("DELETE FROM delegations WHERE project_id=:project AND peer_id=:peer"), {
        "project": project_session[0], "peer": release.peer_id,
    })
    db.execute(text("DELETE FROM credentials WHERE project_id=:project AND provider=:provider"), {
        "project": project_session[0], "provider": f"peer:{release.peer_id}",
    })
    db.commit()
    engine = _ReconciliationEngine()
    dispatch = _ReconciliationDispatch()
    _configure_real_supervisor(monkeypatch, dispatch, engine, peer_destinations={})
    assert peer_reconciliation_supervisor._next_candidate(db) is not None
    assert peer_reconciliation_supervisor._active_count(db) == 0

    assert peer_reconciliation_supervisor.tick(db, max_active=1) == 0
    assert engine.network_calls == []
    assert dispatch.start_calls == []
    assert db.execute(text("""
        SELECT reconciliation_attempts FROM peer_outbound_receipts
        WHERE run_id=:run AND operation_id=:operation
    """), {"run": run_id, "operation": operation_id}).scalar_one() == 0
    assert db.execute(text("SELECT generation FROM runs WHERE id=:run"), {"run": run_id}).scalar_one() == 1
    assert db.execute(text("""
        SELECT count(*) FROM runtime_executors
        WHERE run_id=:run AND peer_reconciliation_attempt IS NOT NULL
    """), {"run": run_id}).scalar_one() == 0


def test_reaper_finds_peer_dispatch_with_immutable_reconciliation_target(db, project_session, monkeypatch):
    run_id, operation_id, release = _waiting_peer(db, project_session)
    _grant_peer(db, project_session, release)
    run = db.execute(text("SELECT revision, project_id FROM runs WHERE id=:run"), {"run": run_id}).one()
    plan = peer_reconciliation_supervisor._approved_plan(db, run_id, run.revision, run.project_id)
    broker.validate_peer_reconciliation(db, run_id, run.project_id, plan, release.release_id)
    dispatch = _ReconciliationDispatch()
    engine = _ReconciliationEngine()
    _configure_real_supervisor(
        monkeypatch, dispatch, engine,
        peer_destinations={str(release.peer_id): _PEER_ENDPOINT},
    )
    assert peer_reconciliation_supervisor.tick(db, max_active=1) == 1
    executor = db.execute(text("""
        SELECT * FROM runtime_executors
        WHERE run_id=:run AND peer_reconciliation_attempt=1
    """), {"run": run_id}).mappings().one()
    db.execute(text("UPDATE runs SET cancel_requested=true WHERE id=:run"), {"run": run_id})
    db.commit()

    peer_reconciliation_supervisor._reap_attempts(db, startup=False)

    assert dispatch.find_calls[-1] == PeerReconciliationTarget(operation_id=operation_id, attempt=1)
    assert executor["id"] in dispatch.terminated


def test_exact_absence_requires_current_engine_even_with_container_id(db, project_session):
    run_id, operation_id, _release = _waiting_peer(db, project_session)
    executor_id, incarnation = uuid4(), uuid4()
    db.execute(text("UPDATE runs SET generation=2 WHERE id=:run"), {"run": run_id})
    db.execute(text("""
        INSERT INTO runtime_executors
          (id, run_id, generation, kind, operation_id, process_incarnation,
           engine_id, container_id, state, peer_reconciliation_attempt)
        VALUES (:id, :run, 2, 'dispatch', :operation, :incarnation,
                'old-engine', :container, 'starting', 1)
    """), {
        "id": executor_id, "run": run_id, "operation": operation_id,
        "incarnation": incarnation, "container": "b" * 64,
    })
    db.commit()

    dispatch = SimpleNamespace(engine_id=lambda: "current-engine")
    row = db.execute(text("SELECT * FROM runtime_executors WHERE id=:id"), {
        "id": executor_id,
    }).mappings().one()
    assert peer_reconciliation_supervisor._prove_exact_absence(db, dispatch, row) is False
    row = db.execute(text("SELECT state, proof FROM runtime_executors WHERE id=:id"), {
        "id": executor_id,
    }).mappings().one()
    assert row["state"] == "starting"
    assert row["proof"] == {}


def test_recover_supplies_immutable_target_for_unbound_peer_executor(db, project_session, monkeypatch):
    run_id, operation_id, release = _waiting_peer(db, project_session)
    executor_id, incarnation = uuid4(), uuid4()
    db.execute(text("UPDATE runs SET generation=2 WHERE id=:run"), {"run": run_id})
    db.execute(text("""
        INSERT INTO runtime_executors
          (id, run_id, generation, kind, operation_id, process_incarnation,
           engine_id, state, peer_reconciliation_attempt)
        VALUES (:id, :run, 2, 'dispatch', :operation, :incarnation,
                'fake-engine', 'starting', 1)
    """), {
        "id": executor_id, "run": run_id, "operation": operation_id,
        "incarnation": incarnation,
    })
    db.commit()
    dispatch = _ReconciliationDispatch()
    _configure_real_supervisor(
        monkeypatch, dispatch, _ReconciliationEngine(),
        peer_destinations={str(release.peer_id): _PEER_ENDPOINT},
    )

    recovered = supervisor.recover(db, run_id)

    assert dispatch.find_calls == [PeerReconciliationTarget(operation_id=operation_id, attempt=1)]
    # A lookup without a physical identity is still uncertain. Recovery must
    # keep the original reservation, rather than infer absence from this stub.
    assert recovered.waiting_reason == "executor_quiescence_unproven"
    assert db.execute(text("SELECT state, reserve_tokens FROM operations WHERE run_id=:run"), {
        "run": run_id,
    }).one() == ("unknown", 100)


def test_docker_dispatch_find_accepts_peer_operation_only_with_immutable_target(monkeypatch):
    digest = "1" * 64
    runtime = DockerDispatchRuntime(DispatchServiceConfig(
        image=f"registry.invalid/scientist-dispatch@sha256:{digest}",
        image_digest=f"sha256:{digest}",
        service_network="scientist-b5-services-test",
        config_path="/run/scientist/dispatch/config.json",
        secrets_dir="/run/scientist/secrets",
        host_config_file="/tmp/dispatch-config.json",
        host_secrets_dir="/tmp/dispatch-secrets",
        launcher_dir="/tmp/dispatch-launches",
    ))
    run_id, executor_id, incarnation = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(runtime, "engine_id", lambda: "fake-engine")
    monkeypatch.setattr(runtime, "_verified_image_id", lambda: f"sha256:{digest}")
    monkeypatch.setattr(runtime, "_docker", lambda *_args: "")

    with pytest.raises(RuntimeError, match="reconciliation operation"):
        runtime.find(object(), run_id, 2, executor_id, "peer-op-reconcile", incarnation)

    target = PeerReconciliationTarget(operation_id="peer-op-reconcile", attempt=1)
    assert runtime.find(
        object(), run_id, 2, executor_id, "peer-op-reconcile", incarnation,
        peer_reconciliation=target,
    ) is None
