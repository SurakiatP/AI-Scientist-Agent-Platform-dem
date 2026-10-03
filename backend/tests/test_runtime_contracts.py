from copy import deepcopy
from hashlib import sha256
import base64
import json
from uuid import uuid4
import pytest
from pydantic import ValidationError
from scientist.contracts import PlanSpec, OperationRequest
from scientist.runtime_contracts import RuntimeContextV1, BoundaryRequest, BootstrapMetadata, operation_fingerprint

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()

@pytest.fixture
def context_data():
    provider=uuid4()
    plan=PlanSpec(input_snapshot_digest='a'*64,provider_id=provider,model='fixture',stages=['search'],allowed_ops=['llm','search'],data_recipients=['https://research.example'],packages=[],token_limit=100000,elapsed_limit_ms=60000)
    return dict(schema_version=1,run_id=str(uuid4()),project_id=str(uuid4()),generation=1,revision=3,input_snapshot_digest='a'*64,plan_digest=sha256(canonical(plan.model_dump(mode='json'))).hexdigest(),runtime_commit='bd0affe5e5f723579df8902852f5d0c47795f355',image_digest='sha256:'+'b'*64,skills_digest='c'*64,environment_digest='d'*64,provider_id=str(provider),provider_endpoint='https://research.example',model='fixture',plan=plan.model_dump(mode='json'),turn_id=str(uuid4()),system_prompt='Owned research assistant',messages=[{'role':'user','content':'Find fixture papers'}],todo={'todos':[],'revision':0},compacted_context=None,boundary='before_model',pending_assistant=None,operation_mappings=[],operation_sequence=0,workspace_manifest=[])

def test_context_json_roundtrip_preserves_immutable_identity(context_data):
    parsed=RuntimeContextV1.model_validate(context_data)
    restored=RuntimeContextV1.model_validate_json(parsed.model_dump_json())
    assert restored==parsed

@pytest.mark.parametrize('field,value',[('generation',True),('generation',0),('runtime_commit','unreviewed'),('system_prompt','x'*1048577),('model','unapproved')])
def test_context_rejects_invalid_identity_and_bounds(context_data,field,value):
    context_data[field]=value
    with pytest.raises(ValidationError): RuntimeContextV1.model_validate(context_data)

def test_pending_tool_prefix_preserves_raw_ids_and_arguments(context_data):
    calls=[{'id':'original-a','type':'function','function':{'name':'search_papers','arguments':'{ "query": "fixture" }'}},{'id':'original-b','type':'function','function':{'name':'search_papers','arguments':'{"query":"second"}'}}]
    context_data['messages'] += [{'role':'assistant','content':None,'tool_calls':calls},{'role':'tool','tool_call_id':'original-a','content':'committed result'}]
    context_data['pending_assistant']={'turn_id':context_data['turn_id'],'message_index':1,'next_tool_index':1}
    context_data['boundary']='before_tool'
    parsed=RuntimeContextV1.model_validate(context_data)
    assert parsed.messages[1].tool_calls[0].function.arguments==calls[0]['function']['arguments']
    context_data['pending_assistant']['next_tool_index']=2
    with pytest.raises(ValidationError): RuntimeContextV1.model_validate(context_data)

def test_original_operation_generation_survives_effective_bootstrap(context_data):
    request=OperationRequest(run_id=context_data['run_id'],generation=1,operation_id='model-first',kind='llm',payload={'model':'fixture','provider_id':context_data['provider_id'],'recipient':'https://research.example','messages':context_data['messages'],'max_output_tokens':10},reserve_tokens=100)
    mapping={'operation_id':request.operation_id,'turn_id':context_data['turn_id'],'purpose':'model','model_sequence':0,'tool_call_id':None,'request':request.model_dump(mode='json'),'payload_hash':operation_fingerprint(request)}
    context_data['operation_mappings']=[mapping]
    context_data['operation_sequence']=1
    original=RuntimeContextV1.model_validate(context_data)
    context_data['generation']=2
    rebound=RuntimeContextV1.model_validate(context_data)
    assert original.generation==1 and rebound.generation==2
    assert rebound.operation_mappings[0].request.generation==1
    changed=request.model_copy(update={'generation':2})
    assert operation_fingerprint(changed)==mapping['payload_hash']
    context_data['operation_mappings'][0]['request']['reserve_tokens']=101
    with pytest.raises(ValidationError): RuntimeContextV1.model_validate(context_data)

@pytest.mark.parametrize('path',['../escape','/absolute','nested/../escape','nested//file','back\\slash','a/./file'])
def test_boundary_rejects_paths_and_retains_exact_file_integrity(context_data,path):
    data=b'fixture'
    payload={'schema_version':1,'boundary_id':str(uuid4()),'expected_checkpoint_revision':0,'context':context_data,'workspace':[{'path':path,'sha256':sha256(data).hexdigest(),'size':len(data),'data_base64':base64.b64encode(data).decode()}]}
    with pytest.raises(ValidationError): BoundaryRequest.model_validate(payload)

