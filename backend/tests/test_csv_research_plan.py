"""Owner selections must become closed grants on captured input identities."""
import json
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from scientist import research, scientific_authority, settings
from scientist.auth import DomainError
from scientist.contracts import ComputeProfilePin, Principal, RuntimePins
from scientist.domain import submit_run

QUERY = dict(source_id='crossref', version=1, access_mode='public_read',
             query='synthetic csv research', doi=None, limit=2)


def test_prepare_selection_is_closed_and_workflow_specific():
    from scientist.api import PreparePlan
    selection = dict(crossref=QUERY, csv_file_id=str(uuid4()), numeric_columns=['x'])
    body = PreparePlan(expected_revision=1, workflow='crossref_csv', csv_selection=selection)
    assert body.csv_selection.numeric_columns == ['x']
    for changes in [dict(workflow='literature'), dict(csv_selection=None),
                    dict(search_terms=['hidden query']),
                    dict(csv_selection={**selection, 'numeric_columns': ['x', 'x']}),
                    dict(csv_selection={**selection, 'numeric_columns': ['\n']}),
                    dict(csv_selection={**selection, 'credential': 'forbidden'})]:
        with pytest.raises(ValidationError):
            PreparePlan.model_validate({**body.model_dump(), **changes})


def test_csv_plan_binds_only_selected_snapshot_file(db, project_session, monkeypatch):
    from scientist.api import CsvResearchSelection
    import scientist.capability_registry as registry_module
    import scientist.instruction_loader as instructions
    project, session = project_session
    file_id, later_id, provider = uuid4(), uuid4(), uuid4()
    data = b'x,y\n1,2\n3,4\n'
    digest = sha256(data).hexdigest()
    key = f'{project}/{digest}'
    db.execute(text("""INSERT INTO stored_objects(key,project_id,sha256,size,content_type)
        VALUES(:key,:project,:sha,:size,'application/octet-stream')"""),
        dict(key=key, project=project, sha=digest, size=len(data)))
    for identifier in [file_id, later_id]:
        db.execute(text("""INSERT INTO file_versions(id,project_id,filename,object_key,size,content_type,state,sha256)
            VALUES(:id,:project,'data.csv',:key,:size,'text/csv','ready',:sha)"""),
            dict(id=identifier, project=project, key=key, sha=digest, size=len(data)))
    owner = Principal(identity=uuid4(), kind='owner')
    run = submit_run(db, owner, project, session, str(uuid4()), 'CSV analysis',
                     [file_id], provider, 'fixture')
    monkeypatch.setattr(settings, 'provider_endpoint', lambda _: 'https://llm.example')
    monkeypatch.setattr(settings, 'scholarly_endpoints', lambda: ['https://api.crossref.org'])
    pins = RuntimePins(image_digest='sha256:'+'a'*64, skills_digest='b'*64, environment_digest='c'*64)
    monkeypatch.setattr(scientific_authority, '_current_runtime_pins', lambda: pins)
    monkeypatch.setattr(scientific_authority, 'trusted_compute_profile', lambda:
        (ComputeProfilePin(profile_id='prof.csv-stdlib@py3.14.7', version='1', image_digest='sha256:'+'d'*64), 'e'*64))
    selection = SimpleNamespace(catalog_commit='154988403bb5a18e9d3c0ce4e6d5e2e4b184a298',
        registry_sha256='f'*64, capability_ids=['paper-lookup','exploratory-data-analysis'])
    monkeypatch.setattr(registry_module, 'load_registry', lambda _: SimpleNamespace(select=lambda _: selection))
    monkeypatch.setattr(instructions, 'load_instruction_pins', lambda _: {})
    monkeypatch.setattr(instructions, 'load_instruction_bundle', lambda *a, **kw:
        SimpleNamespace(instruction_fingerprint='1'*64))
    requested = CsvResearchSelection(crossref=QUERY, csv_file_id=file_id, numeric_columns=['x'])
    plan = research.build_plan(db, owner, run.run_id, [], workflow='crossref_csv', csv_selection=requested)
    binding = plan.scientific
    grant = binding.csv_describe_grants['csv_describe']
    assert binding.binding_version == 2 and binding.agent_runtime_pins == pins
    assert grant.input_ref.key == key and grant.input_ref.sha256 == digest
    assert grant.numeric_columns == ['x'] and grant.input_ref.size == len(data)
    assert binding.approved_crossref_queries['crossref'].query == QUERY['query']
    assert set(plan.allowed_ops) == {'llm', 'search', 'compute'}
    assert set(plan.data_recipients) == {'https://llm.example','https://api.crossref.org'}
    # Existing project files that were not captured for this run confer no authority.
    with pytest.raises(DomainError, match='scientific_input_unavailable'):
        research.build_plan(db, owner, run.run_id, [], workflow='crossref_csv',
                            csv_selection=requested.model_copy(update={'csv_file_id': later_id}))
