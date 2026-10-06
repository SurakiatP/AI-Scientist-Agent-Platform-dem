from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from test_broker import broker_fixture
from test_scientific_receipts import receipt_boundary
from test_runtime_contracts import context_data


def _operation(db, run_id, operation_id, generation):
    db.execute(text("""
        INSERT INTO operations (id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens, result)
        VALUES (:id, :run, :operation, :generation, 'compute', :hash, 'reserved', 0, '{}'::jsonb)
    """), {
        "id": uuid4(), "run": run_id, "operation": operation_id, "generation": generation, "hash": "a" * 64,
    })


def _executor(db, run_id, generation, *, kind, operation_id=None, compute_operation_id=None):
    executor_id = uuid4()
    db.execute(text("""
        INSERT INTO runtime_executors
            (id, run_id, generation, kind, operation_id, compute_operation_id, process_incarnation, state)
        VALUES (:id, :run, :generation, :kind, :operation, :compute_operation, :incarnation, 'starting')
    """), {
        "id": executor_id, "run": run_id, "generation": generation, "kind": kind,
        "operation": operation_id, "compute_operation": compute_operation_id, "incarnation": uuid4(),
    })
    return executor_id


def test_migration_016_preserves_legacy_authorities_and_binds_compute_per_operation(broker_fixture):
    db, _, run_id, _, _ = broker_fixture
    legacy_worker = _executor(db, run_id, 1, kind="worker")
    legacy_dispatch = _executor(db, run_id, 1, kind="dispatch")
    _operation(db, run_id, "compute-a", 1)
    _operation(db, run_id, "compute-b", 1)
    first = _executor(db, run_id, 1, kind="compute", compute_operation_id="compute-a")
    second = _executor(db, run_id, 1, kind="compute", compute_operation_id="compute-b")
    db.commit()

    assert db.execute(text("SELECT COUNT(*) FROM runtime_executors WHERE id IN (:worker, :dispatch)"), {
        "worker": legacy_worker, "dispatch": legacy_dispatch,
    }).scalar_one() == 2
    assert db.execute(text("SELECT COUNT(*) FROM runtime_executors WHERE run_id = :run AND kind = 'compute' AND generation = 1"), {
        "run": run_id,
    }).scalar_one() == 2

    with pytest.raises(DBAPIError):
        _executor(db, run_id, 2, kind="compute", compute_operation_id="compute-a")
    db.rollback()

    with pytest.raises(DBAPIError):
        db.execute(text("UPDATE runtime_executors SET compute_operation_id = 'compute-b' WHERE id = :id"), {"id": first})
    db.rollback()

    db.execute(text("UPDATE runtime_executors SET container_id = :cid, engine_id = 'fixture-engine', state = 'active' WHERE id = :id"), {
        "cid": "b" * 64, "id": first,
    })
    db.commit()
    with pytest.raises(DBAPIError):
        db.execute(text("UPDATE runtime_executors SET engine_id = 'replacement-engine' WHERE id = :id"), {"id": first})
    db.rollback()


def test_migration_016_receipts_replay_by_output_index(receipt_boundary):
    db, token, controller, boundary, _ = receipt_boundary
    controller.boundary(db, token, boundary)
    receipt = db.execute(text("SELECT * FROM scientific_artifact_receipts WHERE run_id=:run"), {
        "run": boundary.context.run_id,
    }).one()

    for output_index in (1, 2, 3):
        db.execute(text("""
            INSERT INTO scientific_artifact_receipts
                (run_id, tool_call_id, output_index, project_id, checkpoint_id, artifact_id, receipt_sha256)
            VALUES (:run, :tool_call, :index, :project, :checkpoint, :artifact, :sha)
        """), {
            "run": receipt.run_id, "tool_call": receipt.tool_call_id, "index": output_index,
            "project": receipt.project_id, "checkpoint": receipt.checkpoint_id,
            "artifact": receipt.artifact_id, "sha": receipt.receipt_sha256,
        })
    db.commit()

    indexes = db.execute(text("SELECT array_agg(output_index ORDER BY output_index) FROM scientific_artifact_receipts WHERE run_id=:run AND tool_call_id=:tool_call"), {
        "run": receipt.run_id, "tool_call": receipt.tool_call_id,
    }).scalar_one()
    assert indexes == [0, 1, 2, 3]
    with pytest.raises(DBAPIError):
        with db.begin_nested():
            db.execute(text("""
                INSERT INTO scientific_artifact_receipts
                    (run_id, tool_call_id, output_index, project_id, checkpoint_id, artifact_id, receipt_sha256)
                VALUES (:run, :tool_call, 2, :project, :checkpoint, :artifact, :sha)
            """), {
                "run": receipt.run_id, "tool_call": receipt.tool_call_id, "project": receipt.project_id,
                "checkpoint": receipt.checkpoint_id, "artifact": receipt.artifact_id, "sha": receipt.receipt_sha256,
            })
