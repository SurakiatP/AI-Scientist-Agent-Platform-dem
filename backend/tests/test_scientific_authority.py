"""Run admission rechecks current trusted prerequisites, independent of UI state."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from scientist import domain, profile_preparation as preparation, supervisor
from scientist.auth import DomainError
from scientist.contracts import Principal
from test_scientific_contracts import binding


def test_saved_scientific_plan_cannot_approve_without_accepted_current_environment(db, project_session, monkeypatch):
    from scientist import scientific_authority as authority
    project, session = project_session
    owner = Principal(identity=uuid4(), kind='owner')
    run = domain.submit_run(db, owner, project, session, str(uuid4()), 'Measure resources', [], uuid4(), 'fixture')
    plan = domain.get_plan(db, owner, run.run_id).plan
    profile = next(iter(preparation.load_profiles().values()))
    science = binding(input_snapshot_digest=plan.input_snapshot_digest, profile_id=profile.profile_id,
                      memory_limit_bytes=profile.memory_limit_bytes, workspace_limit_bytes=profile.workspace_limit_bytes,
                      timeout_ms=profile.timeout_ms, max_result_bytes=profile.max_result_bytes)
    monkeypatch.setattr(supervisor, '_config', SimpleNamespace(image_digest=science.image_digest))
    # Declared synthetic instruction identity isolates the actual database/evidence admission guard.
    monkeypatch.setattr(authority, 'validate_instruction', lambda binding, expected_image_digest, **kwargs: None)
    plan = plan.model_copy(update={'scientific': science, 'allowed_ops': ['llm'], 'token_limit': 10000,
                                   'elapsed_limit_ms': 60000, 'data_recipients': ['https://research.example']})
    run = domain.revise_plan(db, owner, run.run_id, run.revision, plan)
    with pytest.raises(DomainError, match='scientific_environment_not_ready'):
        domain.approve_run(db, owner, run.run_id, run.revision, run.plan_digest)
    assert db.execute(text('SELECT count(*) FROM approvals WHERE run_id=:run'), {'run': run.run_id}).scalar_one() == 0
    assert domain.get_run(db, owner, run.run_id).state == 'awaiting_approval'


def test_plan_revision_rejects_changed_instruction_authority_before_persisting(db, project_session, monkeypatch):
    from scientist import scientific_authority as authority
    project, session = project_session
    owner = Principal(identity=uuid4(), kind='owner')
    run = domain.submit_run(db, owner, project, session, str(uuid4()), 'Measure resources', [], uuid4(), 'fixture')
    plan = domain.get_plan(db, owner, run.run_id).plan
    science = binding(input_snapshot_digest=plan.input_snapshot_digest)
    monkeypatch.setattr(supervisor, '_config', SimpleNamespace(image_digest=science.image_digest))
    def reject(*args):
        raise DomainError('scientific_binding_unavailable', 409)
    monkeypatch.setattr(authority, 'validate_instruction', lambda *args, **kwargs: reject(*args))
    with pytest.raises(DomainError, match='scientific_binding_unavailable'):
        domain.revise_plan(db, owner, run.run_id, run.revision, plan.model_copy(update={'scientific': science}))
    assert domain.get_run(db, owner, run.run_id).revision == 1


def test_runtime_validation_uses_dispatch_identity_pins_without_supervisor_config(monkeypatch):
    from scientist import scientific_authority as authority, instruction_loader
    from scientist.contracts import RUNTIME_COMMIT, RuntimePins, ScientificBindingV2

    trusted = RuntimePins(
        runtime_commit=RUNTIME_COMMIT,
        image_digest='sha256:' + 'b' * 64,
        skills_digest='c' * 64,
        environment_digest='d' * 64,
    )
    binding_value = ScientificBindingV2(
        binding_version=2,
        catalog_commit='154988403bb5a18e9d3c0ce4e6d5e2e4b184a298',
        registry_sha256='a' * 64,
        capability_ids=['paper-lookup'],
        instruction_fingerprint='b' * 64,
        agent_runtime_pins=trusted,
        input_snapshot_digest='a' * 64,
    )
    observed = {}
    monkeypatch.setattr(supervisor, '_config', None)
    monkeypatch.setattr(
        instruction_loader,
        'validate_scientific_binding',
        lambda *args, **kwargs: observed.update(kwargs),
    )

    authority.validate_instruction(
        binding_value,
        expected_image_digest=trusted.image_digest,
        trusted_runtime_pins=trusted,
    )

    assert observed['expected_runtime_pins'] == trusted
