from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from scientist.auth import DomainError
from scientist.contracts import PeerReleaseSpec, PlanSpec, Principal, canonical_peer_parameters_bytes
from scientist.db import create_project, create_session, session as open_session
from scientist.domain import approve_run, revise_plan, submit_run
from scientist import peer_receipt_runtime as runtime

OWNER = Principal(identity=uuid4(), kind='owner')
PROVIDER = uuid4()


def _release(snapshot: str, message_id: str, *, peer_id=None, allow_get_task=True, reconciliation_limit=2):
    parameters = {'message': {'messageId': message_id, 'role': 'ROLE_USER', 'parts': [{'text': 'Review the selected result.'}]}}
    return PeerReleaseSpec(
        release_id=uuid4(), peer_id=peer_id or uuid4(), endpoint_fingerprint='a' * 64,
        purpose='Review the selected result.', input_snapshot_digest=snapshot, data_refs=[],
        approved_parameters=parameters,
        parameters_sha256=sha256(canonical_peer_parameters_bytes(parameters)).hexdigest(),
        message_id=message_id, method='SendMessage', allow_get_task=allow_get_task,
        request_bytes_limit=4096, timeout_ms=5000, reserved_tokens=100,
        reconciliation_limit=reconciliation_limit,
    )


def _approved_receipt(db, project_session, *, allow_get_task=True, reconciliation_limit=2, operation_id='peer-op-runtime', second_release=False):
    project_id, session_id = project_session
    run = submit_run(db, OWNER, project_id, session_id, uuid4().hex, 'Check one approved claim', [], PROVIDER, 'fixture-model')
    snapshot = db.execute(text('SELECT digest FROM input_snapshots WHERE run_id=:run'), {'run': run.run_id}).scalar_one().strip()
    release = _release(snapshot, f'msg-{uuid4().hex}', allow_get_task=allow_get_task, reconciliation_limit=reconciliation_limit)
    releases = [release]
    if second_release:
        releases.append(_release(snapshot, f'msg-{uuid4().hex}', peer_id=release.peer_id))
    plan = PlanSpec(
        input_snapshot_digest=snapshot, provider_id=PROVIDER, model='fixture-model',
        stages=['peer review'], allowed_ops=['peer'], data_recipients=[], packages=[],
        token_limit=1000, elapsed_limit_ms=30000, peer_releases=releases,
    )
    revised = revise_plan(db, OWNER, run.run_id, run.revision, plan)
    approve_run(db, OWNER, run.run_id, revised.revision, revised.plan_digest)
    db.execute(text("""INSERT INTO operations (id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens)
        VALUES (:id, :run, :operation, 0, 'peer', :hash, 'reserved', 100)"""),
        {'id': uuid4(), 'run': run.run_id, 'operation': operation_id, 'hash': sha256(operation_id.encode()).hexdigest()})
    db.commit()
    return run.run_id, release, operation_id


def _count(db, run_id, operation_id):
    return db.execute(text('SELECT reconciliation_attempts FROM peer_outbound_receipts WHERE run_id=:run AND operation_id=:operation'),
                      {'run': run_id, 'operation': operation_id}).scalar_one()


def test_prepare_submission_commits_once_and_returns_false_for_exact_replay(db, project_session):
    run_id, release, operation_id = _approved_receipt(db, project_session)
    assert runtime.prepare_submission(run_id, operation_id, release.release_id) is True
    assert runtime.prepare_submission(run_id, operation_id, release.release_id) is False
    with open_session() as check:
        assert check.execute(text('SELECT state FROM peer_outbound_receipts WHERE run_id=:run AND operation_id=:operation'),
                             {'run': run_id, 'operation': operation_id}).scalar_one() == 'prepared'


def test_concurrent_prepare_submission_has_one_new_receipt_result(db, project_session):
    run_id, release, operation_id = _approved_receipt(db, project_session)
    barrier = Barrier(2)

    def prepare():
        barrier.wait(timeout=5)
        return runtime.prepare_submission(run_id, operation_id, release.release_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: prepare(), range(2)))
    assert sorted(results) == [False, True]


