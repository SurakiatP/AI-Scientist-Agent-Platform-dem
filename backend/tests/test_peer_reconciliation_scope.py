import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from scientist import broker, peer_receipt_runtime
from scientist.auth import DomainError
from scientist.dispatch_authority import bind_dispatch
from test_peer_broker import peer_run, peer_request


@pytest.fixture
def peer_read(peer_run, request):
    context_id = getattr(request, 'param', 'known-context')
    db, run_id, release, _, _, _, _ = peer_run
    request = peer_request(run_id, release)
    original = uuid4(); incarnation = uuid4(); container = 'b' * 64
    db.execute(text("INSERT INTO operations(id,run_id,operation_id,generation,kind,payload_hash,state,reserve_tokens,result) VALUES (:id,:run,:op,1,'peer',:hash,'reserved',:reserve,CAST(:result AS jsonb))"),
               {'id':uuid4(),'run':run_id,'op':request.operation_id,'hash':broker._fingerprint(request),'reserve':request.reserve_tokens,'result':json.dumps({'request':request.model_dump(mode='json'),'usage_known':False})})
    db.execute(text('UPDATE runs SET reserved_tokens=:reserve WHERE id=:run'),{'run':run_id,'reserve':request.reserve_tokens})
    db.execute(text("INSERT INTO runtime_executors(id,run_id,generation,kind,process_incarnation,container_id,engine_id,state) VALUES (:id,:run,1,'dispatch',:inc,:container,'fixture-owned-engine','active')"),
               {'id':original,'run':run_id,'inc':incarnation,'container':container})
    db.commit();bind_dispatch(db,original,incarnation,request);db.commit()
    assert peer_receipt_runtime.prepare_submission(run_id,request.operation_id,release.release_id)
    peer_receipt_runtime.record_remote_identity(run_id,request.operation_id,'known-task',context_id)
    broker._record_unknown(db,request,1,usage_tokens=None,result_ref=None)
    proof={'source':'owned-engine-exact-container','container_id':container,'engine_id':'fixture-owned-engine'}
    db.execute(text("UPDATE runtime_executors SET state='inactive',proof=CAST(:proof AS jsonb) WHERE id=:id"),{'id':original,'proof':json.dumps(proof)})
    db.execute(text('UPDATE runs SET generation=2 WHERE id=:run'),{'run':run_id})
    attempt=peer_receipt_runtime.consume_reconciliation_attempt(db,run_id,request.operation_id)
    db.commit()
    return db,run_id,release,request,original,attempt


def read_scope(db,run_id,request,attempt,generation=2):
    from scientist.peer_reconciliation_scope import load_peer_read_scope
    return load_peer_read_scope(db,run_id,request.operation_id,generation,attempt)


def test_reconciliation_scope_uses_durable_remote_identity_and_original_request(peer_read):
    db,run_id,release,request,_,attempt=peer_read
    scope=read_scope(db,run_id,request,attempt)
    assert scope.request==request and scope.request.generation==1
    assert scope.target.peer_release==release and scope.target.url=='https://peer.example/a2a'
    assert (scope.remote_task_id,scope.remote_context_id)==('known-task','known-context')


@pytest.mark.parametrize('tamper',['stale_generation','stale_counter','revoked','origin','unproven_death','active_original','cancel'])
def test_reconciliation_scope_rejects_stale_or_unproven_authority(peer_read,tamper):
    db,run_id,release,request,original,attempt=peer_read
    generation=2
    if tamper=='stale_generation':generation=1
    elif tamper=='stale_counter':attempt+=1
    elif tamper=='revoked':db.execute(text('UPDATE delegations SET revoked_at=now() WHERE peer_id=:peer'),{'peer':release.peer_id})
    elif tamper=='origin':broker._peer_destinations[str(release.peer_id)]='https://changed.example'
    elif tamper=='unproven_death':db.execute(text("UPDATE runtime_executors SET proof='{}'::jsonb WHERE id=:id"),{'id':original})
    elif tamper=='active_original':db.execute(text("UPDATE runtime_executors SET state='active' WHERE id=:id"),{'id':original})
    else:db.execute(text('UPDATE runs SET cancel_requested=true WHERE id=:run'),{'run':run_id})
    db.commit()
    with pytest.raises(DomainError):read_scope(db,run_id,request,attempt,generation)


