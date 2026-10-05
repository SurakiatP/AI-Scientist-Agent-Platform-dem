"""B5 startup accounting regressions against PostgreSQL and the real adapter."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from scientist import supervisor
from scientist.contracts import PlanSpec, Principal
from scientist.dispatch_runtime import DockerDispatchRuntime, DispatchServiceConfig
from scientist.db import create_project, create_session
from scientist.domain import approve_run, revise_plan, submit_run
from scientist.runtime_contracts import RUNTIME_COMMIT
from scientist.supervisor import DispatchPreLaunchRejected, ExecutorRef


class _OwnedEngine:
    """Synthetic owned engine: network setup is observable; executor work is forbidden."""

    def __init__(self) -> None:
        self.network_creates: list[tuple[object, int, object]] = []
        self.container_operations: list[str] = []

    def engine_id(self) -> str:
        return "b5-owned-engine-stable-id"

    def create_run_network(self, run_id, generation, worker_id):
        self.network_creates.append((run_id, generation, worker_id))
        return f"scientist-run-{run_id.hex[:12]}-g{generation}", "172.29.42.2"

    def _verified_image_id(self, image: str, digest: str) -> str:
        raise DispatchPreLaunchRejected("dispatch image does not match configured immutable digest")

    def _docker(self, *args):
        self.container_operations.append(" ".join(args))
        raise AssertionError("dispatch container operation ran before image validation")

    def __getattr__(self, name):
        if name.startswith(("create_", "start_", "connect_", "install_", "stop_", "release_")):
            def forbidden(*args, **kwargs):
                self.container_operations.append(name)
                raise AssertionError(f"unexpected engine operation: {name}")

            return forbidden
        raise AttributeError(name)


class _PossibleSideEffectDispatch:
    """Control adapter whose start may have launched an executor before failing."""

    def __init__(self) -> None:
        self.start_calls = 0
        self.find_calls = 0

    def start(self, db, run_id, generation, network, broker_ip, executor_id, incarnation, *, before_mutation):
        self.start_calls += 1
        before_mutation("b5-owned-engine-stable-id", None)
        raise RuntimeError("launch response lost after possible container creation")

    def find(self, *args):
        self.find_calls += 1
        raise RuntimeError("owned engine inspection failed")

    def stop(self, *args):
        raise AssertionError("unknown executor must not be stopped by guessed identity")

    def inactive(self, *args):
        return False


class _ObservedDispatchEngine:
    """In-memory Docker seam that exposes ordering without invoking Docker."""

    def __init__(self, *, failure=None, run_id=None, verification=None):
        self.failure = failure
        self.run_id = run_id
        self.verification = verification
        self.calls = []
        self.container_id = "e" * 64
        self.created = False
        self.started = False
        self.connected = False
        self.snapshot = None
        self.snapshot_callback = None

    def engine_id(self):
        if self.failure == "engine-id":
            raise RuntimeError("owned dispatch engine identity unavailable")
        return "dispatch-owned-engine-stable"

    def _verified_image_id(self, image, digest):
        if self.failure == "image-inspection":
            raise RuntimeError("dispatch image inspection failed")
        return digest

    def _docker(self, *args):
        self.calls.append(args)
        command = args[0]
        if (
            command in {"create", "start"}
            or (command == "network" and args[1:2] == ("connect",))
        ) and self.snapshot_callback:
            self.snapshot = self.snapshot_callback()
        if command == "ps":
            return ""
        if command == "create":
            if self.failure == "create-response-lost":
                self.created = True
                raise RuntimeError("docker create response lost after apply")
            self.created = True
            return self.container_id
        if command == "inspect":
            if "NetworkSettings" in args[2]:
                return "{}"  # no extra (egress) network attached
            if self.verification == "image-mismatch":
                return "sha256:" + "f" * 64 + "|" + self.image
            if self.created:
                return self.image_digest + "|" + self.image
            return ""
        if command == "start":
            if self.failure == "start-response-lost":
                self.started = True
                raise RuntimeError("docker start response lost after apply")
            self.started = True
            return ""
        if command == "network":
            if args[1:2] == ("inspect",):
                return "{}"
            if self.failure == "connect-response-lost":
                self.connected = True
                raise RuntimeError("docker network connect response lost after apply")
            if args[1:2] == ("connect",):
                self.connected = True
            return ""
        return ""

    def bind(self, image, digest):
        self.image = image
        self.image_digest = digest


def _observed_dispatch(run_id, *, failure=None, verification=None):
    engine = _ObservedDispatchEngine(failure=failure, run_id=run_id, verification=verification)
    digest = "sha256:" + "d" * 64
    engine.bind(f"registry.invalid/scientist-dispatch@{digest}", digest)
    dispatch = DockerDispatchRuntime(_dispatch_config(), engine=engine)
    # Keep adapter control flow real while replacing host/filesystem prerequisites.
    dispatch._materialize_launch_config = lambda *_: "/tmp/unused-b5-launch.json"
    dispatch._secret_mounts = lambda: []
    return dispatch, engine


def _independent_executor_snapshot(db, run_id):
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import NullPool
    from sqlalchemy import create_engine

    from scientist.db import engine as application_engine

    independent = create_engine(application_engine().url, poolclass=NullPool)
    try:
        with Session(independent) as view:
            row = view.execute(
                text("SELECT id, process_incarnation, engine_id, container_id, state, proof FROM runtime_executors WHERE run_id=:run AND generation=1 AND kind='dispatch'"),
                {"run": run_id},
            ).mappings().one()
            return dict(row)
    finally:
        independent.dispose()


def _approved_run(db, project_session):
    project_id, session_id = project_session
    owner = Principal(identity=uuid4(), kind="owner")
    provider_id = uuid4()
    run = submit_run(
        db,
        owner,
        project_id,
        session_id,
        f"b5-startup-{uuid4().hex}",
        "synthetic startup accounting question",
        [],
        provider_id,
        "fixture-model",
    )
    snapshot = db.execute(
        text("SELECT digest FROM input_snapshots WHERE run_id=:run"),
        {"run": run.run_id},
    ).scalar_one().strip()
    plan = PlanSpec(
        input_snapshot_digest=snapshot,
        provider_id=provider_id,
        model="fixture-model",
        stages=["fixture"],
        allowed_ops=["llm"],
        data_recipients=["https://research.example"],
        packages=[],
        token_limit=1000,
        elapsed_limit_ms=60_000,
    )
    revised = revise_plan(db, owner, run.run_id, run.revision, plan)
    approved = approve_run(db, owner, run.run_id, revised.revision, revised.plan_digest)
    return run, owner, approved


def _claim_run(db, run_id):
    """Move only this test's run to running generation 1; touch no other rows."""
    changed = db.execute(
        text("UPDATE runs SET state='running', generation=1, lease_expires_at=now()+interval '5 minutes' WHERE id=:run AND state='queued' RETURNING id"),
        {"run": run_id},
    ).scalar_one_or_none()
    db.commit()
    return (changed, 1) if changed else None


