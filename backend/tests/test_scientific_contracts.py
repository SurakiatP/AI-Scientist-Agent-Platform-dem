"""Scientific authority is immutable plan data, never native skill side effects."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from scientist.contracts import PlanSpec, ScientificBinding, PreparationSubmit, PreparationJobView
from scientist.runtime_contracts import ScientificResultReceipt
from scientist.runtime_contracts import RuntimeContextV1, canonical_bytes
from test_runtime_contracts import context_data
from hashlib import sha256


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


def test_preparation_request_cannot_supply_commands():
    with pytest.raises(ValidationError):
        PreparationSubmit(profile_id="prof.worker-base@py3.14.7", version="1",
                          manifest_sha256="a" * 64, request_id=uuid4(), command="pip install x")


def test_preparation_ready_requires_accepted_evidence():
    with pytest.raises(ValidationError):
        PreparationJobView(id=uuid4(), project_id=uuid4(), profile_id="prof.worker-base@py3.14.7",
                           version="1", manifest_sha256="a" * 64, state="ready", stage="complete")