def activate_reader(peer_read):
    db,run_id,release,request,original,attempt=peer_read
    identity,incarnation=uuid4(),uuid4()
    db.execute(text("INSERT INTO runtime_executors(id,run_id,generation,kind,operation_id,process_incarnation,container_id,engine_id,state,peer_reconciliation_attempt) VALUES (:id,:run,2,'dispatch',:op,:inc,:container,'fixture-owned-engine','active',:attempt)"),
               {'id':identity,'run':run_id,'op':request.operation_id,'inc':incarnation,'container':'c'*64,'attempt':attempt})
    db.commit()
    return db,run_id,request,attempt,identity,incarnation,original


def test_bound_reconciliation_claim_is_committed_once_without_rebinding_original(peer_read):
    from scientist.dispatch_authority import bind_peer_reconciliation
    from scientist.peer_reconciliation_config import PeerReconciliationTarget
    db,run_id,request,attempt,identity,incarnation,original=activate_reader(peer_read)
    target=PeerReconciliationTarget(operation_id=request.operation_id,attempt=attempt)
    scope=bind_peer_reconciliation(db,identity,incarnation,run_id,2,target);db.commit()
    assert scope.remote_task_id=='known-task'
    assert db.execute(text('SELECT peer_reconciliation_started FROM runtime_executors WHERE id=:id'),{'id':identity}).scalar_one() is True
    assert db.execute(text('SELECT executor_id FROM operation_executors WHERE run_id=:run AND operation_id=:op'),{'run':run_id,'op':request.operation_id}).scalar_one()==original
    with pytest.raises(DomainError):bind_peer_reconciliation(db,identity,incarnation,run_id,2,target)


@pytest.mark.parametrize('tamper',['incarnation','executor','generation','attempt','inactive','foreign_active'])
def test_reconciliation_physical_authority_fails_before_started_claim(peer_read,tamper):
    from scientist.dispatch_authority import bind_peer_reconciliation
    from scientist.peer_reconciliation_config import PeerReconciliationTarget
    db,run_id,request,attempt,identity,incarnation,original=activate_reader(peer_read)
    chosen,generation=identity,2
    if tamper=='incarnation':incarnation=uuid4()
    elif tamper=='executor':chosen=uuid4()
    elif tamper=='generation':generation=1
    elif tamper=='attempt':attempt+=1
    elif tamper=='inactive':db.execute(text("UPDATE runtime_executors SET state='inactive' WHERE id=:id"),{'id':identity})
    else:
        db.execute(text("INSERT INTO runtime_executors(id,run_id,generation,kind,process_incarnation,container_id,engine_id,state) VALUES (:id,:run,2,'worker',:inc,:container,'fixture-owned-engine','active')"),{'id':uuid4(),'run':run_id,'inc':uuid4(),'container':'d'*64})
    db.commit()
    with pytest.raises(DomainError):
        bind_peer_reconciliation(db,chosen,incarnation,run_id,generation,PeerReconciliationTarget(operation_id=request.operation_id,attempt=attempt))
    db.rollback()
    assert db.execute(text('SELECT peer_reconciliation_started FROM runtime_executors WHERE id=:id'),{'id':identity}).scalar_one() is False


def test_bound_transport_commits_once_claim_before_reconciliation_callback(peer_read,monkeypatch):
    from scientist.dispatch_authority import BoundDispatchTransport
    from scientist.peer_reconciliation_config import PeerReconciliationTarget
    from scientist.db import session
    db,run_id,request,attempt,identity,incarnation,_=activate_reader(peer_read)
    calls=[]
    def reconcile(scope,generation):
        with session() as witness:
            assert witness.execute(text('SELECT peer_reconciliation_started FROM runtime_executors WHERE id=:id'),{'id':identity}).scalar_one() is True
        calls.append((scope.remote_task_id,generation))
        return 'read-once'
    monkeypatch.setattr(broker,'http_reconcile_peer',reconcile,raising=False)
    transport=BoundDispatchTransport(identity,incarnation,lambda *_: (_ for _ in ()).throw(AssertionError('SendMessage path used')))
    target=PeerReconciliationTarget(operation_id=request.operation_id,attempt=attempt)
    assert transport.reconcile_peer(run_id,2,target)=='read-once'
    with pytest.raises(DomainError):transport.reconcile_peer(run_id,2,target)
    assert calls==[('known-task',2)]