def test_boundary_rejects_hash_size_and_conflicting_paths(context_data):
    data=b'fixture'
    file={'path':'data.txt','sha256':sha256(data).hexdigest(),'size':len(data),'data_base64':base64.b64encode(data).decode()}
    payload={'schema_version':1,'boundary_id':str(uuid4()),'expected_checkpoint_revision':0,'context':context_data,'workspace':[file]}
    parsed=BoundaryRequest.model_validate(payload)
    assert parsed.workspace[0].decoded_data()==data
    for altered in [dict(file,size=999),dict(file,sha256='e'*64),dict(file,data_base64='not valid')]:
        payload['workspace']=[altered]
        with pytest.raises(ValidationError): BoundaryRequest.model_validate(payload)
    payload['workspace']=[file,dict(file,path='data.txt/child')]
    with pytest.raises(ValidationError): BoundaryRequest.model_validate(payload)

def test_native_todo_and_compression_state_are_explicit(context_data):
    context_data['todo']={'todos':[{'id':'task1','content':'Search evidence','status':'cancelled','parent':None}],'revision':4}
    context_data['compacted_context']={'compression_count':2,'previous_summary':'Retained evidence summary','summary_has_user_turn':True,'ineffective_compression_count':0,'micro':{'cursor':3,'rolling_summary':'Micro summary'}}
    parsed=RuntimeContextV1.model_validate(context_data)
    assert parsed.todo.todos[0].content=='Search evidence'
    assert parsed.compacted_context.compression_count==2
    assert parsed.compacted_context.micro.cursor==3


@pytest.mark.parametrize('field', ['token_limit', 'elapsed_limit_ms'])
def test_approval_numbers_cannot_be_coerced_from_bool(context_data, field):
    context_data['plan'][field] = True
    context_data['plan_digest'] = sha256(canonical(context_data['plan'])).hexdigest()
    with pytest.raises(ValidationError):
        RuntimeContextV1.model_validate(context_data)


def test_operation_mapping_cannot_invent_a_tool_call(context_data):
    request = OperationRequest(run_id=context_data['run_id'], generation=1,
        operation_id='search-unrelated', kind='search', reserve_tokens=0,
        payload={'query':'fixture', 'recipient':'https://research.example'})
    context_data['operation_mappings'] = [{
        'operation_id': request.operation_id, 'turn_id': context_data['turn_id'],
        'purpose':'tool', 'model_sequence':None, 'tool_call_id':'never-issued',
        'request': request.model_dump(mode='json'), 'payload_hash':operation_fingerprint(request)}]
    with pytest.raises(ValidationError):
        RuntimeContextV1.model_validate(context_data)


def test_boundary_cannot_conflict_with_context_workspace(context_data):
    data = b'actual bytes'
    context_data['workspace_manifest'] = [{'path':'other.txt', 'size':len(data), 'sha256':sha256(data).hexdigest()}]
    with pytest.raises(ValidationError):
        BoundaryRequest.model_validate({
            'schema_version':1, 'boundary_id':str(uuid4()), 'expected_checkpoint_revision':0,
            'context':context_data, 'workspace':[{'path':'data.txt','size':len(data),
                'sha256':sha256(data).hexdigest(),'data_base64':base64.b64encode(data).decode()}]})


@pytest.mark.parametrize('boundary', ['before_model', 'final'])
def test_model_or_final_boundary_cannot_leave_pending_tools(context_data, boundary):
    context_data['messages'].append({'role':'assistant','content':None,
        'tool_calls':[{'id':'a','type':'function','function':{'name':'search','arguments':'{}'}}]})
    context_data['pending_assistant'] = {'turn_id':context_data['turn_id'],'message_index':1,'next_tool_index':0}
    context_data['boundary'] = boundary
    with pytest.raises(ValidationError):
        RuntimeContextV1.model_validate(context_data)


def test_depth_limit_rejects_nested_operation_payload_before_hashing(context_data):
    nested = 'leaf'
    for _ in range(18):
        nested = {'nested':nested}
    context_data['unexpected'] = nested
    with pytest.raises(ValidationError):
        RuntimeContextV1.model_validate(context_data)


def test_todo_rejects_parent_cycles(context_data):
    context_data['todo'] = {'revision':1,'todos':[
        {'id':'a','content':'a','status':'pending','parent':'b'},
        {'id':'b','content':'b','status':'pending','parent':'a'}]}
    with pytest.raises(ValidationError):
        RuntimeContextV1.model_validate(context_data)


def test_boundary_accepts_only_canonical_base64(context_data):
    data = b'x'
    assert base64.b64decode('eB==', validate=True) == data  # Nonzero pad bits.
    with pytest.raises(ValidationError):
        BoundaryRequest.model_validate({'schema_version':1,'boundary_id':str(uuid4()),
            'expected_checkpoint_revision':0,'context':context_data,
            'workspace':[{'path':'x','sha256':sha256(data).hexdigest(),'size':1,'data_base64':'eB=='}]})