@pytest.fixture
def reject_dispatch_launch_intent(db):
    db.execute(text("""
        CREATE FUNCTION reject_dispatch_launch_intent() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.kind = 'dispatch' AND NEW.proof->>'source' = 'dispatch-launch-intent' THEN
                RAISE EXCEPTION 'test launch intent write failure';
            END IF;
            RETURN NEW;
        END $$
    """))
    db.execute(text("""
        CREATE TRIGGER reject_dispatch_launch_intent
        BEFORE UPDATE ON runtime_executors FOR EACH ROW
        EXECUTE FUNCTION reject_dispatch_launch_intent()
    """))
    db.commit()
    try:
        yield
    finally:
        db.rollback()
        db.execute(text("DROP TRIGGER IF EXISTS reject_dispatch_launch_intent ON runtime_executors"))
        db.execute(text("DROP FUNCTION IF EXISTS reject_dispatch_launch_intent()"))
        db.commit()


def _configure(engine, dispatch, bootstrap_factory=lambda *_: None):
    supervisor.configure(
        image="registry.invalid/scientist-worker@sha256:" + "a" * 64,
        image_digest="sha256:" + "a" * 64,
        broker_url="http://172.29.42.1:8080",
        broker_ip="172.29.42.1",
        broker_port=8080,
        runtime_commit=RUNTIME_COMMIT,
        skills_digest="b" * 64,
        environment_digest="c" * 64,
        bootstrap_factory=bootstrap_factory,
        capability_factory=lambda *_: "synthetic-capability",
        dispatch=dispatch,
        engine=engine,
    )


