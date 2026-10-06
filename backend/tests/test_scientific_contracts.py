"""Scientific authority is immutable plan data, never native skill side effects."""
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from scientist.contracts import PlanSpec, ScientificBinding, PreparationSubmit, PreparationJobView
from scientist.runtime_contracts import ScientificResultReceipt
from scientist.runtime_contracts import RuntimeContextV1, canonical_bytes
from test_runtime_contracts import context_data
from hashlib import sha256
from scientist import contracts


def binding(**changes):
    return ScientificBinding(
        **{
            "catalog_commit": "154988403bb5a18e9d3c0ce4e6d5e2e4b184a298",
            "registry_sha256": "a" * 64,
            "capability_ids": ["get-available-resources"],
            "instruction_fingerprint": "b" * 64,
            "profile_id": "prof.worker-base@py3.14.7",
            "profile_version": "1",
            "image_digest": "sha256:" + "c" * 64,
            "input_snapshot_digest": "d" * 64,
            "parameters": {},
            "max_result_bytes": 1048576,
            "timeout_ms": 30000,
            "memory_limit_bytes": 1073741824,
            "workspace_limit_bytes": 67108864,
            **changes,
        }
    )


def plan(scientific=None):
    return PlanSpec(
        input_snapshot_digest="d" * 64, provider_id=uuid4(), model="research-model",
        stages=["Measure resources"], allowed_ops=["llm"], data_recipients=[],
        packages=[], token_limit=5000, elapsed_limit_ms=60000, scientific=scientific,
    )


def test_legacy_plan_identity_omits_scientific_field():
    assert "scientific" not in plan().model_dump(mode="json")


def test_plan_binds_scientific_snapshot():
    assert plan(binding()).scientific.profile_id == "prof.worker-base@py3.14.7"
    with pytest.raises(ValidationError):
        plan(binding(input_snapshot_digest="e" * 64))


@pytest.mark.parametrize("changes", [
    {"capability_ids": ["get-available-resources", "get-available-resources"]},
    {"capability_ids": ["../escape"]}, {"catalog_commit": "unreviewed"},
    {"max_result_bytes": True}, {"max_result_bytes": 1048577},
    {"timeout_ms": 30001}, {"parameters": {"x": float("nan")}},
    {"parameters": {"command": "pip install unreviewed"}},
])
def test_binding_rejects_forged_or_unbounded_authority(changes):
    with pytest.raises(ValidationError):
        binding(**changes)


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a//b", "a/./b", "a\\b"])
def test_receipt_rejects_workspace_escape(path):
    with pytest.raises(ValidationError):
        ScientificResultReceipt(
            tool_call_id="call_resources", capability_id="get-available-resources",
            binding_sha256="a" * 64, path=path, sha256="b" * 64, size=1,
        )


def scientific_context(context_data):
    authority = binding(input_snapshot_digest=context_data["input_snapshot_digest"],
                        image_digest=context_data["image_digest"])
    context_data["plan"]["scientific"] = authority.model_dump(mode="json")
    context_data["plan_digest"] = sha256(canonical_bytes(context_data["plan"])).hexdigest()
    context_data["messages"] += [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_resources", "type": "function", "function": {
                "name": "scientific_resources", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_resources", "content": "Measured resources"},
    ]
    context_data["scientific_results"] = [{
        "tool_call_id": "call_resources", "capability_id": "get-available-resources",
        "binding_sha256": sha256(canonical_bytes(authority.model_dump(mode="json"))).hexdigest(),
        "path": "outputs/resources.json", "sha256": "e" * 64, "size": 10,
    }]
    return context_data


def test_receipt_requires_issued_tool_and_exact_binding(context_data):
    data = scientific_context(context_data)
    assert len(RuntimeContextV1.model_validate(data).scientific_results) == 1
    data["scientific_results"][0]["tool_call_id"] = "never_issued"
    with pytest.raises(ValidationError):
        RuntimeContextV1.model_validate(data)


