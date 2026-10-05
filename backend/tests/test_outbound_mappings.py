from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from scientist.auth import DomainError
from scientist.contracts import PeerReleaseSpec, PlanSpec, Principal, canonical_peer_parameters_bytes
from scientist.db import create_project, create_session
from scientist.domain import approve_run, prepare_peer_receipt, record_peer_remote_identity, revise_plan, submit_run


OWNER = Principal(identity=uuid4(), kind="owner")
PROVIDER = uuid4()


def _release(snapshot: str, message_id: str, *, peer_id=None, purpose="Review this selected claim."):
    parameters = {"message": {"messageId": message_id, "role": "ROLE_USER", "parts": [{"text": "Please review this selected claim."}]}}
    return PeerReleaseSpec(
        release_id=uuid4(), peer_id=peer_id or uuid4(), endpoint_fingerprint="a" * 64,
        purpose=purpose, input_snapshot_digest=snapshot, data_refs=[],
        approved_parameters=parameters,
        parameters_sha256=sha256(canonical_peer_parameters_bytes(parameters)).hexdigest(),
        message_id=message_id, method="SendMessage", allow_get_task=True,
        request_bytes_limit=4096, timeout_ms=5000, reserved_tokens=100,
        reconciliation_limit=3,
    )


def test_prepared_peer_receipt_binds_ledger_before_remote_identity(db, project_session):
    project_id, session_id = project_session
    run = submit_run(db, OWNER, project_id, session_id, uuid4().hex, "Check this claim", [], PROVIDER, "fixture-model")
    snapshot = db.execute(text("SELECT digest FROM input_snapshots WHERE run_id=:run"), {"run": run.run_id}).scalar_one().strip()
    release = _release(snapshot, "peer-message-1", purpose="Have the configured peer check a selected citation claim.")
    plan = PlanSpec(input_snapshot_digest=snapshot, provider_id=PROVIDER, model="fixture-model", stages=["peer review"], allowed_ops=["peer"], data_recipients=[], packages=[], token_limit=1000, elapsed_limit_ms=30000, peer_releases=[release])
    revised = revise_plan(db, OWNER, run.run_id, run.revision, plan)
    approve_run(db, OWNER, run.run_id, revised.revision, revised.plan_digest)
    operation_id = "peer-op-1"
    db.execute(text("INSERT INTO operations (id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens) VALUES (:id, :run, :operation, 0, 'peer', :hash, 'reserved', :reserve)"), {"id": uuid4(), "run": run.run_id, "operation": operation_id, "hash": "f" * 64, "reserve": release.reserved_tokens})

    prepare_peer_receipt(db, run.run_id, operation_id, release.release_id)
    prepare_peer_receipt(db, run.run_id, operation_id, release.release_id)

    receipt = db.execute(text("SELECT release_id, peer_id, message_id, remote_task_id, remote_context_id FROM peer_outbound_receipts WHERE run_id=:run AND operation_id=:operation"), {"run": run.run_id, "operation": operation_id}).one()
    assert receipt.release_id == release.release_id
    assert receipt.peer_id == release.peer_id
    assert receipt.message_id == release.message_id
    assert receipt.remote_task_id is None and receipt.remote_context_id is None

    db.execute(text("INSERT INTO operations (id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens) VALUES (:id, :run, 'peer-op-duplicate-release', 0, 'peer', :hash, 'reserved', 100)"), {"id": uuid4(), "run": run.run_id, "hash": "c" * 64})
    with pytest.raises(DomainError) as error:
        prepare_peer_receipt(db, run.run_id, "peer-op-duplicate-release", release.release_id)
    assert error.value.code == "peer_release_already_prepared"
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text("""
            INSERT INTO peer_outbound_receipts
                (run_id, project_id, operation_id, release_id, peer_id, message_id,
                 endpoint_fingerprint, parameters_sha256)
            VALUES (:run, :project, 'peer-op-duplicate-release', :release, :peer, :message, :endpoint, :parameters)
        """), {"run": run.run_id, "project": project_id, "release": release.release_id, "peer": release.peer_id, "message": release.message_id, "endpoint": "a" * 64, "parameters": "b" * 64})

    db.execute(text("INSERT INTO operations (id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens) VALUES (:id, :run, 'peer-op-scope', 0, 'peer', :hash, 'reserved', 100)"), {"id": uuid4(), "run": run.run_id, "hash": "c" * 64})
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text("""
            INSERT INTO peer_outbound_receipts
                (run_id, project_id, operation_id, release_id, peer_id, message_id,
                 endpoint_fingerprint, parameters_sha256)
            VALUES (:run, :wrong_project, 'peer-op-scope', :release, :peer, 'scope-test', :endpoint, :parameters)
        """), {"run": run.run_id, "wrong_project": create_project(db, "wrong scope"), "release": release.release_id, "peer": release.peer_id, "endpoint": "a" * 64, "parameters": "b" * 64})