def _dispatch_config() -> DispatchServiceConfig:
    digest = "sha256:" + "d" * 64
    return DispatchServiceConfig(
        image=f"registry.invalid/scientist-dispatch@{digest}",
        image_digest=digest,
        service_network="scientist-b5-services-test",
        config_path="/run/scientist/dispatch/config.json",
        secrets_dir="/run/scientist/secrets",
        port=8123,
        host_config_file="/tmp/b5-dispatch-config.json",
        host_secrets_dir="/tmp/b5-dispatch-secrets",
        launcher_dir="/tmp/b5-dispatch-launches",
    )


def test_pre_discovery_dispatch_image_rejection_is_accounted_as_not_launched(
    db, project_session,
):
    run, _, approved = _approved_run(db, project_session)
    assert approved.state == "queued"

    owned_engine = _OwnedEngine()
    dispatch_engine = _OwnedEngine()
    dispatch = DockerDispatchRuntime(_dispatch_config(), engine=dispatch_engine)
    # Hold the real adapter at its actual image-validation boundary. The fake
    # engine supplies a stable identity and deterministic immutable-image reject.
    _configure(owned_engine, dispatch)

    claimed = _claim_run(db, run.run_id)
    assert claimed == (run.run_id, 1)
    with pytest.raises(RuntimeError, match="immutable digest"):
        supervisor.start(db, run.run_id, 1)

    executors = db.execute(
        text("SELECT id, process_incarnation, kind, state, proof, engine_id, container_id FROM runtime_executors WHERE run_id=:run ORDER BY kind"),
        {"run": run.run_id},
    ).mappings().all()
    dispatch_row = next(row for row in executors if row["kind"] == "dispatch")
    worker_row = next(row for row in executors if row["kind"] == "worker")
    assert dispatch_row["state"] == "inactive"
    assert dispatch_row["proof"]["source"] == "dispatch-launch-not-attempted"
    assert dispatch_row["proof"]["run_id"] == str(run.run_id)
    assert dispatch_row["proof"]["generation"] == 1
    assert dispatch_row["proof"]["executor_id"] == str(dispatch_row["id"])
    assert dispatch_row["proof"]["process_incarnation"] == str(dispatch_row["process_incarnation"])
    assert dispatch_row["container_id"] is None
    assert dispatch_row["engine_id"] is None
    assert worker_row["state"] == "inactive"
    assert worker_row["proof"] == {"source": "worker-launch-not-attempted"}

    stopped = supervisor.stop(db, run.run_id, 0)
    assert stopped.state == "canceled"
    ledger = db.execute(
        text("SELECT generation, usage_tokens, reserved_tokens FROM runs WHERE id=:run"),
        {"run": run.run_id},
    ).one()
    assert (ledger.generation, ledger.usage_tokens, ledger.reserved_tokens) == (1, 0, 0)
    assert owned_engine.container_operations == []
    assert dispatch_engine.container_operations == []


def test_possible_dispatch_side_effect_and_inspection_failure_remain_unknown(
    db, project_session,
):
    run, _, _ = _approved_run(db, project_session)
    owned_engine = _OwnedEngine()
    dispatch = _PossibleSideEffectDispatch()
    _configure(owned_engine, dispatch)

    assert _claim_run(db, run.run_id) == (run.run_id, 1)
    with pytest.raises(RuntimeError, match="response lost"):
        supervisor.start(db, run.run_id, 1)

    row = db.execute(
        text("SELECT state, engine_id, container_id, process_incarnation FROM runtime_executors WHERE run_id=:run AND kind='dispatch'"),
        {"run": run.run_id},
    ).mappings().one()
    assert row["state"] == "unknown"
    assert row["engine_id"] == "b5-owned-engine-stable-id"
    assert row["container_id"] is None
    assert row["process_incarnation"] is not None
    assert row["state"] == "unknown"
    assert dispatch.start_calls == 1
    proof = db.execute(
        text("SELECT proof FROM runtime_executors WHERE run_id=:run AND kind='dispatch'"),
        {"run": run.run_id},
    ).scalar_one()
    assert proof["source"] == "dispatch-launch-intent"

    stopped = supervisor.stop(db, run.run_id, 0)
    assert stopped.state == "waiting_input"
    generation = db.execute(
        text("SELECT generation FROM runs WHERE id=:run"), {"run": run.run_id}
    ).scalar_one()
    assert generation == 1
    assert db.execute(
        text("SELECT state FROM runtime_executors WHERE run_id=:run AND kind='dispatch'"),
        {"run": run.run_id},
    ).scalar_one() == "unknown"


