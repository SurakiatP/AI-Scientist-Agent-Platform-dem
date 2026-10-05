"""Every runs.state transition in broker/supervisor emits a run.state event (SSE/UI must see it)."""
from hashlib import sha256

import pytest
from sqlalchemy import text

from scientist import broker, domain, supervisor
from scientist.auth import DomainError
from scientist.contracts import ObjectRef
from test_broker import PEER_ID, broker_fixture, request  # noqa: F401
from test_owner_decisions import body, dispatch, make_unknown  # noqa: F401


def states(db, run_id):
    return db.execute(text("SELECT payload->>'state' FROM events WHERE run_id=:r AND kind='run.state' ORDER BY sequence"),
                      {"r": run_id}).scalars().all()


def test_unknown_outcome_emits_waiting_input(broker_fixture, dispatch):
    db, _, run_id, transport, _ = broker_fixture
    make_unknown(db, transport, run_id)
    assert states(db, run_id)[-1] == "waiting_input"


def test_retry_choice_emits_queued(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id)
    domain.submit_decision(db, owner, run_id, body(db, run_id, decision_id))
    assert states(db, run_id)[-2:] == ["waiting_input", "queued"]


def test_stop_choice_emits_stopping(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id)
    domain.submit_decision(db, owner, run_id, body(db, run_id, decision_id, choice="stop"))
    assert states(db, run_id)[-2:] == ["waiting_input", "stopping"]


def test_rest_stop_emits_stopping_before_canceled(broker_fixture, dispatch):
    db, _, run_id, _, _ = broker_fixture
    supervisor.stop(db, run_id, 0)
    seen = states(db, run_id)
    assert "stopping" in seen and seen.index("stopping") < seen.index("canceled")


def _crash_after_reservation(db, run_id):
    def crash(request_, target):
        raise SystemExit("simulated worker crash after durable reservation")

    broker.configure(transport=crash, capability_key=b"b" * 32, resolver=lambda host, port: ["8.8.8.8"],
                     peer_destinations={str(PEER_ID): "https://peer.example/a2a"})
    with pytest.raises(SystemExit):
        broker.execute(db, broker.issue_capability(db, run_id, 1, 300), request(run_id))


def test_reconcile_emits_waiting_input_once(broker_fixture):
    db, _, run_id, _, _ = broker_fixture
    _crash_after_reservation(db, run_id)
    with pytest.raises(DomainError, match="revision_conflict"):
        broker.reconcile(db, run_id, "operation-1")
    db.execute(text("UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE id = :run"), {"run": run_id})
    db.commit()
    broker.configure(dispatch_is_inactive=lambda run, operation, generation: True)
    broker.reconcile(db, run_id, "operation-1")
    assert states(db, run_id)[-1] == "waiting_input"
    count = states(db, run_id).count("waiting_input")
    broker.reconcile(db, run_id, "operation-1")
    assert states(db, run_id).count("waiting_input") == count


@pytest.mark.parametrize("lease,expected", [("now() + interval '1 hour'", "running"), ("now() - interval '1 second'", "queued")])
def test_verified_result_emits_next_state(broker_fixture, lease, expected):
    db, owner, run_id, transport, stored = broker_fixture
    transport.lose_response = True
    broker.execute(db, broker.issue_capability(db, run_id, 1, 300), request(run_id))
    project_id = db.execute(text("SELECT project_id FROM runs WHERE id = :run"), {"run": run_id}).scalar_one()
    data = b"provider result"
    digest = sha256(data).hexdigest()
    key = f"verified/{project_id}/{digest}"
    stored[key] = data
    evidence = ObjectRef(project_id=project_id, key=key, sha256=digest, size=len(data), content_type="application/json")
    broker.configure_result_verifier(lambda context, ref: True)
    db.execute(text(f"UPDATE runs SET lease_expires_at = {lease} WHERE id = :run"), {"run": run_id})
    db.commit()
    resolved = broker.resolve_unknown(db, owner, run_id, "operation-1", "verified_result", evidence)
    assert resolved.state == expected and states(db, run_id)[-1] == expected


def test_record_unknown_skips_event_when_already_waiting_input(broker_fixture):
    db, _, run_id, _, _ = broker_fixture
    _crash_after_reservation(db, run_id)
    db.execute(text("UPDATE runs SET state='waiting_input', waiting_reason='budget_exhausted' WHERE id = :run"), {"run": run_id})
    db.commit()
    before = states(db, run_id)
    revision = db.execute(text("SELECT revision FROM runs WHERE id=:r"), {"r": run_id}).scalar_one()
    broker._record_unknown(db, request(run_id), revision, usage_tokens=None, result_ref=None)
    assert states(db, run_id) == before
    assert db.execute(text("SELECT count(*) FROM owner_decisions WHERE run_id=:r AND operation_id='operation-1' AND state='pending'"),
                      {"r": run_id}).scalar_one() == 1
