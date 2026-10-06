"""Owner HTTP setup boundary; profile builds themselves are tested separately."""
from uuid import uuid4
from types import SimpleNamespace
from sqlalchemy import text

from test_rest_workflow import client, storage, new_project, new_session, submit, make_client


def test_setup_reads_never_launch_and_forged_preparation_is_rejected(client, db):
    project = new_project(client)
    url = f"/api/v1/projects/{project['id']}/research-setup"
    first = client.get(url)
    assert first.status_code == 200, first.text
    assert first.headers['cache-control'] == 'no-store'
    assert first.json()['project_id'] == project['id']
    assert first.json()['preparations'] == []
    assert client.get(url).json() == first.json()
    assert make_client(client.app).get(url, headers={'host': 'localhost'}).status_code == 401

    invalid = client.post(f"/api/v1/projects/{project['id']}/preparations", json={
        'profile_id': 'prof.forged@py3.14.7', 'version': '1',
        'manifest_sha256': 'a' * 64, 'request_id': str(uuid4()),
    })
    assert invalid.status_code in (400, 409, 422)
    extra = client.post(f"/api/v1/projects/{project['id']}/preparations", json={
        'profile_id': 'prof.worker-base@py3.14.7', 'version': '1',
        'manifest_sha256': 'a' * 64, 'request_id': str(uuid4()), 'command': 'pip install evil',
    })
    assert extra.status_code == 422
    assert 'pip install evil' not in extra.text
    assert client.get(url).json()['preparations'] == []


def test_resource_plan_is_explicit_revision_bound_and_cannot_skip_preparation(client, db, monkeypatch):
    from scientist import scientific_authority, settings, supervisor
    from test_scientific_contracts import binding
    # Synthetic instructions isolate the HTTP orchestration; physical readiness is not fabricated.
    monkeypatch.setattr(supervisor, '_config', SimpleNamespace(image_digest='sha256:' + 'a' * 64))
    monkeypatch.setattr(settings, 'provider_endpoint', lambda provider: 'https://research.example')
    monkeypatch.setattr(scientific_authority, 'validate_instruction', lambda *args, **kwargs: None)
    monkeypatch.setattr(scientific_authority, 'resource_binding',
                        lambda digest: binding(input_snapshot_digest=digest))
    project = new_project(client)
    session = new_session(client, project['id'])
    run = submit(client, session['id'])
    plan_url = f"/api/v1/runs/{run['run_id']}/plan"
    plan = client.get(plan_url).json()['plan']
    plan.update(token_limit=9000, elapsed_limit_ms=60000)
    amended = client.patch(plan_url, json={'expected_revision': run['revision'], 'plan': plan})
    assert amended.status_code == 200, amended.text
    current = amended.json()
    prepared = client.post(f"/api/v1/runs/{run['run_id']}/prepare-plan", json={
        'expected_revision': current['revision'], 'workflow': 'resources', 'search_terms': []})
    assert prepared.status_code == 200, prepared.text
    view = prepared.json()
    prepared_plan = client.get(plan_url).json()['plan']
    assert prepared_plan['scientific']['capability_ids'] == ['get-available-resources']
    assert prepared_plan['token_limit'] == 9000 and prepared_plan['elapsed_limit_ms'] == 60000
    assert prepared_plan['allowed_ops'] == ['llm'] and prepared_plan['packages'] == []
    readiness = client.get(f"/api/v1/runs/{run['run_id']}/readiness").json()
    assert readiness['revision'] == view['revision'] and readiness['plan_digest'] == view['plan_digest']
    assert readiness['state'] != 'ready'
    denied = client.post(f"/api/v1/runs/{run['run_id']}/approve", json={
        'expected_revision': view['revision'], 'plan_digest': view['plan_digest']})
    assert denied.status_code == 409, denied.text
    assert db.execute(text('SELECT COUNT(*) FROM approvals WHERE run_id=:run'),
                      {'run': run['run_id']}).scalar_one() == 0


def test_run_readiness_is_current_and_owner_only(client):
    project = new_project(client)
    session = new_session(client, project['id'])
    run = submit(client, session['id'])
    url = f"/api/v1/runs/{run['run_id']}/readiness"
    response = client.get(url)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['run_id'] == run['run_id']
    assert data['revision'] == run['revision']
    assert data['plan_digest'] == run['plan_digest']
    assert response.headers['cache-control'] == 'no-store'
    assert make_client(client.app).get(url, headers={'host': 'localhost'}).status_code == 401
