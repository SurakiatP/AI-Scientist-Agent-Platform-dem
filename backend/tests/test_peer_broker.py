from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import text

from scientist import broker
from scientist.auth import DomainError
from scientist.contracts import ObjectRef, OperationRequest
from scientist.domain import approve_run, revise_plan
from test_peer_contracts import OWNER, _run, _plan, _release


@dataclass
class Transport:
    targets: list = field(default_factory=list)
    def __call__(self, request, target):
        self.targets.append(target)
        return b'{"synthetic":true}', 0


@pytest.fixture
def peer_run(db, project_session):
    project_id, _ = project_session
    run, digest = _run(db, project_session)
    origin = "https://peer.example"
    release = _release(digest, endpoint_fingerprint=sha256(origin.encode()).hexdigest())
    plan = _plan(digest, [release])
    run = revise_plan(db, OWNER, run.run_id, run.revision, plan)
    run = approve_run(db, OWNER, run.run_id, run.revision, run.plan_digest)
    credential_id = uuid4()
    delegation_id = uuid4()
    db.execute(text("INSERT INTO delegations(id,project_id,owner_identity,peer_id,actions) VALUES (:id,:project,:owner,:peer,ARRAY['peer'])"),
               {"id": delegation_id, "project": project_id, "owner": OWNER.identity, "peer": release.peer_id})
    db.execute(text("INSERT INTO credentials(id,project_id,label,provider,encrypted_value) VALUES (:id,:project,'fixture',:provider,:value)"),
               {"id": credential_id, "project": project_id, "provider": f"peer:{release.peer_id}", "value": b"offline-only"})
    db.execute(text("UPDATE runs SET state='running',generation=1,lease_expires_at=now()+interval '1 hour' WHERE id=:run"), {"run": run.run_id})
    db.commit()
    transport = Transport()
    stored = []
    def persist(_db, project_id, data, content_type):
        stored.append(data)
        return ObjectRef(project_id=project_id, key="peer/result", sha256=sha256(data).hexdigest(), size=len(data), content_type=content_type)
    broker.configure(transport=transport, persist_result=persist, capability_key=b"p" * 32,
                     resolver=lambda host, port: ["8.8.8.8"], peer_destinations={str(release.peer_id): origin})
    yield db, run.run_id, release, credential_id, delegation_id, transport, stored
    broker.configure()


def peer_request(run_id, release, **changes):
    values = dict(run_id=run_id, generation=1, operation_id="approved-peer", kind="peer",
                  payload={"release_id": str(release.release_id), "parameters": release.approved_parameters},
                  reserve_tokens=release.reserved_tokens)
    values.update(changes)
    return OperationRequest(**values)


def send(db, request):
    return broker.execute(db, broker.issue_capability(db, request.run_id, 1, 300), request)


def test_peer_target_comes_only_from_exact_approved_release(peer_run):
    db, run_id, release, credential_id, _, transport, stored = peer_run
    result = send(db, peer_request(run_id, release))
    assert result.state == "committed"
    target = transport.targets[0]
    assert target.url == "https://peer.example/a2a"
    assert target.credential_id == credential_id
    assert target.peer_release == release
    assert len(stored) == 1


@pytest.mark.parametrize("tamper", ["unknown_release", "parameters", "peer_id", "url", "credential_id", "task_id", "headers", "reserve"])
def test_peer_release_mismatch_fails_before_reservation_or_transport(peer_run, tamper):
    db, run_id, release, _, _, transport, _ = peer_run
    request = peer_request(run_id, release)
    payload = dict(request.payload)
    if tamper == "unknown_release": payload["release_id"] = str(uuid4())
    elif tamper == "parameters": payload["parameters"] = {"message": {"messageId": "different"}}
    elif tamper == "reserve": request = request.model_copy(update={"reserve_tokens": release.reserved_tokens - 1})
    else: payload[tamper] = "unapproved"
    request = request.model_copy(update={"payload": payload})
    with pytest.raises(DomainError):
        send(db, request)
    assert transport.targets == []
    assert db.execute(text("SELECT count(*) FROM operations WHERE run_id=:run"), {"run": run_id}).scalar_one() == 0
    assert db.execute(text("SELECT reserved_tokens FROM runs WHERE id=:run"), {"run": run_id}).scalar_one() == 0


