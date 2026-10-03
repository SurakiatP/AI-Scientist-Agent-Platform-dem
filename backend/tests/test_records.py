from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

import json
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from scientist.contracts import ArtifactView, OperationRequest, RunEvent
from scientist.db import create_project, create_session, migrate, reserve_tokens, session, settle_tokens
from conftest import validate_test_database_url


def test_contract_rejects_negative_reservation():
    with pytest.raises(ValidationError):
        OperationRequest(
            run_id=uuid4(), generation=1, operation_id="op1", kind="llm",
            payload={}, reserve_tokens=-1,
        )

def test_create_helpers_generate_and_return_ids(db):
    project_id = create_project(db, "helper project")
    session_id = create_session(db, project_id, "helper session")
    assert isinstance(project_id, type(uuid4()))
    assert isinstance(session_id, type(uuid4()))

def test_test_database_guard_rejects_unsafe_name_and_remote_host():
    with pytest.raises(RuntimeError, match="dedicated test database"):
        validate_test_database_url("postgresql+psycopg:///?dbname=production&host=/tmp/pg")
    with pytest.raises(RuntimeError, match="local PostgreSQL"):
        validate_test_database_url("postgresql+psycopg://localhost.example/?dbname=scientist_b1")
    for unsafe in (
        "postgresql+psycopg://localhost/scientist_b1?dbname=production",
        "postgresql+psycopg://localhost/scientist_b1?host=remote.example",
        "postgresql+psycopg:///?dbname=scientist_b1&host=/tmp/pg&service=production",
        "postgresql+psycopg:///?dbname=scientist_b1&host=/tmp/pg&passfile=/tmp/pgpass",
        "postgresql+psycopg:///?dbname=scientist_b1&host=/tmp/pg&options=-csearch_path=public",
    ):
        with pytest.raises(RuntimeError):
            validate_test_database_url(unsafe)


def test_public_contracts_reject_private_object_keys_and_event_payload_mismatch():
    with pytest.raises(ValidationError):
        ArtifactView(artifact_id=uuid4(), project_id=uuid4(), run_id=uuid4(), title="x",
                     kind="file", sha256="a" * 64, size=0, content_type="text/plain",
                     partial=False, object_key="private/key")
    with pytest.raises(ValidationError):
        RunEvent(schema_version=1, run_id=uuid4(), sequence=1, revision=1,
                 occurred_at=datetime.now(timezone.utc), kind="run.state",
                 payload={"plan_digest": "a" * 64})


def test_generated_event_schema_accepts_valid_event_and_rejects_invalid_event():
    schema_path = Path(__file__).resolve().parents[2] / "contracts" / "run-event.schema.json"
    schema = json.loads(schema_path.read_text())
    validator = Draft202012Validator(schema)
    valid = {
        "schema_version": 1,
        "run_id": str(uuid4()),
        "sequence": 1,
        "revision": 1,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "kind": "run.state",
        "payload": {"state": "running"},
    }
    assert list(validator.iter_errors(valid)) == []
    invalid = {**valid, "payload": {"plan_digest": "a" * 64}}
    assert list(validator.iter_errors(invalid))


def test_session_cannot_reference_missing_project():
    migrate()
    with session() as db, pytest.raises(IntegrityError):
        create_session(db, uuid4(), "invalid")


def test_records_persist_and_project_ownership_is_enforced():
    migrate()
    project_id, other_id = uuid4(), uuid4()
    with session() as db:
        project_id = create_project(db, "first")
        other_id = create_project(db, "second")
        session_id = create_session(db, project_id, "session")
        db.commit()
    with session() as db:
        assert db.execute(text("SELECT name FROM projects WHERE id = :id"), {"id": project_id}).scalar_one() == "first"
        assert db.execute(text("SELECT project_id FROM sessions WHERE id = :id"), {"id": session_id}).scalar_one() == project_id
    with session() as db:
        with pytest.raises(IntegrityError):
            db.execute(text("INSERT INTO sessions (id, project_id, title, parent_session_id) VALUES (:id, :project, 'session', :parent)"),
                       {"id": uuid4(), "project": other_id, "parent": session_id})