def test_dispatch_launch_intent_is_independently_visible_before_create(
    db, project_session,
):
    run, _, _ = _approved_run(db, project_session)
    owned_engine = _OwnedEngine()
    dispatch, dispatch_engine = _observed_dispatch(run.run_id, failure="create-response-lost")
    dispatch_engine.snapshot_callback = lambda: _independent_executor_snapshot(db, run.run_id)
    _configure(owned_engine, dispatch)

    assert _claim_run(db, run.run_id) == (run.run_id, 1)
    with pytest.raises(RuntimeError, match="response lost"):
        supervisor.start(db, run.run_id, 1)

    snapshot = dispatch_engine.snapshot
    assert snapshot is not None
    assert snapshot["engine_id"] == "dispatch-owned-engine-stable"
    assert snapshot["state"] == "starting"
    assert snapshot["proof"]["source"] == "dispatch-launch-intent"
    assert snapshot["proof"]["engine_id"] == snapshot["engine_id"]
    assert snapshot["proof"]["run_id"] == str(run.run_id)
    assert snapshot["proof"]["generation"] == 1
    assert snapshot["proof"]["executor_id"] == str(snapshot["id"])
    assert snapshot["proof"]["process_incarnation"] == str(snapshot["process_incarnation"])

    rows = db.execute(
        text("SELECT kind, state, proof, engine_id FROM runtime_executors WHERE run_id=:run ORDER BY kind"),
        {"run": run.run_id},
    ).mappings().all()
    dispatch_row = next(row for row in rows if row["kind"] == "dispatch")
    assert dispatch_row["state"] == "unknown"
    assert dispatch_row["engine_id"] == "dispatch-owned-engine-stable"
    assert dispatch_row["proof"]["source"] == "dispatch-launch-intent"
    assert len(rows) == 2
    assert len(owned_engine.network_creates) == 1
    assert owned_engine.container_operations == []
    assert [
        call[0]
        for call in dispatch_engine.calls
        if call[0] in {"create", "start"}
        or (call[0] == "network" and call[1:2] == ("connect",))
    ] == ["create"]
    ledger = db.execute(
        text("SELECT generation, usage_tokens, reserved_tokens FROM runs WHERE id=:run"),
        {"run": run.run_id},
    ).one()
    assert (ledger.generation, ledger.usage_tokens, ledger.reserved_tokens) == (1, 0, 0)


@pytest.mark.parametrize(
    ("failure", "expected_mutations"),
    [
        ("create-response-lost", ["create"]),
        ("start-response-lost", ["create", "network", "start"]),
        ("connect-response-lost", ["create", "network"]),
    ],
)
def test_possible_executor_side_effects_keep_intent_and_never_prove_no_launch(
    db, project_session, failure, expected_mutations,
):
    run, _, _ = _approved_run(db, project_session)
    owned_engine = _OwnedEngine()
    dispatch, dispatch_engine = _observed_dispatch(run.run_id, failure=failure)
    snapshots = []
    dispatch_engine.snapshot_callback = lambda: snapshots.append(
        _independent_executor_snapshot(db, run.run_id)
    ) or snapshots[-1]
    _configure(owned_engine, dispatch)

    assert _claim_run(db, run.run_id) == (run.run_id, 1)
    with pytest.raises(RuntimeError):
        supervisor.start(db, run.run_id, 1)

    mutations = [
        call[0]
        for call in dispatch_engine.calls
        if call[0] in {"create", "start"}
        or (call[0] == "network" and call[1:2] == ("connect",))
    ]
    assert mutations == expected_mutations
    assert dispatch_engine.created
    assert dispatch_engine.started is (failure == "start-response-lost")
    assert dispatch_engine.connected is (failure in {"start-response-lost", "connect-response-lost"})
    assert snapshots
    assert all(item["engine_id"] == "dispatch-owned-engine-stable" for item in snapshots)
    assert all(item["proof"].get("source") == "dispatch-launch-intent" for item in snapshots)
    row = db.execute(
        text("SELECT state, proof, engine_id FROM runtime_executors WHERE run_id=:run AND kind='dispatch'"),
        {"run": run.run_id},
    ).mappings().one()
    assert row["state"] == "unknown"
    assert row["engine_id"] == "dispatch-owned-engine-stable"
    assert row["proof"].get("source") != "dispatch-launch-not-attempted"
    current = db.execute(
        text("SELECT generation, usage_tokens, reserved_tokens FROM runs WHERE id=:run"),
        {"run": run.run_id},
    ).one()
    assert (current.generation, current.usage_tokens, current.reserved_tokens) == (1, 0, 0)


