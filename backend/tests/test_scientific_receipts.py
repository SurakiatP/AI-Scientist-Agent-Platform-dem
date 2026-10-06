"""Actual PostgreSQL receipt/CAS tests; object capture is a declared fixture."""
import base64
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import text

from scientist import broker, domain
from scientist.auth import DomainError
from scientist.contracts import CheckpointManifest, ObjectRef, PlanSpec, Principal
from scientist.private_worker_api import RuntimePins, WorkerController
from scientist.runtime_contracts import BoundaryRequest, RuntimeContextV1, canonical_bytes
from scientist.resource_recipe import canonical_resource_result, WorkerResources, get_available_resources_recipe
from test_runtime_contracts import context_data
from test_scientific_contracts import binding, scientific_context


@pytest.fixture
def receipt_boundary(db, project_session, context_data, monkeypatch):
    from scientist import scientific_authority
    # This fixture exercises receipt/CAS persistence, not instruction/profile admission.
    monkeypatch.setattr(scientific_authority, 'validate_plan_binding', lambda *args, **kwargs: None)
    project, session_id = project_session
    owner = Principal(identity=uuid4(), kind="owner")
    run = domain.submit_run(db, owner, project, session_id, str(uuid4()), "Measure resources",
                            [], uuid4(), "fixture")
    plan = domain.get_plan(db, owner, run.run_id).plan
    authority = binding(input_snapshot_digest=plan.input_snapshot_digest, image_digest=context_data["image_digest"])
    plan = plan.model_copy(update={"scientific": authority, "data_recipients": ["https://research.example"],
                                   "allowed_ops": ["llm"], "token_limit": 10000, "elapsed_limit_ms": 60000})
    run = domain.revise_plan(db, owner, run.run_id, run.revision, plan)
    domain.approve_run(db, owner, run.run_id, run.revision, run.plan_digest)
    db.execute(text("UPDATE runs SET state='running',generation=1,lease_expires_at=now()+interval '1 hour' WHERE id=:run"), {"run": run.run_id})
    data = {**context_data, "run_id": str(run.run_id), "project_id": str(project),
            "revision": run.revision, "input_snapshot_digest": plan.input_snapshot_digest,
            "plan": plan.model_dump(mode="json"), "plan_digest": run.plan_digest,
            "provider_id": str(plan.provider_id), "model": plan.model}
    data = scientific_context(data)
    result = canonical_resource_result(
        get_available_resources_recipe(WorkerResources(2, 2.0, 1024, 512)),
        profile_id=authority.profile_id,
        instruction_fingerprint=authority.instruction_fingerprint,
    )
    receipt = data["scientific_results"][0]
    receipt.update(sha256=sha256(result).hexdigest(), size=len(result))
    data["boundary"] = "tool_committed"
    context = RuntimeContextV1.model_validate(data)
    captured = []

    def capture(database, run_id, generation, raw_context, workspace):
        ctx = RuntimeContextV1.model_validate_json(raw_context)
        revision = database.execute(text("SELECT COALESCE(MAX(revision),0)+1 FROM checkpoints WHERE run_id=:run"), {"run": run_id}).scalar_one()
        def ref(raw):
            digest = sha256(raw).hexdigest()
            return ObjectRef(project_id=project, key=f"{project}/{digest}", sha256=digest,
                             size=len(raw), content_type="application/octet-stream")
        manifest = CheckpointManifest(schema_version=1, run_id=run_id, revision=revision,
            plan_digest=ctx.plan_digest, runtime_commit=ctx.runtime_commit, image_digest=ctx.image_digest,
            skills_digest=ctx.skills_digest, environment_digest=ctx.environment_digest,
            context=ref(raw_context), workspace=[ref((workspace / receipt["path"]).read_bytes())], operation_ids=[])
        database.execute(text("INSERT INTO checkpoints(id,run_id,revision,manifest) VALUES(:id,:run,:rev,CAST(:manifest AS jsonb))"),
                         {"id": uuid4(), "run": run_id, "rev": revision, "manifest": manifest.model_dump_json()})
        captured.append(manifest)
        return manifest

    pins = RuntimePins(image_digest=context.image_digest, skills_digest=context.skills_digest,
                       environment_digest=context.environment_digest)
    broker.configure(capability_key=b"s" * 32)
    token = broker.issue_capability(db, run.run_id, 1, 300)
    controller = WorkerController(pins=pins, provider_destinations={plan.provider_id: "https://research.example"},
                                    capture=capture, scientific_validator=lambda db, project, binding: None)
    boundary = BoundaryRequest(schema_version=1, boundary_id=uuid4(), expected_checkpoint_revision=0,
        context=context, workspace=[{**{k:receipt[k] for k in ("path", "sha256", "size")},
                                     "data_base64": base64.b64encode(result).decode()}])
    yield db, token, controller, boundary, captured
    broker.configure(capability_key=None)


def test_scientific_artifact_is_registered_once_and_finalized(receipt_boundary):
    db, token, controller, boundary, captured = receipt_boundary
    first = controller.boundary(db, token, boundary)
    assert controller.boundary(db, token, boundary) == first
    assert len(captured) == 1
    artifact = db.execute(text("SELECT * FROM artifacts WHERE run_id=:run"), {"run": boundary.context.run_id}).one()
    assert artifact.sha256.strip() == boundary.context.scientific_results[0].sha256
    assert artifact.partial is True
    final = boundary.model_copy(update={"boundary_id": uuid4(), "expected_checkpoint_revision": 1,
        "context": boundary.context.model_copy(update={"boundary": "final"})})
    controller.boundary(db, token, final)
    assert db.execute(text("SELECT COUNT(*) FROM scientific_artifact_receipts WHERE run_id=:run"), {"run": boundary.context.run_id}).scalar_one() == 1
    assert db.execute(text("SELECT partial FROM artifacts WHERE id=:id"), {"id": artifact.id}).scalar_one() is False


def test_missing_scientific_bytes_never_capture_or_publish(receipt_boundary):
    db, token, controller, boundary, captured = receipt_boundary
    missing = boundary.model_copy(update={"workspace": []})
    with pytest.raises(DomainError, match="invalid_scientific_result"):
        controller.boundary(db, token, missing)
    assert captured == []
    assert db.execute(text("SELECT COUNT(*) FROM artifacts WHERE run_id=:run"), {"run": boundary.context.run_id}).scalar_one() == 0


def test_scientific_environment_must_be_configured(receipt_boundary):
    db, token, controller, boundary, captured = receipt_boundary
    controller.scientific_validator = None
    with pytest.raises(DomainError, match="scientific_environment_unavailable"):
        controller.boundary(db, token, boundary)
    assert captured == []


def test_hash_matched_invalid_measurement_never_capture_or_publish(receipt_boundary):
    db, token, controller, boundary, captured = receipt_boundary
    result = b'{"cpu_count":true,"gpu_validation":true}'
    data = boundary.model_dump(mode="json")
    receipt = data["context"]["scientific_results"][0]
    receipt.update(sha256=sha256(result).hexdigest(), size=len(result))
    data["workspace"][0].update(sha256=receipt["sha256"], size=receipt["size"],
                                data_base64=base64.b64encode(result).decode())
    invalid = BoundaryRequest.model_validate(data)
    with pytest.raises(DomainError, match="invalid_scientific_result"):
        controller.boundary(db, token, invalid)
    assert captured == []
    assert db.execute(text("SELECT COUNT(*) FROM artifacts WHERE run_id=:run"),
                      {"run": boundary.context.run_id}).scalar_one() == 0