def test_migration_is_idempotent_and_detects_applied_file_changes(tmp_path):
    migrate()
    migrate()
    changed = tmp_path / "001_initial.sql"
    changed.write_text("-- changed migration")
    with pytest.raises(RuntimeError, match="checksum"):
        migrate(migration_path=changed)


def test_run_cannot_attach_a_session_from_another_project(db, project_session):
    project_id, session_id = project_session
    other_project_id = create_project(db, "second")
    with pytest.raises(IntegrityError):
        db.execute(text("""
            INSERT INTO runs (id, project_id, session_id, caller_identity, submission_key, submission_hash)
            VALUES (:id, :project, :session, :caller, 'key', :hash)
        """), {"id": uuid4(), "project": other_project_id, "session": session_id,
               "caller": uuid4(), "hash": "a" * 64})


def test_input_snapshot_is_append_only(db):
    project_id = create_project(db, "immutable")
    session_id = create_session(db, project_id, "immutable")
    run_id, snapshot_id = uuid4(), uuid4()
    db.execute(text("""
        INSERT INTO runs (id, project_id, session_id, caller_identity, submission_key, submission_hash)
        VALUES (:id, :project, :session, :caller, 'immutable-key', :hash)
    """), {"id": run_id, "project": project_id, "session": session_id,
           "caller": uuid4(), "hash": "c" * 64})
    db.execute(text("""
        INSERT INTO input_snapshots (id, project_id, run_id, digest, manifest)
        VALUES (:id, :project, :run, :digest, '{}'::jsonb)
    """), {"id": snapshot_id, "project": project_id, "run": run_id, "digest": "d" * 64})
    with pytest.raises(DBAPIError, match="immutable"):
        db.execute(text("UPDATE input_snapshots SET manifest = CAST(:manifest AS jsonb) WHERE id = :id"),
                   {"id": snapshot_id, "manifest": '{"changed":true}'})


def test_concurrent_reservations_respect_limit_and_actual_overage_is_recorded(db):
    project_id = create_project(db, "budget")
    session_id = create_session(db, project_id, "budget")
    run_id = uuid4()
    db.execute(text("""
        INSERT INTO runs (id, project_id, session_id, caller_identity, submission_key, submission_hash, token_limit)
        VALUES (:id, :project, :session, :caller, 'budget-key', :hash, 10)
    """), {"id": run_id, "project": project_id, "session": session_id,
           "caller": uuid4(), "hash": "b" * 64})
    db.commit()

    def reserve():
        try:
            with session() as worker_db:
                reserve_tokens(worker_db, run_id, 7)
                worker_db.commit()
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: reserve(), range(2)))
    assert sorted(outcomes) == [False, True]
    with session() as check:
        assert check.execute(text("SELECT reserved_tokens FROM runs WHERE id = :id"), {"id": run_id}).scalar_one() == 7
        settle_tokens(check, run_id, 7, 20)
        check.commit()
    with session() as check:
        assert check.execute(text("SELECT usage_tokens, reserved_tokens FROM runs WHERE id = :id"), {"id": run_id}).one() == (20, 0)
        with pytest.raises(ValueError, match="budget exhausted"):
            reserve_tokens(check, run_id, 1)


def test_followup_migration_stores_time_limit_and_message_order(db, project_session):
    columns = db.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'runs' AND column_name = 'elapsed_limit_ms'")).all()
    assert columns == [("elapsed_limit_ms",)]
    project_id, session_id = project_session
    sequences = []
    for role in ("user", "assistant"):
        sequences.append(db.execute(text("INSERT INTO messages (id, project_id, session_id, role, content) VALUES (:id, :project, :session, :role, 'test message') RETURNING sequence"), {"id": uuid4(), "project": project_id, "session": session_id, "role": role}).scalar_one())
    assert sequences[0] < sequences[1]
    assert db.execute(text("SELECT role FROM messages WHERE session_id = :session ORDER BY sequence"), {"session": session_id}).scalars().all() == ["user", "assistant"]
