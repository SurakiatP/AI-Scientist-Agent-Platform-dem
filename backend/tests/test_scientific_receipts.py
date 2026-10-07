"""Actual PostgreSQL receipt/CAS tests; object capture is a declared fixture."""
import base64
import json
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import text

from scientist import broker, domain
from scientist.auth import DomainError
from scientist.contracts import (
    CheckpointManifest, ComputeProfilePin, CsvDescribeGrantV1, ObjectRef,
    OperationRequest, PlanSpec, Principal, RuntimePins, ScientificBindingV2,
)
from scientist.private_worker_api import RuntimePins, WorkerController
from scientist.runtime_contracts import (
    BoundaryRequest, ComputeOutputEntryV2, ComputeResultEnvelopeV2, OperationMapping,
    RuntimeContextV1, ScientificOutputReceiptV2, ScientificResultReceiptV2,
    ScientificResultReceipt,
    WorkspaceEntry, WorkspaceFile, canonical_bytes, compute_input_manifest_sha256,
    operation_fingerprint,
)
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
    final_data = boundary.context.model_dump(mode="json")
    final_data["boundary"] = "final"
    final_data["messages"][0]["content"] = "Measure resources"
    final_data["messages"].append({"role": "assistant", "content": "The resource report is ready."})
    final = boundary.model_copy(update={"boundary_id": uuid4(), "expected_checkpoint_revision": 1,
        "context": RuntimeContextV1.model_validate(final_data)})
    controller.boundary(db, token, final)
    assert db.execute(text("SELECT COUNT(*) FROM scientific_artifact_receipts WHERE run_id=:run"), {"run": boundary.context.run_id}).scalar_one() == 1
    assert db.execute(text("SELECT partial FROM artifacts WHERE id=:id"), {"id": artifact.id}).scalar_one() is False


def test_plot_receipt_registers_svg_artifact_once(receipt_boundary):
    from types import SimpleNamespace

    db, token, controller, boundary, _ = receipt_boundary
    controller.boundary(db, token, boundary)
    raw = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
    digest = sha256(raw).hexdigest()
    path = "outputs/plots/demo.svg"
    receipt = ScientificResultReceipt(tool_call_id="plot-call", capability_id="scientific-visualization",
                                      binding_sha256="a" * 64, path=path, sha256=digest, size=len(raw))
    row = SimpleNamespace(id=boundary.context.run_id, project_id=boundary.context.project_id,
                          revision=boundary.context.revision)
    checkpoint_id = db.execute(text("SELECT id FROM checkpoints WHERE run_id=:run ORDER BY revision DESC LIMIT 1"),
                               {"run": row.id}).scalar_one()
    entry = WorkspaceEntry(path=path, sha256=digest, size=len(raw))
    ref = ObjectRef(project_id=row.project_id, key=path, sha256=digest, size=len(raw),
                    content_type="image/svg+xml")
    context = SimpleNamespace(scientific_results=[receipt], scientific_results_v2=[], boundary="tool_committed")
    manifest = SimpleNamespace(workspace=[ref])
    controller._register_scientific_results(db, row, context, [entry], manifest, checkpoint_id)
    controller._register_scientific_results(db, row, context, [entry], manifest, checkpoint_id)
    artifact = db.execute(text("SELECT title,kind,content_type,partial FROM artifacts "
                               "WHERE run_id=:run AND kind='plot'"), {"run": row.id}).one()
    assert (artifact.title, artifact.kind, artifact.content_type, artifact.partial) == (
        "Scientific plot", "plot", "image/svg+xml", True)
    assert db.execute(text("SELECT COUNT(*) FROM scientific_artifact_receipts "
                           "WHERE run_id=:run AND tool_call_id='plot-call'"), {"run": row.id}).scalar_one() == 1


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



