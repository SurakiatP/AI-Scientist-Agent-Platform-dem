from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import text

from scientist import broker
from scientist.db import session
from test_broker import broker_fixture, request  # noqa: F401


def _unknown_then_claimed(db, owner, run_id, transport, decision):
    transport.lose_response = True
    broker.execute(db, broker.issue_capability(db, run_id, 1, 300), request(run_id))
    transport.lose_response = False
    db.execute(text("UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE id = :run"), {"run": run_id})
    db.commit()
    broker.reconcile(db, run_id, "operation-1")
    if decision:
        broker.resolve_unknown(db, owner, run_id, "operation-1", decision, None)
    # Force a claimed running generation even without a decision: the broker itself must refuse to dispatch.
    db.execute(text("UPDATE runs SET generation = 2, state = 'running', lease_expires_at = now() + interval '1 hour' "
                    "WHERE id = :run"), {"run": run_id})
    db.commit()


def _replay(db, run_id):
    return broker.execute(db, broker.issue_capability(db, run_id, 2, 300),
                          request(run_id).model_copy(update={"generation": 2}))


def _ledger(db, run_id):
    run = db.execute(text("SELECT usage_tokens, reserved_tokens FROM runs WHERE id = :run"), {"run": run_id}).one()
    ops = db.execute(text("SELECT operation_id, state FROM operations WHERE run_id = :run"), {"run": run_id}).all()
    return (run.usage_tokens, run.reserved_tokens), len(ops)


def test_owner_retry_is_dispatched_once_on_continuation_replay(broker_fixture):
    db, owner, run_id, transport, _ = broker_fixture
    _unknown_then_claimed(db, owner, run_id, transport, "retry")
    assert transport.calls == 1
    result = _replay(db, run_id)
    assert result.state == "committed" and result.result is not None
    assert transport.calls == 2
    # original reservation stays held; retry reservation released into actual usage
    assert _ledger(db, run_id) == ((3, 5), 2)
    assert _replay(db, run_id) == result
    assert transport.calls == 2
    assert _ledger(db, run_id) == ((3, 5), 2)


def test_no_decision_stays_unknown_without_attempt(broker_fixture):
    db, owner, run_id, transport, _ = broker_fixture
    _unknown_then_claimed(db, owner, run_id, transport, None)
    assert _replay(db, run_id).state == "unknown"
    assert transport.calls == 1
    assert _ledger(db, run_id)[1] == 1


def test_stop_decision_never_dispatches(broker_fixture):
    db, owner, run_id, transport, _ = broker_fixture
    _unknown_then_claimed(db, owner, run_id, transport, "stop")
    assert _replay(db, run_id).state == "unknown"
    assert transport.calls == 1
    assert _ledger(db, run_id)[1] == 1


def test_concurrent_replays_make_one_attempt(broker_fixture):
    db, owner, run_id, transport, _ = broker_fixture
    _unknown_then_claimed(db, owner, run_id, transport, "retry")
    entered, release = Event(), Event()
    original_call = type(transport).__call__

    def slow(self, req, target):
        entered.set()
        assert release.wait(10)
        return original_call(self, req, target)

    type(transport).__call__ = slow

    def worker():
        with session() as other:
            return _replay(other, run_id)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(worker)
            assert entered.wait(5)
            second = pool.submit(worker)
            assert second.result(timeout=10).state in {"unknown", "committed"}
            release.set()
            assert first.result(timeout=10).state == "committed"
    finally:
        release.set()
        type(transport).__call__ = original_call
    assert transport.calls == 2
    assert _ledger(db, run_id) == ((3, 5), 2)
    assert _replay(db, run_id).state == "committed"


def _claim(db, run_id, gen):
    db.execute(text("UPDATE runs SET generation = :g, state = 'running', lease_expires_at = now() + interval '1 hour' "
                    "WHERE id = :run"), {"run": run_id, "g": gen})
    db.commit()


def test_worker_fetches_result_of_original_after_retry(broker_fixture):
    from scientist.private_worker_api import RuntimePins, WorkerController
    db, owner, run_id, transport, stored = broker_fixture
    _unknown_then_claimed(db, owner, run_id, transport, "retry")
    assert _replay(db, run_id).state == "committed"
    pins = RuntimePins(image_digest="sha256:" + "b" * 64, skills_digest="c" * 64, environment_digest="d" * 64)
    controller = WorkerController(pins=pins, provider_destinations={}, result_reader=lambda ref: stored[ref.key])
    assert controller.result(db, broker.issue_capability(db, run_id, 2, 300), "operation-1") == b'{"ok":true}'


def test_owner_can_retry_a_retry_that_ended_unknown(broker_fixture):
    db, owner, run_id, transport, _ = broker_fixture
    _unknown_then_claimed(db, owner, run_id, transport, "retry")
    transport.lose_response = True
    assert _replay(db, run_id).state == "unknown" and transport.calls == 2
    transport.lose_response = False
    retry_id = db.execute(text("SELECT result->>'retry_identity' FROM operations WHERE run_id=:r AND operation_id='operation-1'"),
                          {"r": run_id}).scalar_one()
    db.execute(text("UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE id = :run"), {"run": run_id})
    db.commit()
    broker.resolve_unknown(db, owner, run_id, retry_id, "retry", None)
    _claim(db, run_id, 3)
    out = broker.execute(db, broker.issue_capability(db, run_id, 3, 300), request(run_id).model_copy(update={"generation": 3}))
    assert out.state == "committed" and transport.calls == 3
    assert broker.execute(db, broker.issue_capability(db, run_id, 3, 300),
                          request(run_id).model_copy(update={"generation": 3})) == out
    assert transport.calls == 3


def test_staged_retry_is_finalized_on_replay(broker_fixture):
    db, owner, run_id, transport, _ = broker_fixture
    _unknown_then_claimed(db, owner, run_id, transport, "retry")
    original_finalize = broker._finalize_staged_success
    broker._finalize_staged_success = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash before final commit"))
    try:
        with pytest.raises(RuntimeError):
            _replay(db, run_id)
    finally:
        broker._finalize_staged_success = original_finalize
    db.rollback()
    assert _replay(db, run_id).state == "committed"
    assert transport.calls == 2


def test_redeciding_original_after_its_retry_committed_is_rejected(broker_fixture):
    from scientist.auth import DomainError
    db, owner, run_id, transport, _ = broker_fixture
    _unknown_then_claimed(db, owner, run_id, transport, "retry")
    assert _replay(db, run_id).state == "committed"
    db.execute(text("UPDATE runs SET state='waiting_input', waiting_reason='unknown_outcome', lease_expires_at = NULL WHERE id=:r"),
               {"r": run_id})
    db.commit()
    with pytest.raises(DomainError):
        broker.resolve_unknown(db, owner, run_id, "operation-1", "retry", None)
    assert transport.calls == 2


def test_second_replay_waits_on_run_lock_before_retry_row_exists(broker_fixture, monkeypatch):
    db, owner, run_id, transport, _ = broker_fixture
    _unknown_then_claimed(db, owner, run_id, transport, "retry")
    entered, release = Event(), Event()
    real = broker.limits.budget_exhausted

    def gated(*a, **k):
        entered.set()
        assert release.wait(10)
        return real(*a, **k)

    monkeypatch.setattr(broker.limits, "budget_exhausted", gated)

    def worker():
        with session() as other:
            return _replay(other, run_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker)
        assert entered.wait(5)
        second = pool.submit(worker)
        release.set()
        assert {first.result(10).state, second.result(10).state} <= {"committed", "unknown"}  # in-flight duplicate reports unknown
    assert transport.calls == 2
