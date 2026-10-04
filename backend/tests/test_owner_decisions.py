"""Owner decisions: durable decision_id binding, queued-only retry (D1), selection (D2), idempotency (D3)."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text

from scientist import broker, domain, supervisor
from scientist.auth import DomainError
from scientist.contracts import DecisionSubmit, ObjectRef
from scientist.db import session
from scientist.runtime_contracts import RUNTIME_COMMIT
from test_broker import broker_fixture, request  # noqa: F401


class FakeDispatch:
    inactive_result = True

    def inactive(self, db, executor, operation_id):
        return self.inactive_result

    def stop(self, db, executor, grace_seconds):
        return True


@pytest.fixture
def dispatch():
    fake = FakeDispatch()
    supervisor.configure(
        image="sha256:" + "b" * 64, image_digest="sha256:" + "b" * 64, broker_url="http://172.29.42.1:8080",
        broker_ip="172.29.42.1", broker_port=8080, runtime_commit=RUNTIME_COMMIT, skills_digest="c" * 64,
        environment_digest="d" * 64, bootstrap_factory=lambda *a: None, capability_factory=lambda *a: "",
        dispatch=fake, engine=object())
    yield fake
    broker.configure_dispatch_inactivity(None)


def bind_executor(db, run_id, operation_id="operation-1", generation=1):
    executor_id = db.execute(text("SELECT id FROM runtime_executors WHERE run_id=:run AND generation=:gen AND kind='dispatch'"),
                             {"run": run_id, "gen": generation}).scalar_one_or_none()
    if executor_id is None:
        executor_id = uuid4()
        db.execute(text("""INSERT INTO runtime_executors (id, run_id, generation, kind, operation_id, process_incarnation,
            container_id, engine_id, state, proof) VALUES (:id, :run, :gen, 'dispatch', NULL, :inc, :c, 'engine-test', 'inactive',
            CAST(:proof AS jsonb))"""),
                   {"id": executor_id, "run": run_id, "gen": generation, "inc": uuid4(), "c": "a" * 64,
                    "proof": '{"source": "owned-engine-exact-container", "engine_id": "engine-test", "container_id": "' + "a" * 64 + '"}'})
    db.execute(text("INSERT INTO operation_executors (run_id, operation_id, generation, executor_id) VALUES (:run, :op, :gen, :e)"),
               {"run": run_id, "op": operation_id, "gen": generation, "e": executor_id})
    db.commit()


def make_unknown(db, transport, run_id, *, bind=True, operation_id="operation-1"):
    transport.lose_response = True
    broker.execute(db, broker.issue_capability(db, run_id, 1, 300), request(run_id, operation_id))
    transport.lose_response = False
    db.execute(text("UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE id = :run"), {"run": run_id})
    db.commit()
    broker._inactive_dispatches.clear()  # the REST host is a fresh process: no in-memory dispatch history
    if bind:
        bind_executor(db, run_id, operation_id)
    return db.execute(text("SELECT decision_id FROM owner_decisions WHERE run_id=:r AND operation_id=:o AND state='pending'"),
                      {"r": run_id, "o": operation_id}).scalar_one()


def body(db, run_id, decision_id, *, key="key-1", choice="retry", **extra):
    revision = db.execute(text("SELECT revision FROM runs WHERE id=:r"), {"r": run_id}).scalar_one()
    return DecisionSubmit(decision_id=decision_id, expected_revision=revision, idempotency_key=key, choice=choice, **extra)


def retry_ops(db, run_id):
    return db.execute(text("SELECT count(*) FROM operations WHERE run_id=:r AND operation_id LIKE 'retry-%'"), {"r": run_id}).scalar_one()


def test_decision_id_is_bound_to_the_operation_when_issued(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id, bind=False)
    event_id = db.execute(text("SELECT payload->>'decision_id' FROM events WHERE run_id=:r AND kind='decision.required'"),
                          {"r": run_id}).scalar_one()
    assert event_id == str(decision_id)
    # reconcile does not mint a second decision for the same operation
    db.execute(text("UPDATE runs SET state='running' WHERE id=:r"), {"r": run_id})
    db.commit()
    broker.configure_dispatch_inactivity(lambda *a: True)
    try:
        broker.reconcile(db, run_id, "operation-1")
    finally:
        broker.configure_dispatch_inactivity(None)
    assert db.execute(text("SELECT count(*) FROM owner_decisions WHERE run_id=:r"), {"r": run_id}).scalar_one() == 1


def test_dispatch_inactivity_proof_survives_broker_configure(broker_fixture, dispatch):
    db, _, run_id, transport, _ = broker_fixture
    make_unknown(db, transport, run_id)
    broker.configure(transport=transport, capability_key=b"b" * 32)  # a later configure must not drop the proof
    broker.reconcile(db, run_id, "operation-1")
    dispatch.inactive_result = False
    with pytest.raises(DomainError) as err:
        broker.reconcile(db, run_id, "operation-1")
    assert err.value.status == 409


def test_retry_queues_and_never_dispatches_from_the_host(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id)
    db.execute(text("UPDATE runs SET lease_expires_at = now() + interval '1 hour' WHERE id=:r"), {"r": run_id})
    db.commit()
    calls = transport.calls
    view = domain.submit_decision(db, owner, run_id, body(db, run_id, decision_id))
    assert view.state == "queued" and transport.calls == calls
    original = db.execute(text("SELECT state, result FROM operations WHERE run_id=:r AND operation_id='operation-1'"), {"r": run_id}).one()
    assert original.state == "unknown" and original.result["retry_identity"].startswith("retry-")
    assert db.execute(text("SELECT generation FROM runs WHERE id=:r"), {"r": run_id}).scalar_one() == 1
    assert db.execute(text("SELECT reserved_tokens FROM runs WHERE id=:r"), {"r": run_id}).scalar_one() == 5
    assert retry_ops(db, run_id) == 0
    assert original.result["retry_request"]["generation"] == 1 + 1


def test_retry_refused_without_proof_or_binding(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id, bind=False)
    with pytest.raises(DomainError) as err:
        domain.submit_decision(db, owner, run_id, body(db, run_id, decision_id))
    assert err.value.status == 409
    db.rollback()
    bind_executor(db, run_id)
    dispatch.inactive_result = False
    with pytest.raises(DomainError) as err:
        domain.submit_decision(db, owner, run_id, body(db, run_id, decision_id))
    assert err.value.status == 409
    db.rollback()
    assert db.execute(text("SELECT state FROM owner_decisions WHERE decision_id=:d"), {"d": decision_id}).scalar_one() == "pending"


def test_wrong_decision_and_stale_revision_are_refused(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id)
    with pytest.raises(DomainError) as err:
        domain.submit_decision(db, owner, run_id, body(db, run_id, uuid4()))
    assert err.value.status == 404
    stale = body(db, run_id, decision_id).model_copy(update={"expected_revision": 99})
    with pytest.raises(DomainError) as err:
        domain.submit_decision(db, owner, run_id, stale)
    assert (err.value.status, err.value.code) == (409, "revision_conflict")
    with pytest.raises(DomainError) as err:
        domain.submit_decision(db, Principal_external(), run_id, body(db, run_id, decision_id))
    assert err.value.status == 403


def Principal_external():
    from scientist.contracts import Principal
    return Principal(identity=uuid4(), kind="external")


def test_two_pending_candidates_are_ambiguous(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    first = make_unknown(db, transport, run_id)
    db.execute(text("UPDATE runs SET state='running', waiting_reason=NULL, lease_expires_at = now() + interval '1 hour' WHERE id=:r"), {"r": run_id})
    db.commit()
    make_unknown(db, transport, run_id, bind=False, operation_id="operation-2")
    with pytest.raises(DomainError) as err:
        domain.submit_decision(db, owner, run_id, body(db, run_id, first))
    assert (err.value.status, err.value.code) == (409, "decision_ambiguous")


def test_double_click_creates_one_retry(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id)
    first = domain.submit_decision(db, owner, run_id, body(db, run_id, decision_id))
    # revision is unchanged, so an identical resubmit is the same payload
    second = domain.submit_decision(db, owner, run_id, body(db, run_id, decision_id))
    assert first == second
    assert db.execute(text("SELECT result->>'retry_identity' FROM operations WHERE run_id=:r AND operation_id='operation-1'"),
                      {"r": run_id}).scalar_one().startswith("retry-")
    assert db.execute(text("SELECT count(*) FROM owner_decisions WHERE run_id=:r AND state='resolved'"), {"r": run_id}).scalar_one() == 1
    assert transport.calls == 1


def test_same_key_with_different_payload_conflicts(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id)
    domain.submit_decision(db, owner, run_id, body(db, run_id, decision_id))
    with pytest.raises(DomainError) as err:
        domain.submit_decision(db, owner, run_id, body(db, run_id, decision_id, choice="stop"))
    assert (err.value.status, err.value.code) == (409, "idempotency_conflict")


def test_response_loss_replay_returns_original_result(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id)
    submit = body(db, run_id, decision_id)
    domain.submit_decision(db, owner, run_id, submit)  # committed; response "lost"
    db.rollback()
    with session() as fresh:
        again = domain.submit_decision(fresh, owner, run_id, submit)
    assert again.state == "queued" and again.run_id == run_id
    assert db.execute(text("SELECT count(*) FROM owner_decisions WHERE run_id=:r AND idempotency_key IS NOT NULL"), {"r": run_id}).scalar_one() == 1


def test_concurrent_submits_create_one_retry(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id)
    submit = body(db, run_id, decision_id)
    barrier = Barrier(2)

    def go():
        with session() as other:
            barrier.wait(5)
            try:
                return domain.submit_decision(other, owner, run_id, submit).state
            except DomainError as exc:
                other.rollback()
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=20) for f in [pool.submit(go), pool.submit(go)]]
    assert results == ["queued", "queued"]
    assert db.execute(text("SELECT count(*) FROM owner_decisions WHERE run_id=:r AND state='resolved'"), {"r": run_id}).scalar_one() == 1
    assert db.execute(text("SELECT result->>'retry_request' FROM operations WHERE run_id=:r AND operation_id='operation-1'"), {"r": run_id}).scalar_one()
    assert transport.calls == 1


def test_stop_choice_marks_run_stopping(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id)
    view = domain.submit_decision(db, owner, run_id, body(db, run_id, decision_id, choice="stop"))
    assert view.state == "stopping"


def test_budget_extend_converts_additions_and_replays(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    db.execute(text("UPDATE runs SET token_limit = 10, usage_tokens = 8 WHERE id=:r"), {"r": run_id})
    db.commit()
    with pytest.raises(DomainError):
        broker.execute(db, broker.issue_capability(db, run_id, 1, 300), request(run_id, reserve_tokens=5))
    row = db.execute(text("SELECT budget_decision_id, token_limit, elapsed_limit_ms FROM runs WHERE id=:r"), {"r": run_id}).one()
    submit = body(db, run_id, row.budget_decision_id, key="budget-1", choice="extend", add_tokens=100, add_elapsed_ms=0)
    view = domain.submit_decision(db, owner, run_id, submit)
    assert db.execute(text("SELECT token_limit FROM runs WHERE id=:r"), {"r": run_id}).scalar_one() == row.token_limit + 100
    assert view.state == "waiting_input" and view.waiting_reason == "budget_exhausted"  # domain keeps it paused
    again = domain.submit_decision(db, owner, run_id, submit)  # replay must not extend twice
    assert again == view
    assert db.execute(text("SELECT token_limit FROM runs WHERE id=:r"), {"r": run_id}).scalar_one() == row.token_limit + 100


def test_choice_specific_fields_are_validated():
    base = dict(decision_id=uuid4(), expected_revision=1, idempotency_key="k")
    with pytest.raises(ValueError):
        DecisionSubmit(**base, choice="retry", add_tokens=1)
    with pytest.raises(ValueError):
        DecisionSubmit(**base, choice="verified_result")
    with pytest.raises(ValueError):
        DecisionSubmit(**base, choice="extend")
    with pytest.raises(ValueError):
        DecisionSubmit(**{**base, "idempotency_key": ""}, choice="stop")
    ref = ObjectRef(project_id=uuid4(), key="k", sha256="a" * 64, size=1, content_type="text/plain")
    DecisionSubmit(**base, choice="verified_result", result=ref)
    DecisionSubmit(**base, choice="extend", add_tokens=0, add_elapsed_ms=5)


def test_recover_created_unknown_gets_a_decision_and_sibling_is_ambiguous(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    # a reserved op left by a crashed generation; recovery turns it unknown
    transport.lose_response = True
    broker.execute(db, broker.issue_capability(db, run_id, 1, 300), request(run_id))
    transport.lose_response = False
    db.execute(text("DELETE FROM owner_decisions WHERE run_id=:r"), {"r": run_id})
    db.execute(text("UPDATE operations SET state='reserved' WHERE run_id=:r"), {"r": run_id})
    db.execute(text("UPDATE runs SET state='running', waiting_reason=NULL WHERE id=:r"), {"r": run_id})
    db.commit()
    bind_executor(db, run_id)
    supervisor.recover(db, run_id)
    row = db.execute(text("SELECT decision_id FROM owner_decisions WHERE run_id=:r AND state='pending'"), {"r": run_id}).scalar_one()
    assert db.execute(text("SELECT count(*) FROM events WHERE run_id=:r AND kind='decision.required' AND payload->>'decision_id'=:d"),
                      {"r": run_id, "d": str(row)}).scalar_one() == 1
    assert domain.submit_decision(db, owner, run_id, body(db, run_id, row)).state == "queued"


def test_unmapped_sibling_unknown_makes_retry_ambiguous(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id)
    db.execute(text("""INSERT INTO operations (id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens, result)
        SELECT :id, run_id, 'operation-x', generation, kind, payload_hash, 'unknown', 0, result FROM operations WHERE run_id=:r"""),
               {"id": uuid4(), "r": run_id})
    db.commit()
    with pytest.raises(DomainError) as err:
        domain.submit_decision(db, owner, run_id, body(db, run_id, decision_id))
    assert err.value.code == "decision_ambiguous"


def test_stale_budget_decision_on_canceled_run_conflicts(broker_fixture, dispatch):
    db, owner, run_id, _, _ = broker_fixture
    did = uuid4()
    db.execute(text("UPDATE runs SET budget_decision_id=:d, state='canceled', waiting_reason=NULL WHERE id=:r"), {"d": did, "r": run_id})
    db.commit()
    with pytest.raises(DomainError) as err:
        domain.submit_decision(db, owner, run_id, body(db, run_id, did, choice="stop"))
    assert (err.value.status, err.value.code) == (409, "revision_conflict")


def test_unknown_stop_choice_fences_to_canceled_and_replays(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id)
    submit = body(db, run_id, decision_id, choice="stop")
    view = domain.submit_decision(db, owner, run_id, submit)
    assert view.state == "stopping"
    final = supervisor.stop(db, run_id, 10)
    assert final.state == "canceled"
    assert domain.submit_decision(db, owner, run_id, submit).state == "canceled"


def test_resolved_decision_rows_are_immutable(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id)
    domain.submit_decision(db, owner, run_id, body(db, run_id, decision_id))
    with pytest.raises(Exception):
        db.execute(text("UPDATE owner_decisions SET reason='budget_exhausted' WHERE decision_id=:d"), {"d": decision_id})
    db.rollback()


def test_registered_exact_proof_beats_in_memory_history_and_test_hook(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id)
    broker._inactive_dispatches.add((run_id, "operation-1", 1))  # in-process history must not count
    broker.configure(dispatch_is_inactive=lambda *a: True)  # nor a test hook
    dispatch.inactive_result = False
    try:
        with pytest.raises(DomainError) as err:
            domain.submit_decision(db, owner, run_id, body(db, run_id, decision_id))
        assert err.value.status == 409
    finally:
        broker.configure(dispatch_is_inactive=None)


def test_replayed_extend_never_reaps_the_resumed_live_generation(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    stopped = []
    dispatch.stop = lambda db_, executor, grace: stopped.append(executor) or True
    db.execute(text("UPDATE runs SET token_limit = 10, usage_tokens = 8 WHERE id=:r"), {"r": run_id})
    db.commit()
    with pytest.raises(DomainError):
        broker.execute(db, broker.issue_capability(db, run_id, 1, 300), request(run_id, reserve_tokens=5))
    row = db.execute(text("SELECT budget_decision_id, revision FROM runs WHERE id=:r"), {"r": run_id}).one()
    submit = body(db, run_id, row.budget_decision_id, key="race-1", choice="extend", add_tokens=100, add_elapsed_ms=0)
    domain.submit_decision(db, owner, run_id, submit)
    assert supervisor.recover(db, run_id, budget_resume=True).state == "queued"
    # claim() scans every queued run in the shared DB, so claim this one directly as the supervisor does
    db.execute(text("UPDATE runs SET state='running', generation=2, lease_expires_at=now()+interval '1 hour' WHERE id=:r AND state='queued'"), {"r": run_id})
    db.commit()
    before = db.execute(text("SELECT state, generation, lease_expires_at FROM runs WHERE id=:r"), {"r": run_id}).one()
    again = supervisor.recover(db, run_id, budget_resume=True)  # the route's resume after an identical replay
    after = db.execute(text("SELECT state, generation, lease_expires_at FROM runs WHERE id=:r"), {"r": run_id}).one()
    assert tuple(before) == tuple(after) and again.state == "running" and after.generation == 2
    assert stopped == [] and db.execute(text("SELECT count(*) FROM runtime_executors WHERE run_id=:r AND state<>'inactive'"), {"r": run_id}).scalar_one() == 0