def test_v2_receipt_validation_uses_the_committed_effect_result(receipt_boundary, monkeypatch):
    from types import SimpleNamespace

    db, _token, controller, boundary, _captured = receipt_boundary
    project_id = boundary.context.project_id
    input_sha = "b" * 64
    input_ref = ObjectRef(
        project_id=project_id,
        key=f"{project_id}/{input_sha}",
        sha256=input_sha,
        size=20,
        content_type="application/octet-stream",
    )
    grant = CsvDescribeGrantV1(
        recipe_id="csv.describe.v1",
        recipe_version="1",
        recipe_manifest_sha256="f" * 64,
        profile_id="prof.csv-stdlib@py3.14.7",
        profile_version="1",
        image_digest="sha256:" + "a" * 64,
        input_ref=input_ref,
        input_sha256=input_sha,
        numeric_columns=["x"],
    )
    binding = ScientificBindingV2(
        binding_version=2,
        catalog_commit="154988403bb5a18e9d3c0ce4e6d5e2e4b184a298",
        registry_sha256="c" * 64,
        capability_ids=["exploratory-data-analysis"],
        instruction_fingerprint="d" * 64,
        agent_runtime_pins=RuntimePins(
            runtime_commit=boundary.context.runtime_commit,
            image_digest=boundary.context.image_digest,
            skills_digest=boundary.context.skills_digest,
            environment_digest=boundary.context.environment_digest,
        ),
        input_snapshot_digest=boundary.context.input_snapshot_digest,
        required_compute_profiles=[ComputeProfilePin(
            profile_id=grant.profile_id,
            version=grant.profile_version,
            image_digest=grant.image_digest,
        )],
        csv_describe_grants={"grant-1": grant},
    )
    request = OperationRequest(
        run_id=boundary.context.run_id,
        generation=boundary.context.generation,
        kind="compute",
        operation_id="csv-operation",
        reserve_tokens=0,
        payload={"grant_id": "grant-1", "grant": grant.model_dump(mode="json")},
    )
    fingerprint = operation_fingerprint(request)
    names = ["summary.json", "summary.csv", "chart.svg", "report.md"]
    types = ["application/json", "text/csv", "image/svg+xml", "text/markdown"]
    contents = [b"{}", b"x,count\n1,1\n", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', b"# report\n"]
    outputs = []
    files = []
    for index, (name, content_type, content) in enumerate(zip(names, types, contents, strict=True)):
        digest = sha256(content).hexdigest()
        ref = ObjectRef(
            project_id=project_id,
            key=f"{project_id}/{digest}",
            sha256=digest,
            size=len(content),
            content_type="application/octet-stream",
        )
        outputs.append(ComputeOutputEntryV2(
            index=index,
            name=name,
            content_type=content_type,
            object_ref=ref,
            data_base64=base64.b64encode(content).decode(),
        ))
        files.append(WorkspaceFile(
            path=f"outputs/{name}",
            sha256=digest,
            size=len(content),
            data_base64=base64.b64encode(content).decode(),
        ))
    retry_request = request.model_copy(update={
        "operation_id": "retry-csv-operation",
        "generation": boundary.context.generation + 1,
    })
    later_generation = retry_request.generation + 1
    retry_fingerprint = operation_fingerprint(retry_request)
    envelope = ComputeResultEnvelopeV2(
        schema_version=2,
        binding_sha256=sha256(canonical_bytes(binding.model_dump(mode="json"))).hexdigest(),
        grant_id="grant-1",
        operation_id=retry_request.operation_id,
        operation_fingerprint=retry_fingerprint,
        recipe_id=grant.recipe_id,
        recipe_version=grant.recipe_version,
        recipe_manifest_sha256=grant.recipe_manifest_sha256,
        profile_id=grant.profile_id,
        profile_version=grant.profile_version,
        image_digest=grant.image_digest,
        input_manifest_sha256=compute_input_manifest_sha256(grant),
        input_ref=grant.input_ref,
        input_sha256=grant.input_sha256,
        outputs=outputs,
    )
    envelope_bytes = canonical_bytes(envelope.model_dump(mode="json"))
    envelope_sha = sha256(envelope_bytes).hexdigest()
    envelope_ref = ObjectRef(
        project_id=project_id,
        key=f"{project_id}/{envelope_sha}",
        sha256=envelope_sha,
        size=len(envelope_bytes),
        content_type="application/octet-stream",
    )
    receipt_outputs = [
        ScientificOutputReceiptV2(
            index=item.index,
            name=item.name,
            content_type=item.content_type,
            path=f"outputs/{item.name}",
            sha256=item.object_ref.sha256,
            size=item.object_ref.size,
            object_ref=item.object_ref,
        )
        for item in outputs
    ]
    receipt = ScientificResultReceiptV2(
        receipt_version=2,
        tool_call_id="csv-call",
        grant_id="grant-1",
        binding_sha256=envelope.binding_sha256,
        operation_id=request.operation_id,
        effective_operation_id=retry_request.operation_id,
        operation_fingerprint=retry_fingerprint,
        input_manifest_sha256=envelope.input_manifest_sha256,
        recipe_manifest_sha256=grant.recipe_manifest_sha256,
        profile_id=grant.profile_id,
        profile_version=grant.profile_version,
        image_digest=grant.image_digest,
        input_sha256=grant.input_sha256,
        outputs=receipt_outputs,
    )
    mapping = OperationMapping(
        operation_id=request.operation_id,
        turn_id=boundary.context.turn_id,
        purpose="tool",
        model_sequence=None,
        tool_call_id="csv-call",
        request=request,
        payload_hash=fingerprint,
    )
    controller.result_reader = lambda _ref: envelope_bytes
    context = SimpleNamespace(
        plan=SimpleNamespace(scientific=binding),
        operation_mappings=[mapping],
        scientific_results_v2=[receipt],
    )
    row = SimpleNamespace(
        id=boundary.context.run_id, project_id=project_id, generation=later_generation,
    )
    original_result = {
        "request": request.model_dump(mode="json"),
        "retry_identity": retry_request.operation_id,
        "retry_request": retry_request.model_dump(mode="json"),
    }
    retry_result = {
        "request": retry_request.model_dump(mode="json"),
        "ref": envelope_ref.model_dump(mode="json"),
    }
    db.execute(
        text("UPDATE runs SET generation = :generation WHERE id = :run"),
        {"generation": later_generation, "run": request.run_id},
    )
    db.execute(
        text("""
            INSERT INTO operations
                (id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens, result)
            VALUES
                (:id, :run, :operation, :generation, :kind, :hash, 'unknown', :reserve, CAST(:result AS jsonb))
        """),
        {
            "id": uuid4(), "run": request.run_id, "operation": request.operation_id,
            "generation": request.generation, "kind": request.kind, "hash": fingerprint,
            "reserve": request.reserve_tokens, "result": json.dumps(original_result),
        },
    )
    db.execute(
        text("""
            INSERT INTO operations
                (id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens, result)
            VALUES
                (:id, :run, :operation, :generation, :kind, :hash, 'committed', :reserve, CAST(:result AS jsonb))
        """),
        {
            "id": uuid4(), "run": request.run_id, "operation": retry_request.operation_id,
            "generation": retry_request.generation, "kind": retry_request.kind,
            "hash": operation_fingerprint(retry_request), "reserve": retry_request.reserve_tokens,
            "result": json.dumps(retry_result),
        },
    )
    db.commit()
    controller._validate_scientific_compute_results(
        db, row, context, {item.path: item for item in files}
    )
    future_row = SimpleNamespace(
        id=row.id, project_id=row.project_id, generation=retry_request.generation - 1,
    )
    with pytest.raises(DomainError, match="invalid_scientific_result"):
        controller._validate_scientific_compute_results(
            db, future_row, context, {item.path: item for item in files}
        )
    incomplete_files = {item.path: item for item in files if item.path != "outputs/report.md"}
    with pytest.raises(DomainError, match="invalid_scientific_result"):
        controller._validate_scientific_compute_results(db, row, context, incomplete_files)

    unrelated_request = retry_request.model_copy(update={
        "operation_id": "unrelated-committed-operation",
    })
    db.execute(
        text("""
            INSERT INTO operations
                (id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens, result)
            VALUES
                (:id, :run, :operation, :generation, :kind, :hash, 'committed', :reserve, CAST(:result AS jsonb))
        """),
        {
            "id": uuid4(), "run": request.run_id, "operation": unrelated_request.operation_id,
            "generation": unrelated_request.generation, "kind": unrelated_request.kind,
            "hash": operation_fingerprint(unrelated_request), "reserve": unrelated_request.reserve_tokens,
            "result": json.dumps({
                "request": unrelated_request.model_dump(mode="json"),
                "ref": envelope_ref.model_dump(mode="json"),
            }),
        },
    )
    db.commit()
    unrelated_context = SimpleNamespace(
        plan=context.plan,
        operation_mappings=context.operation_mappings,
        scientific_results_v2=[receipt.model_copy(update={
            "effective_operation_id": unrelated_request.operation_id,
        })],
    )
    with pytest.raises(DomainError, match="invalid_scientific_result"):
        controller._validate_scientific_compute_results(
            db, row, unrelated_context, {item.path: item for item in files}
        )

    mismatched_retry = retry_request.model_copy(update={"payload": {"grant_id": "other-grant"}})
    db.execute(
        text("UPDATE operations SET result = CAST(:result AS jsonb) WHERE run_id = :run AND operation_id = :operation"),
        {
            "result": json.dumps({**original_result, "retry_request": mismatched_retry.model_dump(mode="json")}),
            "run": request.run_id,
            "operation": request.operation_id,
        },
    )
    db.commit()
    with pytest.raises(DomainError, match="invalid_scientific_result"):
        controller._validate_scientific_compute_results(
            db, row, context, {item.path: item for item in files}
        )

    mismatched_generation = retry_request.model_copy(update={
        "generation": retry_request.generation + 1,
    })
    db.execute(
        text("UPDATE operations SET result = CAST(:result AS jsonb) WHERE run_id = :run AND operation_id = :operation"),
        {
            "result": json.dumps({
                **original_result,
                "retry_request": mismatched_generation.model_dump(mode="json"),
            }),
            "run": request.run_id,
            "operation": request.operation_id,
        },
    )
    db.commit()
    with pytest.raises(DomainError, match="invalid_scientific_result"):
        controller._validate_scientific_compute_results(
            db, row, context, {item.path: item for item in files}
        )

    db.execute(
        text("UPDATE operations SET result = CAST(:result AS jsonb) WHERE run_id = :run AND operation_id = :operation"),
        {"result": json.dumps(original_result), "run": request.run_id, "operation": request.operation_id},
    )
    db.execute(
        text("UPDATE operations SET state = 'unknown' WHERE run_id = :run AND operation_id = :operation"),
        {"run": request.run_id, "operation": retry_request.operation_id},
    )
    db.commit()
    with pytest.raises(DomainError, match="result_unavailable"):
        controller._validate_scientific_compute_results(
            db, row, context, {item.path: item for item in files}
        )


def test_v2_compute_receipt_publishes_four_downloadable_artifacts_and_events(receipt_boundary):
    from types import SimpleNamespace

    db, token, controller, boundary, captured = receipt_boundary
    controller.boundary(db, token, boundary)
    project_id = boundary.context.project_id
    names = ["summary.json", "summary.csv", "chart.svg", "report.md"]
    content_types = ["application/json", "text/csv", "image/svg+xml", "text/markdown"]
    data = [b"{}", b"x,count\n1,1\n", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', b"# report\n"]
    outputs = []
    files = []
    refs = []
    for index, (name, content_type, raw) in enumerate(zip(names, content_types, data, strict=True)):
        digest = sha256(raw).hexdigest()
        ref = ObjectRef(project_id=project_id, key=f"{project_id}/{digest}", sha256=digest, size=len(raw), content_type="application/octet-stream")
        refs.append(ref)
        outputs.append(ScientificOutputReceiptV2(index=index, name=name, content_type=content_type, path=f"outputs/{name}", sha256=digest, size=len(raw), object_ref=ref))
        files.append(WorkspaceEntry(path=f"outputs/{name}", sha256=digest, size=len(raw)))
    receipt = ScientificResultReceiptV2(
        receipt_version=2, tool_call_id="csv-call", grant_id="grant-1", binding_sha256="a" * 64,
        operation_id="csv-operation", effective_operation_id="csv-operation", operation_fingerprint="b" * 64,
        input_manifest_sha256="c" * 64, recipe_manifest_sha256="d" * 64,
        profile_id="prof.csv-stdlib@py3.14.7", profile_version="1",
        image_digest="sha256:" + "e" * 64, input_sha256="f" * 64, outputs=outputs,
    )
    checkpoint_id = db.execute(
        text("SELECT id FROM checkpoints WHERE run_id=:run ORDER BY revision DESC LIMIT 1"),
        {"run": boundary.context.run_id},
    ).scalar_one()
    context = SimpleNamespace(scientific_results=[], scientific_results_v2=[receipt], boundary="tool_committed")
    row = SimpleNamespace(id=boundary.context.run_id, project_id=project_id, revision=boundary.context.revision)
    manifest = SimpleNamespace(workspace=refs)

    controller._register_scientific_results(db, row, context, files, manifest, checkpoint_id)

    published = db.execute(
        text("SELECT output_index,artifact_id,receipt_sha256 FROM scientific_artifact_receipts WHERE run_id=:run AND tool_call_id='csv-call' ORDER BY output_index"),
        {"run": row.id},
    ).all()
    assert [item.output_index for item in published] == list(range(4))
    assert len({item.receipt_sha256.strip() for item in published}) == 1
    assert db.execute(
        text("SELECT COUNT(*) FROM artifacts WHERE run_id=:run AND title IN ('summary.json','summary.csv','chart.svg','report.md') AND partial=true"),
        {"run": row.id},
    ).scalar_one() == 4
    assert db.execute(
        text("SELECT COUNT(*) FROM events WHERE run_id=:run AND kind='artifact.ready' AND payload->'artifact'->>'title' IN ('summary.json','summary.csv','chart.svg','report.md')"),
        {"run": row.id},
    ).scalar_one() == 4
    controller._register_scientific_results(db, row, context, files, manifest, checkpoint_id)
    assert db.execute(
        text("SELECT COUNT(*) FROM scientific_artifact_receipts WHERE run_id=:run AND tool_call_id='csv-call'"),
        {"run": row.id},
    ).scalar_one() == 4
    assert db.execute(
        text("SELECT COUNT(*) FROM events WHERE run_id=:run AND kind='artifact.ready' AND payload->'artifact'->>'title' IN ('summary.json','summary.csv','chart.svg','report.md')"),
        {"run": row.id},
    ).scalar_one() == 4
    context.boundary = "final"
    controller._register_scientific_results(db, row, context, files, manifest, checkpoint_id)
    assert db.execute(
        text("SELECT COUNT(*) FROM artifacts WHERE run_id=:run AND title IN ('summary.json','summary.csv','chart.svg','report.md') AND partial=false"),
        {"run": row.id},
    ).scalar_one() == 4
    assert db.execute(
        text("SELECT COUNT(*) FROM events WHERE run_id=:run AND kind='artifact.ready' AND payload->'artifact'->>'title' IN ('summary.json','summary.csv','chart.svg','report.md')"),
        {"run": row.id},
    ).scalar_one() == 8
