from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from test_peer_broker import peer_run, peer_request, send


@pytest.fixture
def recovery_row(peer_run):
    db, run_id, release, _, _, _, _ = peer_run
    send(db, peer_request(run_id, release))
    identity = uuid4()
    db.execute(text("INSERT INTO runtime_executors(id,run_id,generation,kind,operation_id,process_incarnation,container_id,engine_id,state,peer_reconciliation_attempt) VALUES (:id,:run,2,'dispatch','approved-peer',:inc,:container,'fixture-engine','active',1)"),
               {'id':identity,'run':run_id,'inc':uuid4(),'container':'a'*64})
    db.commit()
    return db, identity, run_id


def test_recovery_executor_started_claim_is_monotonic(recovery_row):
    db, identity, _ = recovery_row
    assert db.execute(text('SELECT peer_reconciliation_started FROM runtime_executors WHERE id=:id'),{'id':identity}).scalar_one() is False
    db.execute(text('UPDATE runtime_executors SET peer_reconciliation_started=true WHERE id=:id'),{'id':identity});db.commit()
    with pytest.raises(DBAPIError):
        db.execute(text('UPDATE runtime_executors SET peer_reconciliation_started=false WHERE id=:id'),{'id':identity})
    db.rollback()
    assert db.execute(text('SELECT peer_reconciliation_started FROM runtime_executors WHERE id=:id'),{'id':identity}).scalar_one() is True


def test_recovery_executor_attempt_binding_cannot_change(recovery_row):
    db, identity, _ = recovery_row
    with pytest.raises(DBAPIError):
        db.execute(text('UPDATE runtime_executors SET peer_reconciliation_attempt=2 WHERE id=:id'),{'id':identity})
    db.rollback()
    assert db.execute(text('SELECT peer_reconciliation_attempt FROM runtime_executors WHERE id=:id'),{'id':identity}).scalar_one() == 1


@pytest.mark.parametrize('fault', ['normal_started','worker_attempt','missing_operation','duplicate_attempt'])
def test_recovery_executor_shape_and_attempt_are_constrained(recovery_row,fault):
    db, identity, run_id = recovery_row
    values={'id':uuid4(),'run':run_id,'inc':uuid4(),'kind':'dispatch','operation':'approved-peer','attempt':1,'started':False,'generation':3}
    if fault=='normal_started':values.update(operation=None,attempt=None,started=True)
    if fault=='worker_attempt':values['kind']='worker'
    if fault=='missing_operation':values['operation']=None
    with pytest.raises(DBAPIError):
        db.execute(text("INSERT INTO runtime_executors(id,run_id,generation,kind,operation_id,process_incarnation,state,peer_reconciliation_attempt,peer_reconciliation_started) VALUES (:id,:run,:generation,:kind,:operation,:inc,'starting',:attempt,:started)"), values)
    db.rollback()