@pytest.mark.parametrize("fault", [None, "put", "cancel", "generation", "revoked", "task_changed", "reader_inactive"])
def test_private_recovery_bridge_reads_known_task_once_and_keeps_reservation(peer_read,monkeypatch,fault):
    import httpx
    import json
    from types import SimpleNamespace
    from scientist import peer_http_exchange
    from scientist.dispatch_authority import BoundDispatchTransport
    from scientist.peer_reconciliation_config import PeerReconciliationTarget
    from scientist.contracts import ObjectRef
    from scientist.db import session
    from hashlib import sha256
    db,run_id,request,attempt,identity,incarnation,original=activate_reader(peer_read)
    calls=[]
    async def exchange(url,method,headers,body,timeout_ms,max_response_bytes):
        with session() as witness:
            assert witness.execute(text('SELECT peer_reconciliation_started FROM runtime_executors WHERE id=:id'),{'id':identity}).scalar_one() is True
        envelope=json.loads(body);calls.append(envelope)
        with session() as changed:
            if fault == 'cancel': changed.execute(text('UPDATE runs SET cancel_requested=true WHERE id=:run'), {'run':run_id})
            elif fault == 'generation': changed.execute(text('UPDATE runs SET generation=3 WHERE id=:run'), {'run':run_id})
            elif fault == 'reader_inactive': changed.execute(text("UPDATE runtime_executors SET state='inactive' WHERE id=:id"), {'id':identity})
            elif fault == 'revoked': changed.execute(text('UPDATE delegations SET revoked_at=now() WHERE peer_id=:peer'), {'peer':peer_read[2].peer_id})
            changed.commit()
        return httpx.Response(200,json={'jsonrpc':'2.0','id':envelope['id'],'result':{'id':'other-task' if fault == 'task_changed' else 'known-task','contextId':'known-context','status':{'state':'TASK_STATE_COMPLETED'}}})
    monkeypatch.setattr(peer_http_exchange,'pinned_exchange',lambda *_args,**_kwargs:exchange)
    monkeypatch.setattr(broker,'read_secret',lambda *_:'dedicated-fixture')
    def persist(_db,project,data,content_type):
        if fault == 'put': raise RuntimeError('synthetic object writer fault')
        with session() as witness:
            assert witness.execute(text('SELECT remote_task_id FROM peer_outbound_receipts WHERE run_id=:run AND operation_id=:op'),{'run':run_id,'op':request.operation_id}).scalar_one()=='known-task'
        return ObjectRef(project_id=project,key='peer/recovered-task',sha256=sha256(data).hexdigest(),size=len(data),content_type=content_type)
    broker._persist_result=persist
    broker._transport=BoundDispatchTransport(identity,incarnation,broker.http_transport)
    config=SimpleNamespace(mode='peer_get_task',run_id=run_id,generation=2,executor_id=identity,process_incarnation=incarnation,
                           peer_reconciliation=PeerReconciliationTarget(operation_id=request.operation_id,attempt=attempt))
    result=broker.reconcile_peer_from_dispatch(config)
    assert result.state=='unknown' and result.usage_tokens is None
    assert len(calls)==1 and calls[0]['method']=='GetTask' and calls[0]['params']['id']=='known-task'
    row=db.execute(text('SELECT state,result FROM operations WHERE run_id=:run AND operation_id=:op'),{'run':run_id,'op':request.operation_id}).one()
    assert row.state == 'unknown'
    if fault is None:
        assert row.result['peer_task_terminal'] is True and row.result['ref']['key']=='peer/recovered-task'
        assert result.result.key == 'peer/recovered-task'
    else:
        assert not row.result.get('peer_task_terminal') and not row.result.get('ref')
        assert result.result is None
    assert db.execute(text('SELECT reserved_tokens FROM runs WHERE id=:run'),{'run':run_id}).scalar_one()==request.reserve_tokens
    assert db.execute(text('SELECT executor_id FROM operation_executors WHERE run_id=:run AND operation_id=:op'),{'run':run_id,'op':request.operation_id}).scalar_one()==original
    with pytest.raises(DomainError):broker.reconcile_peer_from_dispatch(config)
    assert len(calls)==1


@pytest.mark.parametrize("peer_read", [None], indirect=True)
def test_get_task_may_discover_context_without_resending(peer_read, monkeypatch):
    test_private_recovery_bridge_reads_known_task_once_and_keeps_reservation(peer_read, monkeypatch, None)
    db, run_id, _, request, _, _ = peer_read
    assert db.execute(text("SELECT remote_context_id FROM peer_outbound_receipts WHERE run_id=:run AND operation_id=:op"), {"run":run_id,"op":request.operation_id}).scalar_one() == "known-context"
