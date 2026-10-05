"""Owner REST view of current, durable pending decisions."""
from uuid import uuid4

from sqlalchemy import text

from scientist import broker, domain
from scientist.auth import DomainError
from scientist.contracts import Principal
from test_broker import broker_fixture, request
from test_canceled_reconciliation import canceled_unknown, confirm
from test_owner_decisions import bind_executor, dispatch, make_unknown  # noqa: F401
from test_rest_workflow import control_client, storage, _decide, _revision  # noqa: F401


def _pending(client, run_id):
    return client.get(f"/api/v1/runs/{run_id}/pending-decisions")


def test_unknown_pending_decision_exposes_only_its_operation_reservation(broker_fixture, dispatch, control_client):
    db, _, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id)
    reserve = db.execute(text(
        "SELECT reserve_tokens FROM operations WHERE run_id=:run AND operation_id='operation-1'"
    ), {"run": run_id}).scalar_one()

    response = _pending(control_client, run_id)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == [{
        "decision_id": str(decision_id),
        "reason": "unknown_outcome",
        "required_tokens": None,
        "required_elapsed_ms": None,
        "operation_reserved_tokens": reserve,
    }]


def test_confirmed_canceled_decision_disappears_from_owner_pending_list(broker_fixture, dispatch, control_client):
    db, _, run_id, transport, _ = broker_fixture
    decision_id = canceled_unknown(db, transport, run_id)

    before = _pending(control_client, run_id)
    assert before.status_code == 200, before.text
    assert [row["decision_id"] for row in before.json()] == [str(decision_id)]

    confirmed = _decide(control_client, run_id, decision_id, _revision(db, run_id),
                        choice="confirm_usage", usage_tokens=2)
    assert confirmed.status_code == 200, confirmed.text

    after = _pending(control_client, run_id)
    assert after.status_code == 200, after.text
    assert after.json() == []


def test_multiple_pending_operations_are_listed_in_event_order_with_individual_caps(
    broker_fixture, dispatch, control_client
):
    db, _, run_id, transport, _ = broker_fixture
    first_id = make_unknown(db, transport, run_id, operation_id="operation-1")
    db.execute(text("UPDATE runs SET state='running', waiting_reason=NULL, lease_expires_at=now()+interval '1 hour' WHERE id=:run"), {"run": run_id})
    db.commit()
    transport.lose_response = True
    broker.execute(db, broker.issue_capability(db, run_id, 1, 300),
                   request(run_id, operation_id="operation-2", reserve_tokens=7))
    transport.lose_response = False
    bind_executor(db, run_id, operation_id="operation-2")
    second_id = db.execute(text(
        "SELECT decision_id FROM owner_decisions WHERE run_id=:run AND operation_id='operation-2' AND state='pending'"
    ), {"run": run_id}).scalar_one()

    response = _pending(control_client, run_id)

    assert response.status_code == 200, response.text
    assert [(item["decision_id"], item["operation_reserved_tokens"]) for item in response.json()] == [
        (str(first_id), 5), (str(second_id), 7),
    ]


def test_stale_unknown_decision_for_retried_operation_is_not_actionable(broker_fixture, dispatch, control_client):
    db, _, run_id, transport, _ = broker_fixture
    make_unknown(db, transport, run_id)
    db.execute(text("""
        UPDATE operations SET result = COALESCE(result, '{}'::jsonb) || '{"retry_identity":"retry-1"}'::jsonb
        WHERE run_id=:run AND operation_id='operation-1'
    """), {"run": run_id})
    db.commit()

    response = _pending(control_client, run_id)

    assert response.status_code == 200, response.text
    assert response.json() == []


def test_pending_decisions_are_owner_only_and_run_scoped(broker_fixture, dispatch, control_client):
    db, owner, run_id, transport, _ = broker_fixture
    make_unknown(db, transport, run_id)

    try:
        domain.get_pending_decisions(db, Principal(identity=uuid4(), kind="external"), run_id)
    except DomainError as error:
        assert (error.status, error.code) == (403, "forbidden")
    else:
        raise AssertionError("external principals must not read owner decisions")

    assert _pending(control_client, uuid4()).status_code == 404


def test_owner_cannot_confirm_usage_above_operation_reservation(
    broker_fixture, dispatch, control_client
):
    db, _, run_id, transport, _ = broker_fixture
    decision_id = canceled_unknown(db, transport, run_id)

    response = _decide(
        control_client,
        run_id,
        decision_id,
        _revision(db, run_id),
        choice="confirm_usage",
        usage_tokens=6,
    )

    assert response.status_code == 400, response.text
    pending = _pending(control_client, run_id)
    assert pending.status_code == 200, pending.text
    assert [row["decision_id"] for row in pending.json()] == [str(decision_id)]