def test_prepare_rejects_release_change_for_existing_operation(db, project_session):
    run_id, release, operation_id = _approved_receipt(db, project_session, second_release=True)
    other_release = db.execute(text("SELECT plan FROM plan_revisions WHERE run_id=:run ORDER BY revision DESC LIMIT 1"),
                               {'run': run_id}).scalar_one()['peer_releases'][1]['release_id']
    runtime.prepare_submission(run_id, operation_id, release.release_id)
    with pytest.raises(DomainError) as error:
        runtime.prepare_submission(run_id, operation_id, other_release)
    assert error.value.code == 'peer_operation_conflict'


def test_prepare_commit_failure_never_returns_true(db, project_session, monkeypatch):
    run_id, release, operation_id = _approved_receipt(db, project_session)
    real_session = runtime.db.session

    @contextmanager
    def failing_session():
        with real_session() as session:
            class CommitFails:
                def __getattr__(self, name):
                    return getattr(session, name)

                def commit(self):
                    session.rollback()
                    raise RuntimeError('fixture commit failure')

            yield CommitFails()

    monkeypatch.setattr(runtime.db, 'session', failing_session)
    with pytest.raises(RuntimeError, match='fixture commit failure'):
        runtime.prepare_submission(run_id, operation_id, release.release_id)
    monkeypatch.setattr(runtime.db, 'session', real_session)
    with open_session() as check:
        assert check.execute(text('SELECT 1 FROM peer_outbound_receipts WHERE run_id=:run AND operation_id=:operation'),
                             {'run': run_id, 'operation': operation_id}).scalar_one_or_none() is None


def test_remote_identity_commit_survives_a_later_transport_failure(db, project_session):
    run_id, release, operation_id = _approved_receipt(db, project_session)
    runtime.prepare_submission(run_id, operation_id, release.release_id)
    with pytest.raises(RuntimeError, match='simulated PUT failure'):
        runtime.record_remote_identity(run_id, operation_id, 'remote-task-1', 'remote-context-1')
        raise RuntimeError('simulated PUT failure')
    with open_session() as check:
        receipt = check.execute(text('SELECT remote_task_id, remote_context_id, state FROM peer_outbound_receipts WHERE run_id=:run AND operation_id=:operation'),
                                {'run': run_id, 'operation': operation_id}).one()
        assert (receipt.remote_task_id, receipt.remote_context_id, receipt.state) == ('remote-task-1', 'remote-context-1', 'accepted')


@pytest.mark.parametrize('task_id,context_id', [('', 'context-1'), (' task-1', None), ('task-1 ', None), (None, '  '), (None, None)])
def test_remote_identity_rejects_blank_or_padded_values(db, project_session, task_id, context_id):
    run_id, release, operation_id = _approved_receipt(db, project_session)
    runtime.prepare_submission(run_id, operation_id, release.release_id)
    with pytest.raises(DomainError):
        runtime.record_remote_identity(run_id, operation_id, task_id, context_id)
    with open_session() as check:
        assert check.execute(text('SELECT remote_task_id FROM peer_outbound_receipts WHERE run_id=:run AND operation_id=:operation'),
                             {'run': run_id, 'operation': operation_id}).scalar_one() is None


def test_mark_unknown_commits_state_without_persisting_reason(db, project_session):
    run_id, release, operation_id = _approved_receipt(db, project_session)
    runtime.prepare_submission(run_id, operation_id, release.release_id)
    runtime.mark_unknown(run_id, operation_id, 'private diagnostic marker')
    with open_session() as check:
        receipt = check.execute(text('SELECT state FROM peer_outbound_receipts WHERE run_id=:run AND operation_id=:operation'),
                                {'run': run_id, 'operation': operation_id}).scalar_one()
        assert receipt == 'unknown'
        assert check.execute(text("SELECT count(*) FROM events WHERE run_id=:run AND payload::text LIKE '%private diagnostic marker%'"),
                             {'run': run_id}).scalar_one() == 0


