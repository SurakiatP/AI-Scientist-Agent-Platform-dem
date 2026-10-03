import json
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, DBAPIError

from scientist import broker
from scientist.auth import DomainError
from scientist.contracts import CheckpointManifest, ObjectRef
from scientist.runtime_contracts import BoundaryRequest, RuntimeContextV1, canonical_bytes, operation_fingerprint
from scientist.private_worker_api import RuntimePins, WorkerController, create_private_app, parse_boundary
from test_broker import broker_fixture, request as request_for


@pytest.fixture
def controller_fixture(broker_fixture):
    db, owner, run_id, transport, stored = broker_fixture
    row = db.execute(text('SELECT * FROM runs WHERE id=:run'), {'run':run_id}).one()
    plan = broker._load_plan(db, run_id, row.revision)
    pins = RuntimePins(image_digest='sha256:'+'b'*64, skills_digest='c'*64, environment_digest='d'*64)
    calls = []

    def capture(db, run, generation, context_bytes, workspace_dir):
        ctx = RuntimeContextV1.model_validate_json(context_bytes)
        assert ctx.run_id == run and ctx.generation == generation
        calls.append(context_bytes)
        revision = db.execute(text('SELECT COALESCE(MAX(revision),0)+1 FROM checkpoints WHERE run_id=:run'), {'run':run}).scalar_one()
        ref = ObjectRef(project_id=row.project_id, key=f'test/{run}/context',
                        sha256=sha256(context_bytes).hexdigest(), size=len(context_bytes), content_type='application/octet-stream')
        manifest = CheckpointManifest(schema_version=1, run_id=run, revision=revision,
            plan_digest=ctx.plan_digest, runtime_commit=ctx.runtime_commit,
            image_digest=ctx.image_digest, skills_digest=ctx.skills_digest,
            environment_digest=ctx.environment_digest, context=ref, workspace=[],
            operation_ids=[item.operation_id for item in ctx.operation_mappings])
        db.execute(text('INSERT INTO checkpoints(id,run_id,revision,manifest) VALUES (:id,:run,:revision,CAST(:manifest AS jsonb))'),
                   {'id':uuid4(),'run':run,'revision':revision,'manifest':manifest.model_dump_json()})
        return manifest

    controller = WorkerController(pins=pins, provider_destinations={plan.provider_id:'https://research.example'},
                                  capture=capture, result_reader=lambda ref: stored[ref.key])
    context = RuntimeContextV1.model_validate(dict(schema_version=1, run_id=str(run_id), project_id=str(row.project_id),
        generation=row.generation, revision=row.revision, input_snapshot_digest=plan.input_snapshot_digest,
        plan_digest=row.plan_digest.strip(), runtime_commit=pins.runtime_commit, image_digest=pins.image_digest,
        skills_digest=pins.skills_digest, environment_digest=pins.environment_digest,
        provider_id=str(plan.provider_id), provider_endpoint='https://research.example', model=plan.model,
        plan=plan.model_dump(mode='json'), turn_id=str(uuid4()),system_prompt='Fixture assistant',
        messages=[{'role':'user','content':'fixture'}],todo={'todos':[],'revision':0},compacted_context=None,
        boundary='before_model',pending_assistant=None,operation_mappings=[],operation_sequence=0,workspace_manifest=[]))
    token = broker.issue_capability(db, run_id, row.generation, 300)
    boundary = BoundaryRequest(schema_version=1,boundary_id=uuid4(),expected_checkpoint_revision=0,context=context,workspace=[])
    yield db, run_id, token, boundary, controller, calls


def test_boundary_commit_lost_reply_replays_identical_ack(controller_fixture):
    db, run, token, boundary, controller, calls = controller_fixture
    first = controller.boundary(db, token, boundary)
    db.commit()  # The acknowledgement was lost after durable commit.
    replay = controller.boundary(db, token, boundary)
    assert replay == first
    assert len(calls) == 1
    assert first.checkpoint_revision == 1
    assert first.manifest.revision != boundary.context.revision
    changed = boundary.model_copy(update={'context':boundary.context.model_copy(update={'system_prompt':'changed'})})
    with pytest.raises(DomainError, match='revision_conflict'):
        controller.boundary(db, token, changed)
    stale = boundary.model_copy(update={'boundary_id':uuid4()})
    with pytest.raises(DomainError, match='revision_conflict'):
        controller.boundary(db, token, stale)