@pytest.mark.parametrize("failure", ["engine-id", "image-inspection"])
def test_dispatch_engine_or_image_inspection_failure_stays_unknown(
    db, project_session, failure,
):
    run, _, _ = _approved_run(db, project_session)
    owned_engine = _OwnedEngine()
    dispatch, dispatch_engine = _observed_dispatch(run.run_id, failure=failure)
    _configure(owned_engine, dispatch)

    assert _claim_run(db, run.run_id) == (run.run_id, 1)
    with pytest.raises(RuntimeError):
        supervisor.start(db, run.run_id, 1)

    row = db.execute(
        text("SELECT state, proof FROM runtime_executors WHERE run_id=:run AND kind='dispatch'"),
        {"run": run.run_id},
    ).mappings().one()
    assert row["state"] == "unknown"
    assert row["proof"].get("source") != "dispatch-launch-not-attempted"
    assert not any(
        call[0] in {"create", "start"}
        or (call[0] == "network" and call[1:2] == ("connect",))
        for call in dispatch_engine.calls
    )
    generation = db.execute(
        text("SELECT generation FROM runs WHERE id=:run"), {"run": run.run_id}
    ).scalar_one()
    assert generation == 1


@pytest.mark.parametrize("conflict", ["run", "generation", "executor", "incarnation", "engine", "image"])
def test_discovered_identity_conflicts_never_become_no_launch_proof(
    db, project_session, conflict,
):
    run, _, _ = _approved_run(db, project_session)
    owned_engine = _OwnedEngine()
    dispatch, dispatch_engine = _observed_dispatch(
        run.run_id,
        verification="image-mismatch" if conflict == "image" else None,
    )
    find_count = 0

    def conflicting_find(_db, run_id, generation, executor_id, _operation, incarnation):
        nonlocal find_count
        find_count += 1
        if find_count > 1:
            raise RuntimeError("subsequent inspection unavailable")
        return ExecutorRef(
            uuid4() if conflict == "executor" else executor_id,
            uuid4() if conflict == "run" else run_id,
            generation + 1 if conflict == "generation" else generation,
            "dispatch",
            None,
            uuid4() if conflict == "incarnation" else incarnation,
            "different-owned-engine" if conflict == "engine" else dispatch_engine.engine_id(),
            dispatch_engine.container_id,
        )

    dispatch.find = conflicting_find
    _configure(owned_engine, dispatch)

    assert _claim_run(db, run.run_id) == (run.run_id, 1)
    with pytest.raises(RuntimeError):
        supervisor.start(db, run.run_id, 1)

    row = db.execute(
        text("SELECT state, proof FROM runtime_executors WHERE run_id=:run AND kind='dispatch'"),
        {"run": run.run_id},
    ).mappings().one()
    assert row["state"] == "unknown"
    assert row["proof"].get("source") != "dispatch-launch-not-attempted"
    assert find_count >= 1
    assert owned_engine.container_operations == []
    assert not any(
        call[0] in {"create", "start"}
        or (call[0] == "network" and call[1:2] == ("connect",))
        for call in dispatch_engine.calls
    )


def test_exact_preexisting_dispatch_ref_is_never_reclassified_as_not_launched(
    db, project_session,
):
    run, _, _ = _approved_run(db, project_session)
    owned_engine = _OwnedEngine()
    dispatch, dispatch_engine = _observed_dispatch(run.run_id)
    dispatch_engine.created = True
    find_count = 0

    def find_exact_once(_db, run_id, generation, executor_id, _operation, incarnation):
        nonlocal find_count
        find_count += 1
        if find_count > 1:
            raise RuntimeError("post-start inspection unavailable")
        return ExecutorRef(
            executor_id,
            run_id,
            generation,
            "dispatch",
            None,
            incarnation,
            dispatch_engine.engine_id(),
            dispatch_engine.container_id,
        )

    dispatch.find = find_exact_once
    def bootstrap_unavailable(*_):
        raise RuntimeError("bootstrap unavailable")

    _configure(owned_engine, dispatch, bootstrap_unavailable)

    assert _claim_run(db, run.run_id) == (run.run_id, 1)
    with pytest.raises(RuntimeError):
        supervisor.start(db, run.run_id, 1)

    row = db.execute(
        text("SELECT state, proof, engine_id, container_id FROM runtime_executors WHERE run_id=:run AND kind='dispatch'"),
        {"run": run.run_id},
    ).mappings().one()
    assert row["state"] == "inactive"
    assert row["proof"]["source"] == "owned-engine-exact-container"
    assert owned_engine.container_operations == []
    assert row["engine_id"] == "dispatch-owned-engine-stable"
    assert row["container_id"] == dispatch_engine.container_id
    # start() found the exact ref. Recovery can stop that bound ref without
    # rediscovering it; this physical stop proof is distinct from no-launch.
    assert find_count >= 1
    assert any(call[0] == "start" for call in dispatch_engine.calls)