@pytest.mark.parametrize("field,value", [
    ("binding_sha256", "f" * 64), ("capability_id", "unreviewed"), ("size", 1048577),
])
def test_receipt_cannot_change_approved_computation(context_data, field, value):
    data = scientific_context(context_data)
    data["scientific_results"][0][field] = value
    with pytest.raises(ValidationError):
        RuntimeContextV1.model_validate(data)


def test_receipt_cannot_precede_native_tool_completion(context_data):
    data = scientific_context(context_data)
    data["messages"].pop()
    data["boundary"] = "before_tool"
    data["pending_assistant"] = {"turn_id": data["turn_id"], "message_index": 1, "next_tool_index": 0}
    with pytest.raises(ValidationError):
        RuntimeContextV1.model_validate(data)


def test_legacy_context_omits_scientific_receipts(context_data):
    assert "scientific_results" not in RuntimeContextV1.model_validate(context_data).model_dump(mode="json")

def test_v1_resource_checkpoint_canonical_bytes_are_frozen(context_data):
    context_data.update(
        run_id="00000000-0000-0000-0000-000000000001",
        project_id="00000000-0000-0000-0000-000000000002",
        turn_id="00000000-0000-0000-0000-000000000003",
        provider_id="00000000-0000-0000-0000-000000000004",
    )
    context_data["plan"]["provider_id"] = context_data["provider_id"]
    context_data["scientific_results"] = []
    data = scientific_context(context_data)
    parsed = RuntimeContextV1.model_validate(data)
    wire = canonical_bytes(parsed.model_dump(mode="json"))
    assert parsed.plan_digest == "7793a2fab211ad9557c6c403555deaa807c021c34eea23318e3b91a0844b1165"
    assert sha256(wire).hexdigest() == "28962de465eaadc0aebbb62468d8a9016febe49b2aeb80f7a84d1ead86b086ae"
    assert [item.path for item in parsed.scientific_results] == ["outputs/resources.json"]

def test_crossref_request_is_exact_bounded_and_normalizes_doi():
    model = getattr(contracts, "CrossrefQueryV1", None)
    assert model is not None
    query = model(source_id="crossref", version=1, access_mode="public_read",
                  query="gene expression", doi=None, limit=20)
    assert query.query == "gene expression"
    doi = model(source_id="crossref", version=1, access_mode="public_read",
                query=None, doi="https://doi.org/10.1000/ABC", limit=1)
    assert doi.doi == "10.1000/abc"
    spaced = model(source_id="crossref", version=1, access_mode="public_read",
                   query=None, doi="doi: 10.1234/example", limit=1)
    assert spaced.doi == "10.1234/example"
    with pytest.raises(ValidationError):
        model(source_id="crossref", version=1, access_mode="public_read",
              query=None, doi="doi:\n10.1234/example", limit=1)
    for request in (
        {"source_id": "crossref", "version": 1, "access_mode": "public_read", "query": None, "doi": None, "limit": 1},
        {"source_id": "crossref", "version": 1, "access_mode": "public_read", "query": "x", "doi": "10.1000/x", "limit": 1},
        {"source_id": "crossref", "version": 1, "access_mode": "public_read", "query": "x\nadmin", "doi": None, "limit": 1},
        {"source_id": "crossref", "version": 1, "access_mode": "public_read", "query": "x" * 513, "doi": None, "limit": 1},
        {"source_id": "crossref", "version": 1, "access_mode": "public_read", "query": "x", "doi": None, "limit": True},
        {"source_id": "crossref", "version": 1, "access_mode": "public_read", "query": None, "doi": "https://example.org/works", "limit": 1},
        {"source_id": "crossref", "version": 1, "access_mode": "public_read", "query": "x", "doi": None, "limit": 1, "cursor": "next"},
    ):
        with pytest.raises(ValidationError):
            model(**request)

def test_runtime_pins_move_without_changing_the_private_api_export():
    from scientist import private_worker_api, runtime_contracts
    assert contracts.RuntimePins is private_worker_api.RuntimePins
    assert contracts.RUNTIME_COMMIT == runtime_contracts.RUNTIME_COMMIT

def test_compute_is_an_explicit_operation_kind():
    request = contracts.OperationRequest(
        run_id=uuid4(), generation=1, operation_id="compute-1", kind="compute",
        payload={}, reserve_tokens=0,
    )
    assert request.kind == "compute"