@pytest.mark.parametrize("tamper", ["revoked", "credential_project", "credential_peer", "credential_model", "origin"])
def test_live_peer_authority_mismatch_fails_before_transport(peer_run, tamper):
    db, run_id, release, credential_id, delegation_id, transport, _ = peer_run
    if tamper == "revoked": db.execute(text("UPDATE delegations SET revoked_at=now() WHERE id=:id"), {"id": delegation_id})
    elif tamper == "credential_project": db.execute(text("UPDATE credentials SET project_id=NULL WHERE id=:id"), {"id": credential_id})
    elif tamper == "credential_peer": db.execute(text("UPDATE credentials SET provider='peer:wrong' WHERE id=:id"), {"id": credential_id})
    elif tamper == "credential_model": db.execute(text("UPDATE credentials SET model='wrong-model' WHERE id=:id"), {"id": credential_id})
    else: broker._peer_destinations[str(release.peer_id)] = "https://different.example"
    db.commit()
    with pytest.raises(DomainError):
        send(db, peer_request(run_id, release))
    assert transport.targets == []


def test_sdk_submission_is_bound_and_identity_committed_before_result_put(peer_run, monkeypatch):
    import httpx
    import json
    from scientist import peer_http_exchange
    from scientist.db import session
    from scientist.dispatch_authority import BoundDispatchTransport
    db, run_id, release, credential_id, _, _, stored = peer_run
    executor_id, incarnation = uuid4(), uuid4()
    db.execute(text("INSERT INTO runtime_executors(id,run_id,generation,kind,process_incarnation,container_id,engine_id,state) VALUES (:id,:run,1,'dispatch',:inc,:container,'fixture-owned-engine','active')"),
               {"id": executor_id, "run": run_id, "inc": incarnation, "container": "d" * 64})
    db.commit()
    outgoing = []
    async def exchange(url, method, headers, body, timeout_ms, max_response_bytes):
        with session() as witness:
            binding = witness.execute(text("SELECT executor_id FROM operation_executors WHERE run_id=:run AND operation_id='approved-peer'"), {"run": run_id}).scalar_one()
            receipt = witness.execute(text("SELECT state FROM peer_outbound_receipts WHERE run_id=:run AND operation_id='approved-peer'"), {"run": run_id}).scalar_one()
        assert binding == executor_id and receipt == 'prepared'
        outgoing.append(json.loads(body))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": outgoing[-1]["id"], "result": {"task": {"id": "fixture-task", "contextId": "fixture-context", "status": {"state": "TASK_STATE_COMPLETED"}}}})
    monkeypatch.setattr(broker, "read_secret", lambda _db, chosen: "dedicated-fixture" if chosen == credential_id else (_ for _ in ()).throw(AssertionError("wrong credential")))
    monkeypatch.setattr(peer_http_exchange, "pinned_exchange", lambda *_args, **_kwargs: exchange)
    def persist(_db, project, data, content_type):
        with session() as witness:
            receipt = witness.execute(text("SELECT remote_task_id,remote_context_id FROM peer_outbound_receipts WHERE run_id=:run AND operation_id='approved-peer'"), {"run": run_id}).one()
        assert tuple(receipt) == ('fixture-task','fixture-context')
        stored.append(data)
        return ObjectRef(project_id=project,key='peer/native-result',sha256=sha256(data).hexdigest(),size=len(data),content_type=content_type)
    broker._transport = BoundDispatchTransport(executor_id, incarnation, broker.http_transport)
    broker._persist_result = persist
    result = send(db, peer_request(run_id, release))
    assert result.state == 'unknown' and result.usage_tokens is None
    assert len(outgoing) == 1 and len(stored) == 1
    assert outgoing[0]['method'] == 'SendMessage'
    row = db.execute(text("SELECT result FROM operations WHERE run_id=:run AND operation_id='approved-peer'"), {'run':run_id}).scalar_one()
    assert row['ref']['key'] == 'peer/native-result' and row['usage_known'] is False
    assert row['peer_task_terminal'] is True
    run = db.execute(text("SELECT reserved_tokens,usage_tokens,state FROM runs WHERE id=:run"),{'run':run_id}).one()
    assert tuple(run) == (release.reserved_tokens,0,'waiting_input')