def test_new_start_proof_cannot_rewrite_an_older_unknown_executor(
    db, project_session,
):
    run, _, _ = _approved_run(db, project_session)
    old_executor, old_incarnation = uuid4(), uuid4()
    db.execute(
        text("UPDATE runs SET state='running', generation=2, lease_expires_at=now()+interval '1 minute' WHERE id=:run"),
        {"run": run.run_id},
    )
    db.execute(
        text("""
            INSERT INTO runtime_executors
                (id, run_id, generation, kind, operation_id, process_incarnation, state)
            VALUES (:id, :run, 1, 'dispatch', NULL, :incarnation, 'unknown')
        """),
        {"id": old_executor, "run": run.run_id, "incarnation": old_incarnation},
    )
    db.commit()

    old_before = dict(db.execute(
        text("SELECT * FROM runtime_executors WHERE id=:id"), {"id": old_executor}
    ).mappings().one())
    owned_engine = _OwnedEngine()
    dispatch = DockerDispatchRuntime(_dispatch_config(), engine=_OwnedEngine())
    dispatch._verified_image_id = lambda *_: (_ for _ in ()).throw(
        DispatchPreLaunchRejected("dispatch image does not match configured immutable digest")
    )
    _configure(owned_engine, dispatch)

    with pytest.raises(RuntimeError):
        supervisor.start(db, run.run_id, 2)

    old_after = dict(db.execute(
        text("SELECT * FROM runtime_executors WHERE id=:id"), {"id": old_executor}
    ).mappings().one())
    assert {k: v for k, v in old_after.items() if k != "updated_at"} == {
        k: v for k, v in old_before.items() if k != "updated_at"
    }
    fresh_row = db.execute(
        text("SELECT state, proof, id, process_incarnation FROM runtime_executors WHERE run_id=:run AND generation=2 AND kind='dispatch'"),
        {"run": run.run_id},
    ).mappings().one()
    assert fresh_row["state"] == "inactive"
    assert fresh_row["proof"]["source"] == "dispatch-launch-not-attempted"
    assert fresh_row["proof"]["run_id"] == str(run.run_id)
    assert fresh_row["proof"]["generation"] == 2
    assert fresh_row["proof"]["executor_id"] == str(fresh_row["id"])
    assert fresh_row["proof"]["process_incarnation"] == str(fresh_row["process_incarnation"])
    assert fresh_row["id"] != old_executor
    assert fresh_row["process_incarnation"] != old_incarnation
    assert owned_engine.container_operations == []


def test_failed_launch_intent_write_blocks_every_dispatch_mutation(
    db, project_session, reject_dispatch_launch_intent,
):
    run, _, _ = _approved_run(db, project_session)

    owned_engine = _OwnedEngine()
    dispatch, dispatch_engine = _observed_dispatch(run.run_id)
    _configure(owned_engine, dispatch)
    assert _claim_run(db, run.run_id) == (run.run_id, 1)

    with pytest.raises(RuntimeError):
        supervisor.start(db, run.run_id, 1)

    assert not any(
        call[0] in {"create", "start"}
        or (call[0] == "network" and call[1:2] == ("connect",))
        for call in dispatch_engine.calls
    )
    rows = db.execute(
        text("SELECT kind, state FROM runtime_executors WHERE run_id=:run"),
        {"run": run.run_id},
    ).mappings().all()
    assert len(rows) == 2
    assert len(owned_engine.network_creates) == 1
    assert owned_engine.container_operations == []
    generation = db.execute(
        text("SELECT generation FROM runs WHERE id=:run"), {"run": run.run_id}
    ).scalar_one()
    assert generation == 1