def csv_grant(**changes):
    return {
        "recipe_id": "csv.describe.v1",
        "recipe_version": "1",
        "recipe_manifest_sha256": "f" * 64,
        "profile_id": "prof.csv-stdlib@py3.14.7",
        "profile_version": "1",
        "image_digest": "sha256:" + "a" * 64,
        "input_ref": {
            "project_id": str(UUID(int=9)), "key": "inputs/data.csv",
            "sha256": "b" * 64, "size": 50, "content_type": "text/csv",
        },
        "input_sha256": "b" * 64,
        "numeric_columns": ["x", "y"],
        **changes,
    }

def test_csv_grant_binds_exact_input_recipe_profile_and_fixed_limits():
    model = getattr(contracts, "CsvDescribeGrantV1", None)
    assert model is not None
    grant = model(**csv_grant())
    assert (grant.max_input_bytes, grant.max_output_bytes, grant.timeout_ms,
            grant.memory_limit_bytes, grant.workspace_limit_bytes) == (
                1_048_576, 262_144, 30_000, 1_073_741_824, 67_108_864)
    for changes in (
        {"input_sha256": "c" * 64},
        {"numeric_columns": ["x", "x"]},
        {"numeric_columns": ["x\nheader"]},
        {"max_output_bytes": 262_145},
        {"unapproved_command": "python"},
    ):
        with pytest.raises(ValidationError):
            model(**{**csv_grant(), **changes})

def test_v2_binding_keeps_agent_pins_separate_and_compute_profiles_exact():
    binding_model = getattr(contracts, "ScientificBindingV2", None)
    profile_model = getattr(contracts, "ComputeProfilePin", None)
    assert binding_model is not None and profile_model is not None
    pins = contracts.RuntimePins(
        image_digest="sha256:" + "c" * 64, skills_digest="d" * 64,
        environment_digest="e" * 64,
    )
    query = contracts.CrossrefQueryV1(
        source_id="crossref", version=1, access_mode="public_read",
        query="gene expression", doi=None, limit=5,
    )
    compute_pin = profile_model(
        profile_id="prof.csv-stdlib@py3.14.7", version="1", image_digest="sha256:" + "a" * 64,
    )
    grant = contracts.CsvDescribeGrantV1(**csv_grant())
    binding = binding_model(
        binding_version=2, catalog_commit="154988403bb5a18e9d3c0ce4e6d5e2e4b184a298",
        registry_sha256="a" * 64, capability_ids=["paper-lookup", "exploratory-data-analysis"],
        instruction_fingerprint="b" * 64, agent_runtime_pins=pins, input_snapshot_digest="d" * 64,
        approved_crossref_queries={"request-1": query}, required_compute_profiles=[compute_pin],
        csv_describe_grants={"grant-1": grant},
    )
    parsed = plan(binding).scientific
    assert parsed.binding_version == 2
    assert parsed.agent_runtime_pins == pins
    assert parsed.csv_describe_grants["grant-1"].input_sha256 == "b" * 64
    with pytest.raises(ValidationError):
        binding_model(
            binding_version=2, catalog_commit="154988403bb5a18e9d3c0ce4e6d5e2e4b184a298",
            registry_sha256="a" * 64, capability_ids=["paper-lookup", "exploratory-data-analysis"],
            instruction_fingerprint="b" * 64, agent_runtime_pins=pins, input_snapshot_digest="d" * 64,
            approved_crossref_queries={"request-1": query}, required_compute_profiles=[],
            csv_describe_grants={"grant-1": grant},
        )


def test_preparation_request_cannot_supply_commands():
    with pytest.raises(ValidationError):
        PreparationSubmit(profile_id="prof.worker-base@py3.14.7", version="1",
                          manifest_sha256="a" * 64, request_id=uuid4(), command="pip install x")


def test_preparation_ready_requires_accepted_evidence():
    with pytest.raises(ValidationError):
        PreparationJobView(id=uuid4(), project_id=uuid4(), profile_id="prof.worker-base@py3.14.7",
                           version="1", manifest_sha256="a" * 64, state="ready", stage="complete")