@pytest.mark.parametrize('field,value', [('project_id',uuid4()),('image_digest','sha256:'+'e'*64),('environment_digest','e'*64)])
def test_controller_binds_context_to_actual_run_and_trusted_pins(controller_fixture,field,value):
    db, run, token, boundary, controller, calls = controller_fixture
    altered = boundary.model_copy(update={'context':boundary.context.model_copy(update={field:value})})
    with pytest.raises(DomainError, match='forbidden'):
        controller.boundary(db,token,altered)
    assert calls == []


def test_current_capability_cannot_read_other_run_or_unknown_result(controller_fixture):
    db, run, token, boundary, controller, calls = controller_fixture
    request = request_for(run,'result-owned')
    result = broker.execute(db,token,request)
    db.commit()
    assert controller.result(db,token,request.operation_id)
    with pytest.raises(DomainError):
        controller.result(db,token,'not-owned')
    db.execute(text("UPDATE runs SET generation=generation+1 WHERE id=:run"),{'run':run})
    db.commit()
    with pytest.raises(DomainError,match='forbidden'):
        controller.result(db,token,request.operation_id)


def test_boundary_routes_are_private_and_json_is_bounded_before_parse(controller_fixture):
    db, run, token, boundary, controller, calls = controller_fixture
    client = TestClient(create_private_app(controller))
    response = client.post('/control/boundary',headers={'X-Worker-Capability':token},json=boundary.model_dump(mode='json'))
    assert response.status_code == 200
    assert response.json()['checkpoint_revision'] == 1
    assert client.post('/control/boundary',headers={'X-Worker-Capability':token},content='{"schema_version":1,"schema_version":1}').status_code == 422
    assert client.post('/control/boundary',json=boundary.model_dump(mode='json')).status_code == 422
    with pytest.raises(DomainError,match='invalid_boundary'):
        parse_boundary(b'{"value":NaN}')
    with pytest.raises(DomainError,match='request_too_large'):
        parse_boundary(b' ' * 9, maximum_bytes=8)


def test_physical_executor_identity_cannot_be_rebound_after_start(controller_fixture):
    db, run, token, boundary, controller, calls = controller_fixture
    executor_id = uuid4()
    db.execute(text("INSERT INTO runtime_executors(id,run_id,generation,kind,process_incarnation,container_id,engine_id,state) "
                    "VALUES (:id,:run,1,'dispatch',:inc,:container,'fixture-engine','active')"),
               {'id':executor_id,'run':run,'inc':uuid4(),'container':'a'*64})
    db.commit()
    with pytest.raises(DBAPIError):
        with db.begin_nested():
            db.execute(text('UPDATE runtime_executors SET container_id=:container WHERE id=:id'),{'id':executor_id,'container':'b'*64})
    db.execute(text("UPDATE runtime_executors SET state='inactive',proof=CAST(:proof AS jsonb) WHERE id=:id"),
               {'id':executor_id,'proof':'{"fixture":true}'})
    assert db.execute(text('SELECT container_id FROM runtime_executors WHERE id=:id'),{'id':executor_id}).scalar_one() == 'a'*64