def test_peer_receipt_requires_operation_ledger_and_project_scope(db, project_session):
    project_id, session_id = project_session
    run = submit_run(db, OWNER, project_id, session_id, uuid4().hex, "Check this claim", [], PROVIDER, "fixture-model")
    snapshot = db.execute(text("SELECT digest FROM input_snapshots WHERE run_id=:run"), {"run": run.run_id}).scalar_one().strip()
    release = _release(snapshot, "stable-1")
    plan = PlanSpec(input_snapshot_digest=snapshot, provider_id=PROVIDER, model="fixture-model", stages=["peer review"], allowed_ops=["peer"], data_recipients=[], packages=[], token_limit=1000, elapsed_limit_ms=30000, peer_releases=[release])
    revised = revise_plan(db, OWNER, run.run_id, run.revision, plan)
    approve_run(db, OWNER, run.run_id, revised.revision, revised.plan_digest)

    with pytest.raises(DomainError) as error:
        prepare_peer_receipt(db, run.run_id, "missing-operation", release.release_id)
    assert error.value.code == "peer_operation_missing"

    other = create_project(db, "other project")
    other_session = create_session(db, other, "other session")
    other_run = submit_run(db, OWNER, other, other_session, uuid4().hex, "Other claim", [], PROVIDER, "fixture-model")
    db.execute(text("INSERT INTO operations (id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens) VALUES (:id, :run, 'peer-op-1', 0, 'peer', :hash, 'reserved', 100)"), {"id": uuid4(), "run": other_run.run_id, "hash": "f" * 64})
    with pytest.raises(DomainError) as error:
        prepare_peer_receipt(db, other_run.run_id, "peer-op-1", release.release_id)
    assert error.value.code == "peer_release_unapproved"


