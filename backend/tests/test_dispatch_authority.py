from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from scientist import broker
from scientist.auth import DomainError
from scientist.db import session
from scientist.dispatch_authority import BoundDispatchTransport, dispatch_is_inactive
from test_broker import broker_fixture, request


def executor(db, run):
    identity, incarnation = uuid4(), uuid4()
    db.execute(text("INSERT INTO runtime_executors(id,run_id,generation,kind,process_incarnation,"
                    "container_id,engine_id,state) VALUES (:id,:run,1,'dispatch',:inc,:container,'fixture-engine','active')"),
               {'id':identity,'run':run,'inc':incarnation,'container':'a'*64})
    db.commit()
    return identity, incarnation


def test_binding_is_durable_before_transport_and_fenced_proof_is_operation_specific(broker_fixture,monkeypatch):
    db, owner, run, transport, stored = broker_fixture
    identity, incarnation = executor(db,run)
    seen = []
    def observed_transport(req,target):
        with session() as witness:
            bound = witness.execute(text('SELECT executor_id,generation FROM operation_executors WHERE run_id=:run AND operation_id=:op'),
                                    {'run':run,'op':req.operation_id}).one()
            assert bound.executor_id == identity and bound.generation == 1
        seen.append(req.operation_id)
        return transport(req,target)
    monkeypatch.setattr(broker,'_transport',BoundDispatchTransport(identity,incarnation,observed_transport))
    token = broker.issue_capability(db,run,1,300)
    req = request(run,'bound-effect')
    assert broker.execute(db,token,req).state == 'committed'
    db.commit()
    assert seen == [req.operation_id]
    assert not dispatch_is_inactive(db,run,req.operation_id,1)
    db.execute(text("UPDATE runtime_executors SET state='inactive' WHERE id=:id"),{'id':identity})
    db.commit()
    probes = []
    def reaped(inc,engine,container):
        probes.append((inc,engine,container))
        return True
    assert not dispatch_is_inactive(db,run,'never-dispatched',1,probe=reaped)
    assert not dispatch_is_inactive(db,run,req.operation_id,2,probe=reaped)
    assert probes == []
    assert not dispatch_is_inactive(db,run,req.operation_id,1,probe=lambda *args:False)
    assert dispatch_is_inactive(db,run,req.operation_id,1,probe=reaped)
    assert probes == [(incarnation,'fixture-engine','a'*64)]
    assert not dispatch_is_inactive(db,run,req.operation_id,1,probe=lambda *args: (_ for _ in ()).throw(RuntimeError('inspection unavailable')))
    with pytest.raises(DBAPIError):
        with db.begin_nested():
            db.execute(text('UPDATE operation_executors SET executor_id=:executor WHERE run_id=:run AND operation_id=:op'),
                       {'executor':uuid4(),'run':run,'op':req.operation_id})


def test_wrong_executor_incarnation_never_reaches_outbound_transport(broker_fixture,monkeypatch):
    db, owner, run, transport, stored = broker_fixture
    identity, incarnation = executor(db,run)
    seen = []
    monkeypatch.setattr(broker,'_transport',BoundDispatchTransport(identity,uuid4(),lambda *args:seen.append(args)))
    token = broker.issue_capability(db,run,1,300)
    result = broker.execute(db,token,request(run,'wrong-incarnation'))
    db.commit()
    assert result.state == 'unknown'
    assert seen == []
    assert db.execute(text('SELECT COUNT(*) FROM operation_executors WHERE run_id=:run'),{'run':run}).scalar_one() == 0
    assert db.execute(text('SELECT reserved_tokens FROM runs WHERE id=:run'),{'run':run}).scalar_one() == 5


def test_stopping_executor_cannot_begin_a_new_outbound_effect(broker_fixture,monkeypatch):
    db, owner, run, transport, stored = broker_fixture
    identity, incarnation = executor(db,run)
    db.execute(text("UPDATE runtime_executors SET state='fencing' WHERE id=:id"),{'id':identity})
    db.commit()
    seen = []
    monkeypatch.setattr(broker,'_transport',BoundDispatchTransport(identity,incarnation,lambda *args:seen.append(args)))
    token = broker.issue_capability(db,run,1,300)
    assert broker.execute(db,token,request(run,'fenced-new-effect')).state == 'unknown'
    assert seen == []
