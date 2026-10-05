from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from scientist import checkpoints, objects
from scientist.contracts import PlanSpec, Principal
from scientist.db import create_project, create_session, engine, migrate, session
from scientist.domain import approve_run, revise_plan, submit_run
from scientist.runtime_contracts import RuntimeContextV1, RUNTIME_COMMIT
from scientist import supervisor
from scientist.supervisor import ExecutorRef


class _MemoryS3:
    def __init__(self):
        self.data = {}

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.data[(Bucket, Key)] = bytes(Body)

    def head_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.data:
            raise ClientError({"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
        return {"ContentLength": len(self.data[(Bucket, Key)])}

    def get_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.data:
            raise ClientError({"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "GetObject")
        return {"Body": BytesIO(self.data[(Bucket, Key)])}


def _approved_run(db, project_session):
    project_id, session_id = project_session
    owner = Principal(identity=uuid4(), kind="owner")
    provider_id = uuid4()
    run = submit_run(db, owner, project_id, session_id, "checkpoint-run", "question", [], provider_id, "fixture")
    snapshot = db.execute(text("SELECT digest FROM input_snapshots WHERE run_id=:run"),
                          {"run": run.run_id}).scalar_one().strip()
    plan = PlanSpec(input_snapshot_digest=snapshot, provider_id=provider_id, model="fixture",
                    stages=["search"], allowed_ops=["llm", "search"],
                    data_recipients=["https://research.example"], packages=[],
                    token_limit=1000, elapsed_limit_ms=60000)
    run = revise_plan(db, owner, run.run_id, run.revision, plan)
    return approve_run(db, owner, run.run_id, run.revision, run.plan_digest), plan


@pytest.fixture
def object_fixture(monkeypatch):
    client = _MemoryS3()
    monkeypatch.setattr(objects, "_client", lambda: client)
    return client


def _context(run, plan, workspace_path: Path, *, boundary="before_model"):
    project_id = run.project_id
    snapshot = plan.input_snapshot_digest
    data = (workspace_path / "result.txt").read_bytes()
    body = {
        "schema_version": 1, "run_id": str(run.run_id), "project_id": str(project_id),
        "generation": 1, "revision": run.revision, "input_snapshot_digest": snapshot,
        "plan_digest": run.plan_digest, "runtime_commit": RUNTIME_COMMIT,
        "image_digest": "sha256:" + "b" * 64, "skills_digest": "c" * 64,
        "environment_digest": "d" * 64, "provider_id": str(plan.provider_id),
        "provider_endpoint": plan.data_recipients[0], "model": plan.model,
        "plan": plan.model_dump(mode="json"), "turn_id": str(uuid4()),
        "system_prompt": "Trusted research system prompt",
        "messages": [{"role": "user", "content": "Question"}],
        "todo": {"todos": [], "revision": 0}, "compacted_context": None,
        "boundary": boundary, "pending_assistant": None,
        "operation_mappings": [], "operation_sequence": 0,
        "workspace_manifest": [{"path": "result.txt", "sha256": hashlib.sha256(data).hexdigest(),
                                "size": len(data)}],
    }
    return RuntimeContextV1.model_validate(body).model_dump_json().encode()


def test_capture_does_not_commit_and_restore_verifies_every_object(
    db, project_session, object_fixture, tmp_path,
):
    run, plan = _approved_run(db, project_session)
    db.execute(text("UPDATE runs SET state='running', generation=1, lease_expires_at=now()+interval '1 hour' WHERE id=:run"),
               {"run": run.run_id})
    db.commit()
    checkpoints.configure_trusted_pins(image_digest="sha256:" + "b" * 64,
                                      skills_digest="c" * 64, environment_digest="d" * 64)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "result.txt").write_text("durable result", encoding="utf-8")

    try:
        manifest = checkpoints.capture(db, run.run_id, 1, _context(run, plan, workspace), workspace)
        assert manifest.revision == 1
        assert len(manifest.workspace) == 1
        external = engine().connect()
        try:
            assert external.execute(text("SELECT 1 FROM checkpoints WHERE run_id=:run"),
                                    {"run": run.run_id}).scalar_one_or_none() is None
        finally:
            external.close()

        db.commit()
        restored_dir = tmp_path / "restored"
        context = checkpoints.restore(db, manifest, restored_dir)
        assert RuntimeContextV1.model_validate_json(context).run_id == run.run_id
        assert (restored_dir / "result.txt").read_text(encoding="utf-8") == "durable result"

        # Corruption is discovered before the current workspace is replaced.
        (restored_dir / "keep.txt").write_text("preserve", encoding="utf-8")
        object_fixture.data[(objects.BUCKET, manifest.workspace[0].key)] = b"corrupt"
        with pytest.raises(checkpoints.CheckpointIntegrityError):
            checkpoints.restore(db, manifest, restored_dir)
        assert (restored_dir / "keep.txt").read_text(encoding="utf-8") == "preserve"
    finally:
        db.rollback()
        db.execute(text("UPDATE runs SET state='failed', lease_expires_at=NULL WHERE id=:run"),
                   {"run": run.run_id})
        db.commit()


def test_claim_skips_unapproved_queue_and_respects_active_capacity():
    schema = f"claim_{uuid4().hex}"
    with engine().begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated_engine = create_engine(
        engine().url,
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    claim_db = Session(isolated_engine, expire_on_commit=False)
    try:
        migrate(isolated_engine)
        project_id = create_project(claim_db, "claim regression")
        session_id = create_session(claim_db, project_id, "claim regression")
        owner = Principal(identity=uuid4(), kind="owner")
        provider_id = uuid4()
        unapproved_run = submit_run(
            claim_db, owner, project_id, session_id, "unapproved-claim", "question", [],
            provider_id, "fixture",
        )
        snapshot = claim_db.execute(text(
            "SELECT digest FROM input_snapshots WHERE run_id=:run"
        ), {"run": unapproved_run.run_id}).scalar_one().strip()
        unapproved_plan = PlanSpec(
            input_snapshot_digest=snapshot, provider_id=provider_id, model="fixture",
            stages=["search"], allowed_ops=["llm", "search"],
            data_recipients=["https://research.example"], packages=[],
            token_limit=1000, elapsed_limit_ms=60000,
        )
        unapproved_run = revise_plan(
            claim_db, owner, unapproved_run.run_id, unapproved_run.revision, unapproved_plan
        )
        claim_db.execute(text("UPDATE runs SET state='queued' WHERE id=:run"),
                         {"run": unapproved_run.run_id})
        claim_db.commit()

        approved, _plan = _approved_run(claim_db, (project_id, session_id))
        claim_db.commit()
        claim_db.execute(text("SELECT pg_sleep(0.01)"))
        claim_db.commit()
        later_approved, _plan = _approved_run(claim_db, (project_id, session_id))
        claim_db.commit()

        active_owner = Principal(identity=uuid4(), kind="owner")
        active_run = submit_run(
            claim_db, active_owner, project_id, session_id, "active-claim-capacity",
            "question", [], uuid4(), "fixture",
        )
        claim_db.execute(text(
            "UPDATE runs SET state='running',generation=7 WHERE id=:run"
        ), {"run": active_run.run_id})
        claim_db.commit()

        assert supervisor.claim(claim_db, max_active=2) == (approved.run_id, 1)
        claimed = claim_db.execute(text("SELECT state,generation FROM runs WHERE id=:run"),
                                   {"run": approved.run_id}).one()
        assert (claimed.state, claimed.generation) == ("running", 1)
        assert claim_db.execute(text("SELECT state FROM runs WHERE id=:run"),
                                {"run": unapproved_run.run_id}).scalar_one() == "queued"
        assert claim_db.execute(text("SELECT state FROM runs WHERE id=:run"),
                                {"run": later_approved.run_id}).scalar_one() == "queued"
        assert claim_db.execute(text("SELECT count(*) FROM events WHERE run_id=:run AND kind='run.state' AND payload->>'state'='running'"),
                                {"run": approved.run_id}).scalar_one() == 1
        assert supervisor.claim(claim_db, max_active=2) is None
    finally:
        claim_db.rollback()
        claim_db.close()
        isolated_engine.dispose()
        with engine().begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.mark.parametrize("entry", ["symlink", "hardlink"])
def test_capture_workspace_rejects_linked_files(entry, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    source = tmp_path / "source"
    source.write_text("data", encoding="utf-8")
    if entry == "symlink":
        (root / "escape").symlink_to(source)
    else:
        (root / "linked").hardlink_to(source)
    with pytest.raises((ValueError, checkpoints.CheckpointIntegrityError)):
        checkpoints._read_workspace(root)


def _prepare_recovery_run(db, project_session, object_fixture, tmp_path, *, boundary="before_model"):
    run, plan = _approved_run(db, project_session)
    db.execute(text("UPDATE runs SET state='running', generation=1, lease_expires_at=now()+interval '1 hour' WHERE id=:run"),
               {"run": run.run_id})
    db.commit()
    checkpoints.configure_trusted_pins(
        image_digest="sha256:" + "b" * 64,
        skills_digest="c" * 64,
        environment_digest="d" * 64,
    )
    workspace = tmp_path / f"workspace-{run.run_id.hex}"
    workspace.mkdir()
    (workspace / "result.txt").write_text("durable result", encoding="utf-8")
    checkpoints.capture(db, run.run_id, 1, _context(run, plan, workspace, boundary=boundary), workspace)
    db.execute(text("UPDATE runs SET lease_expires_at=now()-interval '1 second' WHERE id=:run"),
               {"run": run.run_id})
    db.commit()
    return run


def _insert_executor(db, run_id, ref, state, *, bound):
    db.execute(text("""
        INSERT INTO runtime_executors
          (id, run_id, generation, kind, operation_id, process_incarnation,
           container_id, engine_id, state)
        VALUES (:id, :run, :generation, :kind, NULL, :incarnation,
                :container, :engine, :state)
    """), {
        "id": ref.executor_id,
        "run": run_id,
        "generation": ref.generation,
        "kind": ref.kind,
        "incarnation": ref.process_incarnation,
        "container": ref.container_id if bound else None,
        "engine": ref.engine_id if bound or ref.kind == "worker" else None,
        "state": state,
    })


def test_recover_final_quiescent_checkpoint_completes_once(
    db, project_session, object_fixture, tmp_path, monkeypatch
):
    run = _prepare_recovery_run(
        db, project_session, object_fixture, tmp_path, boundary="final"
    )
    queued_events_before = db.execute(
        text("SELECT count(*) FROM events WHERE run_id=:run AND kind='run.state' AND payload->>'state'='queued'"),
        {"run": run.run_id},
    ).scalar_one()
    monkeypatch.setattr(
        supervisor, "_config", SimpleNamespace(engine=object(), dispatch=object())
    )

    recovered = supervisor.recover(db, run.run_id)

    assert recovered.state == "completed"
    assert recovered.waiting_reason is None
    assert db.execute(
        text("SELECT state FROM runs WHERE id=:run"), {"run": run.run_id}
    ).scalar_one() == "completed"
    assert db.execute(
        text("SELECT count(*) FROM events WHERE run_id=:run AND kind='run.state' AND payload->>'state'='completed'"),
        {"run": run.run_id},
    ).scalar_one() == 1
    assert db.execute(
        text("SELECT count(*) FROM events WHERE run_id=:run AND kind='run.state' AND payload->>'state'='queued'"),
        {"run": run.run_id},
    ).scalar_one() == queued_events_before


def test_recover_pauses_at_exhausted_elapsed_budget_after_proving_quiescence(
    db, project_session, object_fixture, tmp_path, monkeypatch
):
    run = _prepare_recovery_run(
        db, project_session, object_fixture, tmp_path, boundary="before_model"
    )
    db.execute(text("""
        UPDATE runs SET elapsed_used_ms=elapsed_limit_ms,
            elapsed_active_since=clock_timestamp()-interval '1 second'
        WHERE id=:run
    """), {"run": run.run_id})
    db.commit()
    monkeypatch.setattr(
        supervisor, "_config", SimpleNamespace(engine=object(), dispatch=object())
    )

    recovered = supervisor.recover(db, run.run_id)

    assert recovered.state == "waiting_input"
    assert recovered.waiting_reason == "budget_exhausted"
    assert db.execute(text("""
        SELECT elapsed_active_since IS NULL, elapsed_used_ms >= elapsed_limit_ms
        FROM runs WHERE id=:run
    """), {"run": run.run_id}).one() == (True, True)
    assert db.execute(text("""
        SELECT count(*) FROM events WHERE run_id=:run AND kind='decision.required'
          AND payload->>'reason'='budget_exhausted'
    """), {"run": run.run_id}).scalar_one() == 1


def test_recover_final_checkpoint_completion_wins_at_elapsed_ceiling(
    db, project_session, object_fixture, tmp_path, monkeypatch
):
    run = _prepare_recovery_run(
        db, project_session, object_fixture, tmp_path, boundary="final"
    )
    db.execute(text("""
        UPDATE runs SET elapsed_used_ms=elapsed_limit_ms,
            elapsed_active_since=clock_timestamp()-interval '1 second'
        WHERE id=:run
    """), {"run": run.run_id})
    db.commit()
    monkeypatch.setattr(
        supervisor, "_config", SimpleNamespace(engine=object(), dispatch=object())
    )

    recovered = supervisor.recover(db, run.run_id)

    assert recovered.state == "completed"
    assert recovered.waiting_reason is None
    assert db.execute(text("""
        SELECT elapsed_active_since IS NULL FROM runs WHERE id=:run
    """), {"run": run.run_id}).scalar_one() is True
    assert db.execute(text("""
        SELECT count(*) FROM events WHERE run_id=:run AND kind='decision.required'
          AND payload->>'reason'='budget_exhausted'
    """), {"run": run.run_id}).scalar_one() == 0


def test_recover_stale_final_checkpoint_fails_closed(
    db, project_session, object_fixture, tmp_path, monkeypatch
):
    run = _prepare_recovery_run(
        db, project_session, object_fixture, tmp_path, boundary="final"
    )
    db.execute(text("UPDATE runs SET generation=2 WHERE id=:run"), {"run": run.run_id})
    db.commit()
    monkeypatch.setattr(
        supervisor, "_config", SimpleNamespace(engine=object(), dispatch=object())
    )

    recovered = supervisor.recover(db, run.run_id)

    assert (recovered.state, recovered.waiting_reason) == (
        "waiting_input", "checkpoint_integrity_unproven"
    )


def test_recover_rejects_inactive_executor_without_proof(
    db, project_session, object_fixture, tmp_path, monkeypatch
):
    run = _prepare_recovery_run(
        db, project_session, object_fixture, tmp_path, boundary="final"
    )
    executor = ExecutorRef(uuid4(), run.run_id, 1, "worker", None, uuid4(), "engine-test", "3" * 64)
    _insert_executor(db, run.run_id, executor, "inactive", bound=True)
    db.commit()
    monkeypatch.setattr(
        supervisor, "_config", SimpleNamespace(engine=object(), dispatch=object())
    )

    recovered = supervisor.recover(db, run.run_id)

    assert (recovered.state, recovered.waiting_reason) == (
        "waiting_input", "executor_quiescence_unproven"
    )


def test_recover_does_not_complete_final_checkpoint_with_unknown_operation(
    db, project_session, object_fixture, tmp_path, monkeypatch
):
    run = _prepare_recovery_run(
        db, project_session, object_fixture, tmp_path, boundary="final"
    )
    db.execute(text("""
        INSERT INTO operations(id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens)
        VALUES (:id, :run, 'final-unknown-effect', 1, 'search', :hash, 'unknown', 9)
    """), {"id": uuid4(), "run": run.run_id, "hash": "f" * 64})
    db.commit()
    monkeypatch.setattr(
        supervisor, "_config", SimpleNamespace(engine=object(), dispatch=object())
    )

    recovered = supervisor.recover(db, run.run_id)

    assert (recovered.state, recovered.waiting_reason) == (
        "waiting_input", "unknown_outcome"
    )
    assert db.execute(
        text("SELECT count(*) FROM events WHERE run_id=:run AND kind='run.state' AND payload->>'state'='completed'"),
        {"run": run.run_id},
    ).scalar_one() == 0


def test_stop_records_exact_worker_inactive_proof(
    db, project_session, monkeypatch
):
    run, _ = _approved_run(db, project_session)
    db.execute(
        text("""UPDATE runs SET state='running', generation=1, elapsed_used_ms=11,
            elapsed_active_since=clock_timestamp()-interval '2 seconds' WHERE id=:run"""),
        {"run": run.run_id},
    )
    worker_ref = ExecutorRef(uuid4(), run.run_id, 1, "worker", None, uuid4(), "engine-test", "1" * 64)
    dispatch_ref = ExecutorRef(uuid4(), run.run_id, 1, "dispatch", None, uuid4(), "engine-test", "2" * 64)
    _insert_executor(db, run.run_id, worker_ref, "active", bound=True)
    _insert_executor(db, run.run_id, dispatch_ref, "active", bound=True)
    db.commit()

    class Engine:
        def stop_worker(self, ref, grace_seconds):
            assert ref == worker_ref
            assert grace_seconds == 0
            return True

    class Dispatch:
        def stop(self, db, ref, grace_seconds):
            assert ref == dispatch_ref
            assert grace_seconds == 0
            return True

    monkeypatch.setattr(supervisor, "_config", SimpleNamespace(engine=Engine(), dispatch=Dispatch()))
    stopped = supervisor.stop(db, run.run_id, 0)

    assert stopped.state == "canceled", stopped.waiting_reason
    elapsed_used, elapsed_active_since = db.execute(text("""
        SELECT elapsed_used_ms, elapsed_active_since FROM runs WHERE id=:run
    """), {"run": run.run_id}).one()
    assert elapsed_active_since is None
    assert elapsed_used >= 1_900
    rows = db.execute(
        text("SELECT kind, state, proof FROM runtime_executors WHERE run_id=:run ORDER BY kind"),
        {"run": run.run_id},
    ).mappings().all()
    assert len(rows) == 2
    assert all(row["state"] == "inactive" and row["proof"] for row in rows)
    worker = next(row for row in rows if row["kind"] == "worker")
    assert worker["proof"]["container_id"] == worker_ref.container_id
    assert worker["proof"]["engine_id"] == worker_ref.engine_id


@pytest.mark.parametrize(("state", "bound"), [("starting", False), ("unknown", False), ("active", True)])
def test_recover_exactly_reaps_previous_generation_and_claims_next(
    db, project_session, object_fixture, tmp_path, monkeypatch, state, bound
):
    run = _prepare_recovery_run(db, project_session, object_fixture, tmp_path)
    worker_ref = ExecutorRef(uuid4(), run.run_id, 1, "worker", None, uuid4(), "engine-test", "a" * 64)
    dispatch_ref = ExecutorRef(uuid4(), run.run_id, 1, "dispatch", None, uuid4(), "engine-test", "b" * 64)
    _insert_executor(db, run.run_id, worker_ref, state, bound=bound)
    _insert_executor(db, run.run_id, dispatch_ref, state, bound=bound)
    db.commit()

    class Engine:
        found = []
        stopped = []

        def find_worker(self, run_id, generation, executor_id, incarnation, engine_id):
            self.found.append((run_id, generation, executor_id, incarnation, engine_id))
            return worker_ref

        def stop_worker(self, ref, grace_seconds):
            assert ref == worker_ref
            assert grace_seconds == 0
            self.stopped.append(ref)
            return True

    class Dispatch:
        found = []
        stopped = []

        def find(self, db, run_id, generation, executor_id, operation_id, incarnation):
            self.found.append((run_id, generation, executor_id, operation_id, incarnation))
            return dispatch_ref

        def stop(self, db, ref, grace_seconds):
            assert ref == dispatch_ref
            assert grace_seconds == 0
            self.stopped.append(ref)
            return True

        def inactive(self, db, ref, operation_id):
            raise AssertionError("there are no pending operations to probe")

    engine, dispatch = Engine(), Dispatch()
    monkeypatch.setattr(supervisor, "_config", SimpleNamespace(engine=engine, dispatch=dispatch))

    supervisor.recover(db, run.run_id)

    rows = db.execute(text("""
        SELECT kind, state, container_id, engine_id, proof
        FROM runtime_executors WHERE run_id=:run AND generation=1 ORDER BY kind
    """), {"run": run.run_id}).mappings().all()
    assert len(rows) == 2
    assert all(row["state"] == "inactive" for row in rows)
    assert {row["container_id"] for row in rows} == {worker_ref.container_id, dispatch_ref.container_id}
    assert {row["engine_id"] for row in rows} == {"engine-test"}
    assert all(row["proof"]["container_id"] in {worker_ref.container_id, dispatch_ref.container_id} for row in rows)
    assert len(engine.stopped) == len(dispatch.stopped) == 1
    assert len(engine.found) == (0 if bound else 1)
    assert len(dispatch.found) == (0 if bound else 1)

    recovered = db.execute(text("SELECT state, waiting_reason FROM runs WHERE id=:run"),
                           {"run": run.run_id}).one()
    assert recovered.state == "queued", (recovered.state, recovered.waiting_reason)
    claim_eligible = db.execute(text("""
        SELECT EXISTS (
            SELECT 1 FROM runs r WHERE r.id=:run AND r.state='queued'
              AND r.cancel_requested=false AND r.plan_digest IS NOT NULL
              AND EXISTS (SELECT 1 FROM approvals a WHERE a.run_id=r.id
                AND a.revision=r.revision AND a.plan_digest=r.plan_digest)
        )
    """), {"run": run.run_id}).scalar_one()
    assert claim_eligible is True
    assert db.execute(text("SELECT generation FROM runs WHERE id=:run"), {"run": run.run_id}).scalar_one() == 1
    assert db.execute(text("SELECT count(*) FROM runtime_executors WHERE run_id=:run AND state='active' AND generation=1"),
                      {"run": run.run_id}).scalar_one() == 0


def test_recover_failed_dispatch_stop_keeps_reservation_and_generation_paused(
    db, project_session, object_fixture, tmp_path, monkeypatch
):
    run = _prepare_recovery_run(db, project_session, object_fixture, tmp_path)
    worker_ref = ExecutorRef(uuid4(), run.run_id, 1, "worker", None, uuid4(), "engine-test", "c" * 64)
    dispatch_ref = ExecutorRef(uuid4(), run.run_id, 1, "dispatch", None, uuid4(), "engine-test", "d" * 64)
    _insert_executor(db, run.run_id, worker_ref, "active", bound=True)
    _insert_executor(db, run.run_id, dispatch_ref, "active", bound=True)
    operation_id = "recovery-reserved-operation"
    db.execute(text("""
        INSERT INTO operations(id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens)
        VALUES (:id, :run, :operation, 1, 'search', :hash, 'reserved', 7)
    """), {"id": uuid4(), "run": run.run_id, "operation": operation_id, "hash": "e" * 64})
    db.commit()

    class Engine:
        def stop_worker(self, ref, grace_seconds):
            assert ref == worker_ref
            return True

    class Dispatch:
        def stop(self, db, ref, grace_seconds):
            assert ref == dispatch_ref
            return False

    monkeypatch.setattr(supervisor, "_config", SimpleNamespace(engine=Engine(), dispatch=Dispatch()))

    supervisor.recover(db, run.run_id)

    current = db.execute(text("SELECT state, waiting_reason, generation FROM runs WHERE id=:run"),
                         {"run": run.run_id}).one()
    assert (current.state, current.waiting_reason, current.generation) == (
        "waiting_input", "executor_quiescence_unproven", 1
    )
    operation = db.execute(text("SELECT state, reserve_tokens FROM operations WHERE run_id=:run AND operation_id=:op"),
                           {"run": run.run_id, "op": operation_id}).one()
    assert (operation.state, operation.reserve_tokens) == ("reserved", 7)
    assert db.execute(text("SELECT state FROM runtime_executors WHERE id=:id"),
                      {"id": dispatch_ref.executor_id}).scalar_one() == "unknown"
    db.rollback()
    assert db.execute(text("SELECT state, generation FROM runs WHERE id=:run"),
                      {"run": run.run_id}).one() == ("waiting_input", 1)


def test_recover_retries_unknown_unbound_identity_after_engine_discovery_recovers(
    db, project_session, object_fixture, tmp_path, monkeypatch
):
    run = _prepare_recovery_run(db, project_session, object_fixture, tmp_path)
    worker_ref = ExecutorRef(uuid4(), run.run_id, 1, "worker", None, uuid4(), "engine-test", "e" * 64)
    dispatch_ref = ExecutorRef(uuid4(), run.run_id, 1, "dispatch", None, uuid4(), "engine-test", "f" * 64)
    _insert_executor(db, run.run_id, worker_ref, "unknown", bound=False)
    _insert_executor(db, run.run_id, dispatch_ref, "unknown", bound=False)
    db.commit()

    class Engine:
        failures = 0
        stopped = []

        def find_worker(self, run_id, generation, executor_id, incarnation, engine_id):
            assert (run_id, generation, executor_id, incarnation, engine_id) == (
                worker_ref.run_id, worker_ref.generation, worker_ref.executor_id,
                worker_ref.process_incarnation, worker_ref.engine_id,
            )
            if self.failures:
                self.failures -= 1
                raise RuntimeError("owned engine discovery temporarily unavailable")
            return worker_ref

        def stop_worker(self, ref, grace_seconds):
            assert ref == worker_ref
            assert grace_seconds == 0
            self.stopped.append(ref)
            return True

    class Dispatch:
        stopped = []

        def find(self, db, run_id, generation, executor_id, operation_id, incarnation):
            assert (run_id, generation, executor_id, operation_id, incarnation) == (
                dispatch_ref.run_id, dispatch_ref.generation, dispatch_ref.executor_id,
                None, dispatch_ref.process_incarnation,
            )
            return dispatch_ref

        def stop(self, db, ref, grace_seconds):
            assert ref == dispatch_ref
            assert grace_seconds == 0
            self.stopped.append(ref)
            return True

        def inactive(self, db, ref, operation_id):
            raise AssertionError("there are no pending operations to probe")

    engine, dispatch = Engine(), Dispatch()
    engine.failures = 1
    monkeypatch.setattr(supervisor, "_config", SimpleNamespace(engine=engine, dispatch=dispatch))

    supervisor.recover(db, run.run_id)
    first_state = db.execute(text("SELECT state, waiting_reason, generation FROM runs WHERE id=:run"),
                             {"run": run.run_id}).one()
    assert (first_state.state, first_state.waiting_reason, first_state.generation) == (
        "waiting_input", "executor_quiescence_unproven", 1
    )
    assert db.execute(text("SELECT state FROM runtime_executors WHERE id=:id"),
                      {"id": worker_ref.executor_id}).scalar_one() == "unknown"
    assert db.execute(text("SELECT state FROM runtime_executors WHERE id=:id"),
                      {"id": dispatch_ref.executor_id}).scalar_one() == "inactive"
    assert dispatch.stopped == [dispatch_ref]
    assert engine.stopped == []

    supervisor.recover(db, run.run_id)

    rows = db.execute(text("""
        SELECT state, container_id, engine_id, proof FROM runtime_executors
        WHERE run_id=:run AND generation=1 ORDER BY kind
    """), {"run": run.run_id}).mappings().all()
    assert len(rows) == 2
    assert all(row["state"] == "inactive" for row in rows)
    assert {row["container_id"] for row in rows} == {worker_ref.container_id, dispatch_ref.container_id}
    assert all(row["proof"]["container_id"] in {worker_ref.container_id, dispatch_ref.container_id} for row in rows)
    final_state = db.execute(text("SELECT state, waiting_reason, generation FROM runs WHERE id=:run"),
                             {"run": run.run_id}).one()
    assert (final_state.state, final_state.waiting_reason, final_state.generation) == ("queued", None, 1)
    assert engine.stopped == [worker_ref]
    assert db.execute(text("SELECT count(*) FROM runtime_executors WHERE run_id=:run AND generation=1 AND state='active'"),
                      {"run": run.run_id}).scalar_one() == 0


def _recover_with_budget_decision(db, project_session, object_fixture, tmp_path, monkeypatch, decision):
    run = _prepare_recovery_run(db, project_session, object_fixture, tmp_path, boundary="before_model")
    db.execute(text("""
        UPDATE runs SET state='waiting_input', waiting_reason='budget_exhausted',
            budget_decision_id=:decision, usage_tokens=10, elapsed_used_ms=0,
            elapsed_active_since=NULL, lease_expires_at=NULL
        WHERE id=:run
    """), {"run": run.run_id, "decision": decision})
    db.commit()
    monkeypatch.setattr(supervisor, "_config", SimpleNamespace(engine=object(), dispatch=object()))
    before = _snapshot(db, run.run_id)
    return run, supervisor.recover(db, run.run_id), before


def _snapshot(db, run_id):
    events = db.execute(text("""
        SELECT count(*) FILTER (WHERE kind='decision.required'),
               count(*) FILTER (WHERE kind='run.state')
        FROM events WHERE run_id=:run"""), {"run": run_id}).one()
    usage = db.execute(text("""
        SELECT usage_tokens, reserved_tokens, elapsed_used_ms FROM runs WHERE id=:run"""),
        {"run": run_id}).one()
    return tuple(events), tuple(usage)


def _queued_events(db, run_id):
    return db.execute(text("""
        SELECT count(*) FROM events WHERE run_id=:run AND kind='run.state'
          AND payload->>'state'='queued'"""), {"run": run_id}).scalar_one()


def test_recover_keeps_pending_owner_budget_decision_with_tokens_remaining(
    db, project_session, object_fixture, tmp_path, monkeypatch
):
    decision = uuid4()
    run, recovered, before = _recover_with_budget_decision(
        db, project_session, object_fixture, tmp_path, monkeypatch, decision)

    assert recovered.state == "waiting_input"
    assert recovered.waiting_reason == "budget_exhausted"
    assert db.execute(text("SELECT budget_decision_id FROM runs WHERE id=:run"),
                      {"run": run.run_id}).scalar_one() == decision
    assert _snapshot(db, run.run_id) == before


def test_recover_requeues_budget_wait_after_owner_decision_consumed(
    db, project_session, object_fixture, tmp_path, monkeypatch
):
    _, recovered, _ = _recover_with_budget_decision(
        db, project_session, object_fixture, tmp_path, monkeypatch, None)

    assert recovered.state == "queued"


def _decision_run(db, project_session, object_fixture, tmp_path, usage, *, worker_state="active"):
    run = _prepare_recovery_run(db, project_session, object_fixture, tmp_path)
    decision = uuid4()
    db.execute(text("""UPDATE runs SET state='waiting_input', waiting_reason='budget_exhausted',
        budget_decision_id=:d, usage_tokens=CASE WHEN :u < 0 THEN token_limit ELSE :u END,
        elapsed_used_ms=0, elapsed_active_since=NULL, lease_expires_at=NULL WHERE id=:run"""),
        {"run": run.run_id, "d": decision, "u": usage})
    worker = ExecutorRef(uuid4(), run.run_id, 1, "worker", None, uuid4(), "engine-test", "c" * 64)
    _insert_executor(db, run.run_id, worker, worker_state, bound=True)
    db.commit()
    return run, decision


def _flaky_stop_engine(monkeypatch):
    calls = []

    class Engine:
        def stop_worker(self, ref, grace):
            calls.append(ref)
            return len(calls) > 1

    monkeypatch.setattr(supervisor, "_config", SimpleNamespace(engine=Engine(), dispatch=object()))


def _run_row(db, run_id):
    db.rollback()
    return db.execute(text("SELECT state, waiting_reason, budget_decision_id, generation FROM runs WHERE id=:r"),
                      {"r": run_id}).one()


def test_two_step_recover_through_quiescence_keeps_owner_decision(
    db, project_session, object_fixture, tmp_path, monkeypatch
):
    run, decision = _decision_run(db, project_session, object_fixture, tmp_path, 10)
    _flaky_stop_engine(monkeypatch)

    first = supervisor.recover(db, run.run_id)
    assert first.waiting_reason == "executor_quiescence_unproven"
    second = supervisor.recover(db, run.run_id)

    row = _run_row(db, run.run_id)
    assert (row.state, row.waiting_reason, row.budget_decision_id) == (
        "waiting_input", "budget_exhausted", decision)
    assert second.state == "waiting_input"
    assert _run_row(db, run.run_id).generation == 1


def test_recover_of_resolved_queued_run_keeps_pending_owner_decision(
    db, project_session, object_fixture, tmp_path, monkeypatch
):
    # Shape left by broker.resolve after unknown_outcome: queued, decision still set.
    run, decision = _decision_run(db, project_session, object_fixture, tmp_path, 10)
    db.execute(text("DELETE FROM runtime_executors WHERE run_id=:r"), {"r": run.run_id})
    db.execute(text("UPDATE runs SET state='queued', waiting_reason=NULL WHERE id=:r"), {"r": run.run_id})
    db.commit()
    monkeypatch.setattr(supervisor, "_config", SimpleNamespace(engine=object(), dispatch=object()))

    supervisor.recover(db, run.run_id)

    row = _run_row(db, run.run_id)
    assert (row.state, row.waiting_reason, row.budget_decision_id) == (
        "waiting_input", "budget_exhausted", decision)


def test_exhausted_run_with_overwritten_reason_accepts_owner_extension(
    db, project_session, object_fixture, tmp_path, monkeypatch
):
    from scientist.domain import extend_run_budget

    run, decision = _decision_run(db, project_session, object_fixture, tmp_path, -1)
    _flaky_stop_engine(monkeypatch)
    supervisor.recover(db, run.run_id)
    supervisor.recover(db, run.run_id)

    row = _run_row(db, run.run_id)
    assert (row.state, row.waiting_reason, row.budget_decision_id) == (
        "waiting_input", "budget_exhausted", decision)
    revision = db.execute(text("SELECT revision FROM runs WHERE id=:r"), {"r": run.run_id}).scalar_one()
    owner = Principal(identity=uuid4(), kind="owner")
    limit = db.execute(text("SELECT token_limit FROM runs WHERE id=:r"), {"r": run.run_id}).scalar_one()
    extend_run_budget(db, owner, run.run_id, revision, decision, "ext-1", limit + 500, 120000)


def test_claim_refuses_queued_run_with_pending_budget_decision():
    schema = f"claim_{uuid4().hex}"
    with engine().begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = create_engine(engine().url, connect_args={"options": f"-csearch_path={schema}"}, poolclass=NullPool)
    claim_db = Session(isolated, expire_on_commit=False)
    try:
        migrate(isolated)
        project_id = create_project(claim_db, "claim decision")
        session_id = create_session(claim_db, project_id, "claim decision")
        run, _ = _approved_run(claim_db, (project_id, session_id))
        decision = uuid4()
        claim_db.execute(text("UPDATE runs SET state='queued', budget_decision_id=:d WHERE id=:r"),
                         {"r": run.run_id, "d": decision})
        claim_db.commit()

        assert supervisor.claim(claim_db, max_active=3) is None
        row = _run_row(claim_db, run.run_id)
        assert row.state == "waiting_input" and row.budget_decision_id == decision and row.generation == 0
    finally:
        claim_db.close()
        isolated.dispose()
        with engine().begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def _reserved_stop_run(db, project_session, monkeypatch, *, worker_ok=True, dispatch_ok=True):
    run, _ = _approved_run(db, project_session)
    db.execute(text("UPDATE runs SET state='running', generation=1, reserved_tokens=7 WHERE id=:run"),
               {"run": run.run_id})
    worker_ref = ExecutorRef(uuid4(), run.run_id, 1, "worker", None, uuid4(), "engine-test", "1" * 64)
    dispatch_ref = ExecutorRef(uuid4(), run.run_id, 1, "dispatch", None, uuid4(), "engine-test", "2" * 64)
    _insert_executor(db, run.run_id, worker_ref, "active", bound=True)
    _insert_executor(db, run.run_id, dispatch_ref, "active", bound=True)
    db.execute(text("""
        INSERT INTO operations(id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens)
        VALUES (:id, :run, 'hung-stop-op', 1, 'search', :hash, 'reserved', 7)
    """), {"id": uuid4(), "run": run.run_id, "hash": "e" * 64})
    db.execute(text("""INSERT INTO operation_executors(run_id, operation_id, generation, executor_id)
        VALUES (:run, 'hung-stop-op', 1, :executor)"""), {"run": run.run_id, "executor": dispatch_ref.executor_id})
    db.commit()
    flags = SimpleNamespace(worker_ok=worker_ok, dispatch_ok=dispatch_ok, during_stop=None)

    class Engine:
        def stop_worker(self, ref, grace_seconds):
            return flags.worker_ok

    class Dispatch:
        def stop(self, db, ref, grace_seconds):
            if flags.during_stop:
                flags.during_stop()
            return flags.dispatch_ok

        def inactive(self, db, ref, operation_id):
            return True

    monkeypatch.setattr(supervisor, "_config", SimpleNamespace(engine=Engine(), dispatch=Dispatch()))
    return run, flags


def _ops_and_budget(db, run_id):
    op = db.execute(text("SELECT state, reserve_tokens, usage_tokens, result FROM operations WHERE run_id=:run"),
                    {"run": run_id}).one()
    return tuple(op), db.execute(text("SELECT reserved_tokens FROM runs WHERE id=:run"), {"run": run_id}).scalar_one()


def test_stop_moves_reserved_operation_to_unknown_and_keeps_reservation(db, project_session, monkeypatch):
    run, _ = _reserved_stop_run(db, project_session, monkeypatch)
    before = _ops_and_budget(db, run.run_id)
    assert supervisor.stop(db, run.run_id, 0).state == "canceled"
    state, reserve, usage, result = _ops_and_budget(db, run.run_id)[0]
    assert (state, reserve, usage, result) == ("unknown", *before[0][1:])
    assert _ops_and_budget(db, run.run_id)[1] == before[1] == 7
    assert db.execute(text("SELECT count(*) FROM events WHERE run_id=:run AND kind='run.state' "
                           "AND payload->>'state'='canceled'"), {"run": run.run_id}).scalar_one() == 1


def test_stop_emits_no_canceled_event_when_run_left_stopping_during_grace(db, project_session, monkeypatch):
    run, flags = _reserved_stop_run(db, project_session, monkeypatch)

    def dying_dispatch_records_unknown():
        with session() as other:
            other.execute(text("UPDATE runs SET state='waiting_input', waiting_reason='unknown_outcome' WHERE id=:run"),
                          {"run": run.run_id})
            other.commit()

    flags.during_stop = dying_dispatch_records_unknown
    assert supervisor.stop(db, run.run_id, 0).state == "waiting_input"
    assert db.execute(text("SELECT count(*) FROM events WHERE run_id=:run AND kind='run.state' "
                           "AND payload->>'state'='canceled'"), {"run": run.run_id}).scalar_one() == 0


@pytest.mark.parametrize("fail", ["worker", "dispatch"])
def test_stop_keeps_reserved_operation_when_fence_unproven(db, project_session, monkeypatch, fail):
    run, _ = _reserved_stop_run(db, project_session, monkeypatch,
                                worker_ok=fail != "worker", dispatch_ok=fail != "dispatch")
    before = _ops_and_budget(db, run.run_id)
    stopped = supervisor.stop(db, run.run_id, 0)
    assert (stopped.state, stopped.waiting_reason) == ("waiting_input", "executor_quiescence_unproven")
    assert _ops_and_budget(db, run.run_id) == before
    assert before[0][0] == "reserved"


def test_recover_cancel_requested_moves_reserved_operation_to_unknown(db, project_session, monkeypatch):
    run, flags = _reserved_stop_run(db, project_session, monkeypatch, dispatch_ok=False)
    before = _ops_and_budget(db, run.run_id)
    assert supervisor.stop(db, run.run_id, 0).state == "waiting_input"
    assert _ops_and_budget(db, run.run_id) == before
    flags.dispatch_ok = True
    assert supervisor.recover(db, run.run_id).state == "canceled"
    after = _ops_and_budget(db, run.run_id)
    assert after[0][0] == "unknown" and after[0][1:] == before[0][1:] and after[1] == before[1] == 7
    assert db.execute(text("SELECT count(*) FROM owner_decisions WHERE run_id=:r AND state='pending' AND reason='unknown_outcome'"),
                      {"r": run.run_id}).scalar_one() == 1


def test_restore_reraises_storage_outage_but_corruption_stays_integrity(
    db, project_session, object_fixture, tmp_path, monkeypatch
):
    from scientist.auth import DomainError
    run = _prepare_recovery_run(db, project_session, object_fixture, tmp_path)
    manifest = checkpoints.CheckpointManifest.model_validate(db.execute(
        text("SELECT manifest FROM checkpoints WHERE run_id=:run"), {"run": run.run_id}).scalar_one())
    good = objects._client

    def outage():
        raise ConnectionError("storage down")

    monkeypatch.setattr(objects, "_client", outage)
    with pytest.raises(DomainError) as outage_error:
        checkpoints.restore(db, manifest, tmp_path / "r1")
    assert outage_error.value.code == "storage_unavailable"
    monkeypatch.setattr(objects, "_client", good)
    object_fixture.data[(objects.BUCKET, manifest.workspace[0].key)] = b"corrupt"
    with pytest.raises(checkpoints.CheckpointIntegrityError):
        checkpoints.restore(db, manifest, tmp_path / "r2")


def test_recover_during_storage_outage_waits_then_requeues_after_it_returns(
    db, project_session, object_fixture, tmp_path, monkeypatch
):
    run = _prepare_recovery_run(db, project_session, object_fixture, tmp_path)
    monkeypatch.setattr(supervisor, "_config", SimpleNamespace(engine=object(), dispatch=object()))
    good = objects._client

    def outage():
        raise ConnectionError("storage down")

    monkeypatch.setattr(objects, "_client", outage)
    recovered = supervisor.recover(db, run.run_id)
    assert (recovered.state, recovered.waiting_reason) == ("waiting_input", "storage_unavailable")
    monkeypatch.setattr(objects, "_client", good)
    recovered = supervisor.recover(db, run.run_id)
    assert (recovered.state, recovered.waiting_reason) == ("queued", None)


def test_containment_network_pick_skips_used_subnets(monkeypatch):
    import sys
    from unittest.mock import MagicMock
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "live"))
    monkeypatch.setitem(sys.modules, "b5_live_config", MagicMock())
    sys.modules.pop("b5_containment_acceptance", None)
    import b5_containment_acceptance as live
    try:
        assert live.pick_free_third_octet(0, set()) == 32
        assert live.pick_free_third_octet(5, {37, 38}) == 39
        assert live.pick_free_third_octet(189, {221}) == 32  # wraps
        with pytest.raises(live.HarnessError):
            live.pick_free_third_octet(0, set(range(32, 222)))
    finally:
        sys.modules.pop("b5_containment_acceptance", None)


def test_restore_missing_object_is_integrity_not_outage(db, project_session, object_fixture, tmp_path):
    run = _prepare_recovery_run(db, project_session, object_fixture, tmp_path)
    manifest = checkpoints.CheckpointManifest.model_validate(db.execute(
        text("SELECT manifest FROM checkpoints WHERE run_id=:run"), {"run": run.run_id}).scalar_one())
    del object_fixture.data[(objects.BUCKET, manifest.workspace[0].key)]
    with pytest.raises(checkpoints.CheckpointIntegrityError):
        checkpoints.restore(db, manifest, tmp_path / "r")


def _slow_fence_fakes(monkeypatch, *, dispatch_ok=True, worker_raises=False):
    import threading
    # Both stops must be in flight at once to pass the barrier; a serial fence would time out (no wall-clock bound).
    barrier = threading.Barrier(2, timeout=5)
    seen = SimpleNamespace(threads=set(), dbs=[], overlapped=[])

    def meet():
        try:
            barrier.wait()
            seen.overlapped.append(True)
        except threading.BrokenBarrierError:
            seen.overlapped.append(False)

    class Engine:
        def stop_worker(self, ref, grace_seconds):
            seen.threads.add(threading.get_ident())
            meet()
            if worker_raises:
                raise RuntimeError("docker unavailable")
            return True

    class Dispatch:
        def stop(self, db, ref, grace_seconds):
            seen.threads.add(threading.get_ident())
            seen.dbs.append(db)
            meet()
            return dispatch_ok

        def inactive(self, db, ref, operation_id):
            return True

    monkeypatch.setattr(supervisor, "_config", SimpleNamespace(engine=Engine(), dispatch=Dispatch()))
    return seen


def test_fence_generation_stops_executors_concurrently_without_session(db, project_session, monkeypatch):
    import threading
    run, _ = _reserved_stop_run(db, project_session, monkeypatch)
    seen = _slow_fence_fakes(monkeypatch)
    assert supervisor._fence_generation(db, run.run_id, 1, 0) is True
    assert seen.overlapped == [True, True]
    assert len(seen.threads) == 2 and threading.get_ident() not in seen.threads
    assert not any(isinstance(arg, Session) for arg in seen.dbs)
    states = db.execute(text("SELECT kind, state, proof->>'source' FROM runtime_executors "
                             "WHERE run_id=:run ORDER BY kind"), {"run": run.run_id}).all()
    assert states == [("dispatch", "inactive", "owned-engine-generation-fence"),
                      ("worker", "inactive", "owned-engine-generation-fence")]


def test_fence_generation_one_failed_concurrent_stop_is_incomplete(db, project_session, monkeypatch):
    run, _ = _reserved_stop_run(db, project_session, monkeypatch)
    _slow_fence_fakes(monkeypatch, dispatch_ok=False)
    before = _ops_and_budget(db, run.run_id)
    assert supervisor._fence_generation(db, run.run_id, 1, 0) is False
    assert dict(db.execute(text("SELECT kind, state FROM runtime_executors WHERE run_id=:run"),
                           {"run": run.run_id}).all()) == {"worker": "inactive", "dispatch": "unknown"}
    assert _ops_and_budget(db, run.run_id) == before
    stopped = supervisor.stop(db, run.run_id, 0)
    assert (stopped.state, stopped.waiting_reason) == ("waiting_input", "executor_quiescence_unproven")
    assert _ops_and_budget(db, run.run_id) == before


def test_fence_generation_stop_exception_is_never_proof(db, project_session, monkeypatch):
    run, _ = _reserved_stop_run(db, project_session, monkeypatch)
    _slow_fence_fakes(monkeypatch, worker_raises=True)
    before = _ops_and_budget(db, run.run_id)
    assert supervisor._fence_generation(db, run.run_id, 1, 0) is False
    assert dict(db.execute(text("SELECT kind, state FROM runtime_executors WHERE run_id=:run"),
                           {"run": run.run_id}).all()) == {"worker": "unknown", "dispatch": "inactive"}
    assert _ops_and_budget(db, run.run_id) == before
