"""Owner-confirmed usage for an unknown operation on a canceled run (never an automatic release)."""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import text

from scientist import domain, supervisor
from scientist.auth import DomainError, Principal
from scientist.contracts import DecisionSubmit
from scientist.db import session
from test_broker import broker_fixture  # noqa: F401
from test_owner_decisions import body, bind_executor, dispatch, make_unknown  # noqa: F401
from test_rest_workflow import control_client, storage, _decide  # noqa: F401


def canceled_unknown(db, transport, run_id, **kw):
    decision_id = make_unknown(db, transport, run_id, **kw)
    domain.request_stop(db, Principal(identity=uuid4(), kind="owner"), run_id)
    assert supervisor.stop(db, run_id, 10).state == "canceled"
    return decision_id


def numbers(db, run_id):
    run = db.execute(text("SELECT usage_tokens, reserved_tokens, revision FROM runs WHERE id=:r"), {"r": run_id}).one()
    op = db.execute(text("SELECT state, result FROM operations WHERE run_id=:r AND operation_id='operation-1'"), {"r": run_id}).one()
    return run.usage_tokens, run.reserved_tokens, op.state, op.result


def confirm(db, run_id, did, n=3, key="k1"):
    return body(db, run_id, did, key=key, choice="confirm_usage", usage_tokens=n)


def test_contract_requires_usage_tokens_only_with_confirm_usage():
    base = {"decision_id": "00000000-0000-0000-0000-000000000001", "expected_revision": 1, "idempotency_key": "k"}
    DecisionSubmit(**base, choice="confirm_usage", usage_tokens=0)
    for bad in ({"choice": "confirm_usage"}, {"choice": "retry", "usage_tokens": 1},
                {"choice": "confirm_usage", "usage_tokens": -1}):
        with pytest.raises(ValidationError):
            DecisionSubmit(**base, **bad)


def test_cancel_alone_never_releases_the_reservation(broker_fixture, dispatch):
    db, _, run_id, transport, _ = broker_fixture
    canceled_unknown(db, transport, run_id)  # real path: request_stop then supervisor.stop fencing
    assert db.execute(text("SELECT state FROM runs WHERE id=:r"), {"r": run_id}).scalar_one() == "canceled"
    usage, reserved, state, _ = numbers(db, run_id)
    assert (usage, reserved, state) == (0, 5, "unknown")