def test_peer_message_id_is_unique_across_runs(db, project_session):
    project_id, session_id = project_session
    first = submit_run(db, OWNER, project_id, session_id, uuid4().hex, "First claim", [], PROVIDER, "fixture-model")
    snapshot = db.execute(text("SELECT digest FROM input_snapshots WHERE run_id=:run"), {"run": first.run_id}).scalar_one().strip()
    release = _release(snapshot, "stable-across-runs")
    plan = PlanSpec(input_snapshot_digest=snapshot, provider_id=PROVIDER, model="fixture-model", stages=["peer review"], allowed_ops=["peer"], data_recipients=[], packages=[], token_limit=1000, elapsed_limit_ms=30000, peer_releases=[release])
    revised = revise_plan(db, OWNER, first.run_id, first.revision, plan)
    approve_run(db, OWNER, first.run_id, revised.revision, revised.plan_digest)
    db.execute(text("INSERT INTO operations (id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens) VALUES (:id, :run, 'peer-op-first', 0, 'peer', :hash, 'reserved', 100)"), {"id": uuid4(), "run": first.run_id, "hash": "f" * 64})
    prepare_peer_receipt(db, first.run_id, "peer-op-first", release.release_id)

    second = submit_run(db, OWNER, project_id, session_id, uuid4().hex, "Second claim", [], PROVIDER, "fixture-model")
    second_snapshot = db.execute(text("SELECT digest FROM input_snapshots WHERE run_id=:run"), {"run": second.run_id}).scalar_one().strip()
    second_release = release.model_copy(update={"release_id": uuid4(), "input_snapshot_digest": second_snapshot})
    second_plan = PlanSpec(input_snapshot_digest=second_snapshot, provider_id=PROVIDER, model="fixture-model", stages=["peer review"], allowed_ops=["peer"], data_recipients=[], packages=[], token_limit=1000, elapsed_limit_ms=30000, peer_releases=[second_release])
    second_revised = revise_plan(db, OWNER, second.run_id, second.revision, second_plan)
    approve_run(db, OWNER, second.run_id, second_revised.revision, second_revised.plan_digest)
    db.execute(text("INSERT INTO operations (id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens) VALUES (:id, :run, 'peer-op-second', 0, 'peer', :hash, 'reserved', 100)"), {"id": uuid4(), "run": second.run_id, "hash": "e" * 64})
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text("""
            INSERT INTO peer_outbound_receipts
                (run_id, project_id, operation_id, release_id, peer_id, message_id,
                 endpoint_fingerprint, parameters_sha256)
            VALUES (:run, :project, 'peer-op-second', :release, :peer, :message, :endpoint, :parameters)
        """), {"run": second.run_id, "project": project_id, "release": second_release.release_id, "peer": second_release.peer_id, "message": second_release.message_id, "endpoint": "a" * 64, "parameters": "b" * 64})

    with pytest.raises(DomainError) as error:
        prepare_peer_receipt(db, second.run_id, "peer-op-second", second_release.release_id)
    assert error.value.code == "peer_message_already_prepared"


def test_peer_remote_identity_can_be_recorded_once_but_not_replaced(db, project_session):
    # Reuse the first test's setup via direct construction to make the receipt FK real.
    project_id, session_id = project_session
    run = submit_run(db, OWNER, project_id, session_id, uuid4().hex, "Claim", [], PROVIDER, "fixture-model")
    snapshot = db.execute(text("SELECT digest FROM input_snapshots WHERE run_id=:run"), {"run": run.run_id}).scalar_one().strip()
    release = _release(snapshot, "stable-2")
    plan = PlanSpec(input_snapshot_digest=snapshot, provider_id=PROVIDER, model="fixture-model", stages=["peer review"], allowed_ops=["peer"], data_recipients=[], packages=[], token_limit=1000, elapsed_limit_ms=30000, peer_releases=[release])
    revised = revise_plan(db, OWNER, run.run_id, run.revision, plan)
    approve_run(db, OWNER, run.run_id, revised.revision, revised.plan_digest)
    db.execute(text("INSERT INTO operations (id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens) VALUES (:id, :run, 'peer-op-2', 0, 'peer', :hash, 'reserved', 100)"), {"id": uuid4(), "run": run.run_id, "hash": "f" * 64})
    prepare_peer_receipt(db, run.run_id, "peer-op-2", release.release_id)

    record_peer_remote_identity(db, run.run_id, "peer-op-2", "remote-task-1", "remote-context-1")
    record_peer_remote_identity(db, run.run_id, "peer-op-2", "remote-task-1", "remote-context-1")
    with pytest.raises(DomainError) as error:
        record_peer_remote_identity(db, run.run_id, "peer-op-2", "remote-task-2", "remote-context-2")
    assert error.value.code == "peer_identity_conflict"

    with pytest.raises(DBAPIError), db.begin_nested():
        db.execute(text("UPDATE peer_outbound_receipts SET peer_id=:peer WHERE run_id=:run AND operation_id='peer-op-2'"), {"peer": uuid4(), "run": run.run_id})