@pytest.mark.parametrize('dispatched', [False, True])
def test_restore_preserves_ledger_generation_but_rebinds_unsent_requests(controller_fixture, dispatched):
    db, run, token, boundary, controller, calls = controller_fixture
    request = request_for(run,'recover-tool')
    if dispatched:
        assert broker.execute(db,token,request).state == 'committed'
        db.commit()
    data = boundary.context.model_dump(mode='json')
    data['messages'].append({'role':'assistant','content':None,'tool_calls':[
        {'id':'raw-search','type':'function','function':{'name':'search','arguments':'{}'}}]})
    data['boundary'] = 'before_tool'
    data['pending_assistant'] = {'turn_id':data['turn_id'],'message_index':1,'next_tool_index':0}
    data['operation_mappings'] = [{'operation_id':request.operation_id,'turn_id':data['turn_id'],
        'purpose':'tool','model_sequence':None,'tool_call_id':'raw-search',
        'request':request.model_dump(mode='json'),'payload_hash':operation_fingerprint(request)}]
    original = RuntimeContextV1.model_validate(data).model_dump_json().encode()
    db.execute(text('UPDATE runs SET generation=2 WHERE id=:run'),{'run':run})
    db.commit()
    restored = controller.bootstrap_context(db,original,run,2)
    assert restored.generation == 2
    assert restored.operation_mappings[0].request.generation == (1 if dispatched else 2)
    assert RuntimeContextV1.model_validate_json(original).generation == 1
    assert RuntimeContextV1.model_validate_json(original).operation_mappings[0].request.generation == 1
    current_token = broker.issue_capability(db,run,2,300)
    current_boundary = BoundaryRequest(schema_version=1,boundary_id=uuid4(),expected_checkpoint_revision=0,
                                       context=restored,workspace=[])
    controller.boundary(db,current_token,current_boundary)


def test_controller_rejects_checkpoint_ack_with_changed_pins(controller_fixture):
    db, run, token, boundary, controller, calls = controller_fixture
    original = controller.capture
    controller.capture = lambda *args: original(*args).model_copy(update={'image_digest':'sha256:'+'e'*64})
    with pytest.raises(DomainError,match='storage_unavailable'):
        controller.boundary(db,token,boundary)


def test_streaming_limit_rejects_before_json_parse(controller_fixture, monkeypatch):
    import scientist.private_worker_api as private_api
    db, run, token, boundary, controller, calls = controller_fixture
    monkeypatch.setattr(private_api,'MAX_BOUNDARY_BYTES',128)
    def forbidden_parse(*args,**kwargs):
        pytest.fail('oversized request must not reach JSON parsing')
    monkeypatch.setattr(private_api,'parse_boundary',forbidden_parse)
    client = TestClient(create_private_app(controller))
    assert client.post('/control/boundary',headers={'X-Worker-Capability':token},
                       content=iter([b' '*64,b' '*65])).status_code == 413


def test_unknown_and_corrupt_effect_results_are_not_exposed(controller_fixture):
    db, run, token, boundary, controller, calls = controller_fixture
    request = request_for(run,'result-corrupt')
    broker.execute(db,token,request)
    db.commit()
    controller.result_reader = lambda ref: b'changed bytes'
    with pytest.raises(DomainError,match='storage_unavailable'):
        controller.result(db,token,request.operation_id)
    db.execute(text("UPDATE operations SET state='unknown' WHERE run_id=:run AND operation_id=:op"),
               {'run':run,'op':request.operation_id})
    with pytest.raises(DomainError,match='result_unavailable'):
        controller.result(db,token,request.operation_id)


def test_capture_cannot_acknowledge_different_same_project_context(controller_fixture):
    db, run, token, boundary, controller, calls = controller_fixture
    original = controller.capture
    def mixed_capture(db,run,generation,context_bytes,workspace):
        wrong = RuntimeContextV1.model_validate_json(context_bytes).model_copy(update={'system_prompt':'different continuation'})
        return original(db,run,generation,wrong.model_dump_json().encode(),workspace)
    controller.capture = mixed_capture
    with pytest.raises(DomainError,match='storage_unavailable'):
        controller.boundary(db,token,boundary)
    db.rollback()
    assert db.execute(text('SELECT COUNT(*) FROM checkpoint_boundaries WHERE run_id=:run'),{'run':run}).scalar_one() == 0
    assert db.execute(text('SELECT COUNT(*) FROM checkpoints WHERE run_id=:run'),{'run':run}).scalar_one() == 0