def test_confirm_usage_applies_exactly_once_and_replays(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    did = canceled_unknown(db, transport, run_id)
    db.execute(text("UPDATE runs SET reserved_tokens = reserved_tokens + 7 WHERE id=:r"), {"r": run_id})  # another held reservation
    db.commit()
    submit = confirm(db, run_id, did, 3)
    view = domain.submit_decision(db, owner, run_id, submit)
    assert (view.usage_tokens, view.reserved_tokens, view.state) == (3, 7, "canceled")  # 12 - op.reserve(5)
    usage, reserved, state, result = numbers(db, run_id)
    assert (usage, reserved, state) == (3, 7, "committed")
    row = db.execute(text("SELECT state, resolution, idempotency_key, payload_hash FROM owner_decisions WHERE decision_id=:d"), {"d": did}).one()
    assert row.state == "resolved" and row.resolution["choice"] == "confirm_usage" and row.idempotency_key == "k1" and row.payload_hash and result["owner_confirmed_usage"] is True
    events = db.execute(text("SELECT kind, payload FROM events WHERE run_id=:r ORDER BY sequence DESC LIMIT 1"), {"r": run_id}).one()
    assert events.kind == "usage.updated" and events.payload["usage_tokens"] == 3
    again = domain.submit_decision(db, owner, run_id, submit)
    assert (again.usage_tokens, again.reserved_tokens) == (3, 7)
    assert numbers(db, run_id)[:2] == (3, 7)
    with pytest.raises(DomainError) as err:
        domain.submit_decision(db, owner, run_id, confirm(db, run_id, did, 4))
    assert (err.value.status, err.value.code) == (409, "idempotency_conflict")
    assert numbers(db, run_id)[:2] == (3, 7)


def test_concurrent_confirmations_apply_once(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    did = canceled_unknown(db, transport, run_id)
    submit = confirm(db, run_id, did, 4)
    gate = Barrier(4)

    def post(_):
        with session() as s:
            gate.wait()
            try:
                return domain.submit_decision(s, owner, run_id, submit).usage_tokens
            except DomainError as exc:
                s.rollback()
                return exc.status

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(post, range(4)))
    assert results.count(4) >= 1 and set(results) <= {4, 409}
    assert numbers(db, run_id)[:3] == (4, 0, "committed")


def test_unproven_quiescence_changes_nothing(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    did = canceled_unknown(db, transport, run_id)
    dispatch.inactive_result = False
    with pytest.raises(DomainError) as err:
        domain.submit_decision(db, owner, run_id, confirm(db, run_id, did))
    assert err.value.status == 409
    assert numbers(db, run_id)[:3] == (0, 5, "unknown")


def test_usage_above_reserve_is_rejected(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    did = canceled_unknown(db, transport, run_id)
    with pytest.raises(DomainError) as err:
        domain.submit_decision(db, owner, run_id, confirm(db, run_id, did, 6))
    assert err.value.status == 400
    assert numbers(db, run_id)[:3] == (0, 5, "unknown")


def test_confirm_usage_on_a_live_run_is_rejected(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    did = make_unknown(db, transport, run_id)  # waiting_input / unknown_outcome
    with pytest.raises(DomainError) as err:
        domain.submit_decision(db, owner, run_id, confirm(db, run_id, did))
    assert err.value.status == 400
    assert numbers(db, run_id)[:3] == (0, 5, "unknown")
    db.execute(text("UPDATE runs SET state='running' WHERE id=:r"), {"r": run_id})
    db.commit()
    with pytest.raises(DomainError) as err:
        domain.submit_decision(db, owner, run_id, confirm(db, run_id, did, key="k2"))
    assert err.value.status == 400


@pytest.mark.parametrize("choice", ["retry", "stop", "verified_result"])
def test_other_choices_on_a_canceled_run_conflict(broker_fixture, dispatch, choice):
    db, owner, run_id, transport, _ = broker_fixture
    did = canceled_unknown(db, transport, run_id)
    extra = {}
    if choice == "verified_result":
        from scientist.contracts import ObjectRef
        extra["result"] = ObjectRef(project_id=db.execute(text("SELECT project_id FROM runs WHERE id=:r"), {"r": run_id}).scalar_one(),
                                    key="k", sha256="a" * 64, size=1, content_type="text/plain")
    with pytest.raises(DomainError) as err:
        domain.submit_decision(db, owner, run_id, body(db, run_id, did, choice=choice, **extra))
    assert err.value.status == 409
    assert numbers(db, run_id)[:3] == (0, 5, "unknown")


def test_rest_confirm_usage(broker_fixture, dispatch, control_client):
    db, _, run_id, transport, _ = broker_fixture
    did = canceled_unknown(db, transport, run_id)
    rev = db.execute(text("SELECT revision FROM runs WHERE id=:r"), {"r": run_id}).scalar_one()
    r = _decide(control_client, run_id, did, rev, choice="confirm_usage", usage_tokens=2)
    assert r.status_code == 200, r.text
    assert (r.json()["usage_tokens"], r.json()["reserved_tokens"]) == (2, 0)


def test_generated_contracts_are_stable():
    root = Path(__file__).resolve().parents[2] / "contracts"
    files = [root / "api-types.ts", root / "run-event.schema.json"]
    before = [f.read_bytes() for f in files]
    for _ in range(2):
        subprocess.run([sys.executable, "-m", "scientist.contracts"], check=True)
        assert [f.read_bytes() for f in files] == before
    block = (root / "api-types.ts").read_text().split("export interface DecisionSubmit {")[1].split("\n}")[0]
    assert "usage_tokens?:" in block and "confirm_usage" in block


def test_non_owner_is_forbidden(broker_fixture, dispatch):
    db, _, run_id, transport, _ = broker_fixture
    did = canceled_unknown(db, transport, run_id)
    with pytest.raises(DomainError) as err:
        domain.submit_decision(db, Principal(identity=uuid4(), kind="external"), run_id, confirm(db, run_id, did))
    assert err.value.status == 403


@pytest.mark.parametrize("n", [0, 5])
def test_boundaries_zero_and_equal_to_reserve(broker_fixture, dispatch, n):
    db, owner, run_id, transport, _ = broker_fixture
    did = canceled_unknown(db, transport, run_id)
    domain.submit_decision(db, owner, run_id, confirm(db, run_id, did, n))
    assert numbers(db, run_id)[:3] == (n, 0, "committed")


@pytest.mark.parametrize("patch", ["result = result || '{\"usage_known\": true}'::jsonb",
                                   "result = result || '{\"retry_identity\": \"retry-x\"}'::jsonb"])
def test_settled_or_retried_operation_conflicts(broker_fixture, dispatch, patch):
    db, owner, run_id, transport, _ = broker_fixture
    did = canceled_unknown(db, transport, run_id)
    db.execute(text(f"UPDATE operations SET {patch} WHERE run_id=:r"), {"r": run_id})
    db.commit()
    with pytest.raises(DomainError) as err:
        domain.submit_decision(db, owner, run_id, confirm(db, run_id, did))
    assert err.value.status == 409
    assert numbers(db, run_id)[:2] == (0, 5)


def test_reserve_column_is_authoritative_and_drift_is_not_a_500(broker_fixture, dispatch):
    db, owner, run_id, transport, _ = broker_fixture
    did = canceled_unknown(db, transport, run_id)
    # JSON request says 5; the column says 9: the column wins (usage 6 is then valid, 400 would mean JSON was used).
    db.execute(text("UPDATE operations SET reserve_tokens=9 WHERE run_id=:r"), {"r": run_id})
    db.commit()
    with pytest.raises(DomainError) as err:  # run holds only 5, cannot release 9: mapped 409, not IntegrityError
        domain.submit_decision(db, owner, run_id, confirm(db, run_id, did, 6))
    assert err.value.status == 409
    assert numbers(db, run_id)[:3] == (0, 5, "unknown")