def test_reconciliation_attempt_is_uncommitted_until_caller_commits_and_survives_sessions(db, project_session):
    run_id, release, operation_id = _approved_receipt(db, project_session)
    runtime.prepare_submission(run_id, operation_id, release.release_id)
    runtime.record_remote_identity(run_id, operation_id, 'remote-task-known', None)
    with open_session() as first:
        assert runtime.consume_reconciliation_attempt(first, run_id, operation_id) == 1
        assert _count(first, run_id, operation_id) == 1
        first.rollback()
    with open_session() as check:
        assert _count(check, run_id, operation_id) == 0
    with open_session() as second:
        assert runtime.consume_reconciliation_attempt(second, run_id, operation_id) == 1
        second.commit()
    with open_session() as check:
        assert _count(check, run_id, operation_id) == 1


def test_get_task_requires_current_approval_known_task_and_explicit_permission(db, project_session):
    run_id, release, operation_id = _approved_receipt(db, project_session, allow_get_task=False)
    runtime.prepare_submission(run_id, operation_id, release.release_id)
    runtime.record_remote_identity(run_id, operation_id, 'remote-task-known', None)
    with open_session() as check, pytest.raises(DomainError):
        runtime.consume_reconciliation_attempt(check, run_id, operation_id)
    with open_session() as check:
        assert _count(check, run_id, operation_id) == 0

    run_without_identity, release_without_identity, operation_without_identity = _approved_receipt(
        db, project_session, allow_get_task=True, operation_id='peer-op-no-identity',
    )
    runtime.prepare_submission(run_without_identity, operation_without_identity, release_without_identity.release_id)
    with open_session() as check, pytest.raises(DomainError):
        runtime.consume_reconciliation_attempt(check, run_without_identity, operation_without_identity)


def test_concurrent_final_reconciliation_attempt_is_consumed_only_once(db, project_session):
    run_id, release, operation_id = _approved_receipt(db, project_session, reconciliation_limit=1)
    runtime.prepare_submission(run_id, operation_id, release.release_id)
    runtime.record_remote_identity(run_id, operation_id, 'remote-task-known', None)
    barrier = Barrier(2)

    def consume():
        with open_session() as session:
            barrier.wait(timeout=5)
            try:
                attempt = runtime.consume_reconciliation_attempt(session, run_id, operation_id)
                session.commit()
                return attempt
            except DomainError:
                session.rollback()
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))
    assert results.count(1) == 1
    assert results.count(None) == 1
    with open_session() as check:
        assert _count(check, run_id, operation_id) == 1


def test_canceled_receipt_is_unchanged_and_database_blocks_counter_or_identity_rewrites(db, project_session):
    run_id, release, operation_id = _approved_receipt(db, project_session)
    runtime.prepare_submission(run_id, operation_id, release.release_id)
    runtime.record_remote_identity(run_id, operation_id, 'remote-task-known', None)
    db.execute(text("UPDATE runs SET state='canceled', cancel_requested=true WHERE id=:run"), {'run': run_id})
    db.commit()
    with open_session() as check, pytest.raises(DomainError):
        runtime.consume_reconciliation_attempt(check, run_id, operation_id)
    with open_session() as check:
        assert _count(check, run_id, operation_id) == 0
        for update in (
            "UPDATE peer_outbound_receipts SET reconciliation_attempts=2 WHERE run_id=:run AND operation_id=:operation",
            "UPDATE peer_outbound_receipts SET remote_task_id='replacement-task' WHERE run_id=:run AND operation_id=:operation",
        ):
            with pytest.raises(DBAPIError), check.begin_nested():
                check.execute(text(update), {'run': run_id, 'operation': operation_id})