def test_separate_public_starts_allocate_distinct_executor_incarnations(
    db, project_session,
):
    first, _, _ = _approved_run(db, project_session)
    second, _, _ = _approved_run(db, project_session)
    owned_engine = _OwnedEngine()
    dispatch = DockerDispatchRuntime(_dispatch_config(), engine=_OwnedEngine())
    dispatch._verified_image_id = lambda *_: (_ for _ in ()).throw(
        RuntimeError("dispatch image inspection failed")
    )
    _configure(owned_engine, dispatch)

    first_claim = _claim_run(db, first.run_id)
    assert first_claim == (first.run_id, 1)
    with pytest.raises(RuntimeError):
        supervisor.start(db, first_claim[0], first_claim[1])
    second_claim = _claim_run(db, second.run_id)
    assert second_claim == (second.run_id, 1)
    with pytest.raises(RuntimeError):
        supervisor.start(db, second_claim[0], second_claim[1])

    rows = db.execute(
        text("SELECT run_id, kind, id, process_incarnation FROM runtime_executors WHERE run_id IN (:first, :second) AND kind='dispatch' ORDER BY run_id"),
        {"first": first.run_id, "second": second.run_id},
    ).mappings().all()
    assert len(rows) == 2
    assert rows[0]["id"] != rows[1]["id"]
    assert rows[0]["process_incarnation"] != rows[1]["process_incarnation"]


def _real_engine(raw):
    from scientist.supervisor import DockerWorkerEngine

    engine = DockerWorkerEngine.__new__(DockerWorkerEngine)
    engine._docker = lambda *args: raw
    return engine


_IMG_DIGEST = "sha256:" + "d" * 64
_TAGGED = f"registry.invalid/scientist-dispatch:tag@{_IMG_DIGEST}"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"sha256:{'f' * 64}|[\"{_TAGGED}\"]", DispatchPreLaunchRejected),
        (f"{_IMG_DIGEST}|[]", DispatchPreLaunchRejected),
        (f"{_IMG_DIGEST}|[\"other@{_IMG_DIGEST}\"]", DispatchPreLaunchRejected),
        (f"{_IMG_DIGEST}|null", RuntimeError),
        (f"{_IMG_DIGEST}|not-json", RuntimeError),
        ("no-separator", RuntimeError),
    ],
)
def test_real_verified_image_id_types_only_affirmative_mismatch(raw, expected):
    with pytest.raises(expected) as caught:
        _real_engine(raw)._verified_image_id(_TAGGED, _IMG_DIGEST)
    if expected is RuntimeError:
        assert not isinstance(caught.value, DispatchPreLaunchRejected)


def test_rejection_after_adapter_returns_without_intent_stays_unknown(db, project_session):
    run, _, _ = _approved_run(db, project_session)

    class SkipsCallback(_PossibleSideEffectDispatch):
        def start(self, *args, before_mutation):
            return ExecutorRef(args[5], args[1], args[2], "dispatch", None, args[6], "e", "c" * 64)

    _configure(_OwnedEngine(), SkipsCallback())
    assert _claim_run(db, run.run_id) == (run.run_id, 1)
    with pytest.raises(RuntimeError):
        supervisor.start(db, run.run_id, 1)
    row = db.execute(
        text("SELECT state, proof FROM runtime_executors WHERE run_id=:run AND kind='dispatch'"),
        {"run": run.run_id},
    ).mappings().one()
    assert row["state"] == "unknown"
    assert row["proof"].get("source") != "dispatch-launch-not-attempted"


def test_rejection_raised_outside_dispatch_start_is_not_no_launch_proof(db, project_session):
    run, _, _ = _approved_run(db, project_session)

    class Late(_PossibleSideEffectDispatch):
        def start(self, *args, before_mutation):
            before_mutation("b5-owned-engine-stable-id", None)
            return ExecutorRef(args[5], args[1], args[2], "dispatch", None, args[6], "b5-owned-engine-stable-id", "c" * 64)

    def reject(*_):
        raise DispatchPreLaunchRejected("late typed rejection")

    _configure(_OwnedEngine(), Late(), reject)
    assert _claim_run(db, run.run_id) == (run.run_id, 1)
    with pytest.raises(RuntimeError):
        supervisor.start(db, run.run_id, 1)
    row = db.execute(
        text("SELECT proof FROM runtime_executors WHERE run_id=:run AND kind='dispatch'"),
        {"run": run.run_id},
    ).mappings().one()
    assert row["proof"].get("source") != "dispatch-launch-not-attempted"