def test_native_reset_compression_state_roundtrips_without_coercion(context_data):
    context_data['compacted_context'] = {'compression_count':0,'previous_summary':None,
        'summary_has_user_turn':None,'ineffective_compression_count':0,'micro':{}}
    parsed = RuntimeContextV1.model_validate(context_data)
    assert parsed.compacted_context.summary_has_user_turn is None
    assert parsed.compacted_context.micro.defrag_threshold_tokens == 2000


def test_raw_to_applied_tool_ids_are_explicit_and_unambiguous(context_data):
    context_data['messages'].append({'role':'assistant','content':None,'tool_calls':[
        {'id':'raw-a','type':'function','function':{'name':'todo','arguments':'{ }'}},
        {'id':'raw-b','type':'function','function':{'name':'todo','arguments':'{}'}}]})
    context_data['boundary'] = 'before_tool'
    context_data['pending_assistant'] = {'turn_id':context_data['turn_id'],'message_index':1,'next_tool_index':0,
        'applied_tool_ids':[{'raw_id':'raw-a','applied_id':'native-a'},{'raw_id':'raw-b','applied_id':'native-b'}]}
    parsed = RuntimeContextV1.model_validate(context_data)
    assert parsed.pending_assistant.applied_tool_ids[0].raw_id == 'raw-a'
    assert parsed.messages[1].tool_calls[0].function.arguments == '{ }'
    context_data['pending_assistant']['applied_tool_ids'][1]['applied_id'] = 'native-a'
    with pytest.raises(ValidationError):
        RuntimeContextV1.model_validate(context_data)


def test_bootstrap_checkpoint_sequence_is_required_and_distinct_from_run_revision():
    assert BootstrapMetadata(schema_version=1,checkpoint_revision=0).checkpoint_revision == 0
    assert BootstrapMetadata(schema_version=1,checkpoint_revision=12).checkpoint_revision == 12
    for value in ({'schema_version':1}, {'schema_version':True,'checkpoint_revision':0},
                  {'schema_version':1,'checkpoint_revision':True}):
        with pytest.raises(ValidationError):
            BootstrapMetadata.model_validate(value)


def test_native_timestamps_bind_to_canonical_history_without_polluting_wire(context_data):
    context_data['native_message_metadata'] = [{'message_index':0,'timestamp':'2026-10-03T13:00:00+00:00'}]
    parsed = RuntimeContextV1.model_validate(context_data)
    assert parsed.native_message_metadata[0].timestamp == '2026-10-03T13:00:00+00:00'
    assert 'timestamp' not in parsed.messages[0].model_dump()
    for metadata in ([{'message_index':1,'timestamp':'2026-10-03T13:00:00+00:00'}],
                     context_data['native_message_metadata'] * 2,
                     [{'message_index':0,'timestamp':'not-a-timestamp'}]):
        context_data['native_message_metadata'] = metadata
        with pytest.raises(ValidationError):
            RuntimeContextV1.model_validate(context_data)


@pytest.mark.parametrize('timestamp', [0, 1791032400, 1791032400.123456])
def test_native_numeric_timestamp_roundtrip_is_lossless(context_data, timestamp):
    context_data['native_message_metadata'] = [{'message_index':0,'timestamp':timestamp}]
    context_data['native_turn_timestamp'] = timestamp
    parsed = RuntimeContextV1.model_validate(context_data)
    restored = RuntimeContextV1.model_validate_json(parsed.model_dump_json())
    assert restored.native_message_metadata[0].timestamp == timestamp
    assert type(restored.native_message_metadata[0].timestamp) is type(timestamp)
    assert restored.native_turn_timestamp == timestamp
    assert type(restored.native_turn_timestamp) is type(timestamp)


@pytest.mark.parametrize('timestamp', [True, False, -1, float('nan'), float('inf'), -float('inf')])
def test_native_numeric_timestamp_rejects_invalid_values(context_data, timestamp):
    for field in ('native_message_metadata', 'native_turn_timestamp'):
        data = dict(context_data)
        data[field] = ([{'message_index':0,'timestamp':timestamp}]
                       if field == 'native_message_metadata' else timestamp)
        with pytest.raises(ValidationError):
            RuntimeContextV1.model_validate(data)


def test_current_turn_anchor_must_reference_primary_user(context_data):
    context_data['current_turn_user_index'] = 0
    assert RuntimeContextV1.model_validate(context_data).current_turn_user_index == 0
    context_data['messages'].append({'role':'assistant','content':'done'})
    for anchor in (True, -1, 1, 2):
        context_data['current_turn_user_index'] = anchor
        with pytest.raises(ValidationError):
            RuntimeContextV1.model_validate(context_data)
