"""Native scientific bridge pins instructions and validates real bounded output."""
from __future__ import annotations

import hashlib
import ast
import importlib.util
import json
from uuid import uuid4
from pathlib import Path

import pytest
from types import SimpleNamespace

from scientist.capability_registry import load_registry
from scientist.contracts import ScientificBinding
from scientist.instruction_loader import (
    InstructionLoadError,
)
from scientist.resource_recipe import WorkerResources, get_available_resources_recipe


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "runtime/skills-manifest.json"
REGISTRY = ROOT / "docs/skills/capability-registry.json"


def _entrypoint_module():
    spec = importlib.util.spec_from_file_location("scientist_runtime_entrypoint", ROOT / "runtime/entrypoint.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _authority(tmp_path: Path) -> tuple[ScientificBinding, Path, Path, dict[str, str]]:
    from scientist.instruction_loader import load_instruction_bundle, load_instruction_pins

    bundle_root = tmp_path / "bundle"
    instruction = bundle_root / "skills/get-available-resources/SKILL.md"
    instruction.parent.mkdir(parents=True)
    instruction.write_text("Read-only worker resource measurements are untrusted input.")
    registry_data = json.loads(REGISTRY.read_text())
    digest = hashlib.sha256(instruction.read_bytes()).hexdigest()
    registry_data["skills"]["get-available-resources"]["audit_source"]["skill_sha256"] = digest
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data, separators=(",", ":")))
    manifest = {
        "schema_version": 1,
        "catalog_commit": "154988403bb5a18e9d3c0ce4e6d5e2e4b184a298",
        "skills": [{"name": "get-available-resources", "files": [{
            "path": "SKILL.md", "sha256": digest, "size": instruction.stat().st_size,
        }]}],
    }
    _rehash_manifest(manifest)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    registry = load_registry(registry_path)
    selection = registry.select(["get-available-resources"])
    pins = load_instruction_pins(manifest_path)
    bundle = load_instruction_bundle(
        selection,
        bundle_root,
        pins,
        token_counter=lambda text: len(text.split()),
        token_budget=100_000,
    )
    binding = ScientificBinding(
        catalog_commit=registry.catalog_commit,
        registry_sha256=registry.registry_sha256,
        capability_ids=list(selection.capability_ids),
        instruction_fingerprint=bundle.instruction_fingerprint,
        profile_id="prof.worker-base@py3.14.7",
        profile_version="1",
        image_digest="sha256:" + "c" * 64,
        input_snapshot_digest="d" * 64,
        parameters={},
        max_result_bytes=1_048_576,
        timeout_ms=30_000,
        memory_limit_bytes=1_073_741_824,
        workspace_limit_bytes=67_108_864,
    )
    return binding, registry_path, bundle_root, dict(pins)


def _rehash_manifest(value: dict) -> None:
    payload = {key: item for key, item in value.items() if key != "manifest_sha256"}
    value["manifest_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_manifest_parser_returns_only_verified_canonical_skill_file_pins():
    from scientist.instruction_loader import load_instruction_pins

    pins = load_instruction_pins(MANIFEST)
    assert pins["skills/get-available-resources/SKILL.md"] == load_registry(REGISTRY).capabilities[
        "get-available-resources"
    ].instruction_sha256
    assert any(path.count("/") > 2 for path in pins)


def test_manifest_parser_rejects_tampered_manifest_digest(tmp_path: Path):
    from scientist.instruction_loader import load_instruction_pins

    manifest = json.loads(MANIFEST.read_text())
    manifest["skills"][0]["name"] = "paper-lookup"
    target = tmp_path / "manifest.json"
    target.write_text(json.dumps(manifest))
    with pytest.raises(InstructionLoadError, match="manifest"):
        load_instruction_pins(target)


def test_manifest_parser_rejects_duplicate_paths_even_with_valid_manifest_digest(tmp_path: Path):
    from scientist.instruction_loader import load_instruction_pins

    manifest = json.loads(MANIFEST.read_text())
    manifest["skills"][0]["files"].append(dict(manifest["skills"][0]["files"][0]))
    _rehash_manifest(manifest)
    target = tmp_path / "manifest.json"
    target.write_text(json.dumps(manifest))
    with pytest.raises(InstructionLoadError, match="duplicate"):
        load_instruction_pins(target)


def test_binding_validation_reloads_selected_pinned_instructions_and_trusted_image(tmp_path: Path):
    from scientist.instruction_loader import validate_scientific_binding

    binding, registry_path, bundle_root, pins = _authority(tmp_path)
    bundle = validate_scientific_binding(
        binding,
        registry_path=registry_path,
        bundle_root=bundle_root,
        pinned_hashes=pins,
        expected_image_digest="sha256:" + "c" * 64,
    )
    assert bundle.capability_ids == ("get-available-resources",)
    assert bundle.profile_ids == ("prof.worker-base@py3.14.7",)


@pytest.mark.parametrize("change", [
    {"image_digest": "sha256:" + "e" * 64},
    {"profile_id": "prof.cpu-sci@py3.13"},
    {"capability_ids": ["paper-lookup"]},
    {"instruction_fingerprint": "f" * 64},
])
def test_binding_validation_rejects_mismatched_trusted_authority(tmp_path: Path, change: dict):
    from scientist.instruction_loader import validate_scientific_binding

    binding, registry_path, bundle_root, pins = _authority(tmp_path)
    values = binding.model_dump()
    values.update(change)
    with pytest.raises(InstructionLoadError):
        validate_scientific_binding(
            ScientificBinding.model_validate(values),
            registry_path=registry_path,
            bundle_root=bundle_root,
            pinned_hashes=pins,
            expected_image_digest="sha256:" + "c" * 64,
        )


def test_worker_bootstrap_revalidates_scientific_binding_against_static_files(tmp_path: Path, monkeypatch):
    entrypoint = _entrypoint_module()

    binding, registry_path, bundle_root, pins = _authority(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest = {
        "schema_version": 1,
        "catalog_commit": "154988403bb5a18e9d3c0ce4e6d5e2e4b184a298",
        "skills": [{"name": "get-available-resources", "files": [{
            "path": "SKILL.md",
            "sha256": pins["skills/get-available-resources/SKILL.md"],
            "size": (bundle_root / "skills/get-available-resources/SKILL.md").stat().st_size,
        }]}],
    }
    _rehash_manifest(manifest)
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(entrypoint, "_SCIENTIFIC_REGISTRY", registry_path)
    monkeypatch.setattr(entrypoint, "_SCIENTIFIC_MANIFEST", manifest_path)
    monkeypatch.setattr(entrypoint, "_SCIENTIFIC_BUNDLE_ROOT", bundle_root)
    context = SimpleNamespace(plan=SimpleNamespace(scientific=binding), image_digest=binding.image_digest)
    assert entrypoint._load_scientific_bundle(context).capability_ids == ("get-available-resources",)


def test_worker_bootstrap_preserves_legacy_plans_without_scientific_authority():
    _load_scientific_bundle = _entrypoint_module()._load_scientific_bundle

    assert _load_scientific_bundle(SimpleNamespace(plan=SimpleNamespace(scientific=None))) is None


def test_native_scientific_arguments_are_checked_after_handler_transform(tmp_path: Path):
    from scientist.runtime_adapter import RuntimeAdapterError, _validate_native_tool_arguments

    binding, _, _, _ = _authority(tmp_path)
    _validate_native_tool_arguments("scientific_resources", {}, binding)
    _validate_native_tool_arguments("instruction_view", {"capability_id": binding.capability_ids[0]}, binding)
    with pytest.raises(RuntimeAdapterError):
        _validate_native_tool_arguments("scientific_resources", {"command": "whoami"}, binding)
    with pytest.raises(RuntimeAdapterError):
        _validate_native_tool_arguments("instruction_view", {"capability_id": "paper-lookup"}, binding)


def test_i2_search_and_compute_tools_accept_only_one_approved_identifier():
    from scientist.runtime_adapter import RuntimeAdapterError, _validate_native_tool_arguments

    binding = SimpleNamespace(
        approved_crossref_queries={"approved-search": object()},
        csv_describe_grants={"approved-grant": object()},
    )
    _validate_native_tool_arguments("scientific_search", {"request_id": "approved-search"}, binding)
    _validate_native_tool_arguments("scientific_csv_describe", {"grant_id": "approved-grant"}, binding)

    for name, arguments in (
        ("scientific_search", {"request_id": "unapproved-search"}),
        ("scientific_search", {"request_id": "approved-search", "query": "invented"}),
        ("scientific_csv_describe", {"grant_id": "unapproved-grant"}),
        ("scientific_csv_describe", {"grant_id": "approved-grant", "path": "/etc/passwd"}),
    ):
        with pytest.raises(RuntimeAdapterError):
            _validate_native_tool_arguments(name, arguments, binding)


def test_resource_result_validator_accepts_canonical_real_runtime_measurement():
    from scientist.resource_recipe import canonical_resource_result, validate_resource_result

    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        binding, _, _, _ = _authority(Path(directory))
        result = canonical_resource_result(
            get_available_resources_recipe(WorkerResources(2, 1.5, 1024, 512)),
            profile_id=binding.profile_id,
            instruction_fingerprint=binding.instruction_fingerprint,
        )
        assert validate_resource_result(
            result,
            profile_id=binding.profile_id,
            instruction_fingerprint=binding.instruction_fingerprint,
            max_bytes=binding.max_result_bytes,
        )


@pytest.mark.parametrize("measurement", [
    {"cpu_count": True, "cpu_quota_cores": 1.0, "memory_limit_bytes": 1024,
     "memory_current_bytes": 1, "gpu_validation": False},
    {"cpu_count": 1, "cpu_quota_cores": 1.0, "memory_limit_bytes": 1,
     "memory_current_bytes": 2, "gpu_validation": False},
])
def test_resource_result_validator_rejects_semantically_invalid_measurement(measurement: dict):
    from scientist.resource_recipe import validate_resource_result

    envelope = {
        "schema_version": 1,
        "recipe_id": "get-available-resources",
        "profile_id": "prof.worker-base@py3.14.7",
        "instruction_fingerprint": "b" * 64,
        "measurement": measurement,
    }
    with pytest.raises(ValueError):
        validate_resource_result(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(),
            profile_id=envelope["profile_id"],
            instruction_fingerprint=envelope["instruction_fingerprint"],
            max_bytes=4096,
        )


def _native_dispatch_seam(calls=None, capability_ids=("get-available-resources",)):
    import scientist.runtime_adapter as runtime

    source = (ROOT / "backend/src/scientist/runtime_adapter.py").read_text()
    build = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "build_native_agent"
    )
    functions = {
        node.name: node for node in ast.walk(build)
        if isinstance(node, ast.FunctionDef)
    }
    calls = calls or [
        ("instruction_view", {"capability_id": "get-available-resources"}),
        ("scientific_resources", {}),
    ]
    raw_calls = [
        {"id": f"raw-{index}", "function": {"name": name, "arguments": json.dumps(args)}}
        for index, (name, args) in enumerate(calls, start=1)
    ]
    adapter = SimpleNamespace(
        _native_active_tool_calls={"applied-1": "raw-1", "applied-2": "raw-2"},
        _native_consumed_tool_calls=set(),
        _native_scientific_outputs={},
        context=SimpleNamespace(
            pending_assistant=SimpleNamespace(message_index=0, next_tool_index=0),
            messages=[SimpleNamespace(tool_calls=raw_calls)],
        ),
    )
    binding = SimpleNamespace(capability_ids=list(capability_ids))
    adapter._native_dispatch_agent = SimpleNamespace(_scientific_binding=binding)
    forwarded = []
    namespace = vars(runtime).copy()

    def resolve_call(_agent, call):
        function = call["function"]
        name = function["name"]
        args = function["arguments"]
        if isinstance(args, str):
            args = json.loads(args)
        # Production canonicalizes the legacy alias before invoking Hermes'
        # _unwrap_tool_search_call; direct names reach that helper unchanged.
        if name == "todo":
            name = "todo_list"
        if name == "tool_call":
            name, args = args["name"], args["arguments"]
        if name not in {"todo_list", "todo", "instruction_view", "scientific_resources"}:
            raise runtime.RuntimeAdapterError("outside test dispatch surface")
        if name in runtime._REVIEWED_NATIVE_SCIENTIFIC_TOOL_NAMES:
            runtime._validate_native_tool_arguments(name, args, binding)
        return name, args

    def dispatch(*args, **kwargs):
        forwarded.append((runtime._NATIVE_TOOL_CALL_ID.get(), args, kwargs))
        name = kwargs.get("function_name", args[0] if args else None)
        arguments = kwargs.get("function_args", args[1] if len(args) > 1 else None)
        if name == "instruction_view":
            return namespace["instruction_view_handler"](arguments)
        if name == "scientific_resources":
            namespace["require_active_call"]("scientific_resources", arguments)
            return "synthetic resource result"
        return None

    namespace.update(
        adapter=adapter,
        binding=binding,
        agent=SimpleNamespace(_scientific_binding=binding),
        instruction_bundle=SimpleNamespace(text="synthetic approved instruction"),
        _validate_native_todo_call=resolve_call,
        original_handle_function_call=dispatch,
    )
    for name in (
        "require_active_call",
        "instruction_view_handler",
        "scientific_resources_handler",
        "native_tool_call_context",
    ):
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[functions[name]], type_ignores=[])),
                "backend/src/scientist/runtime_adapter.py",
                "exec",
            ),
            namespace,
        )
    return runtime, namespace, forwarded


def test_saved_todo_alias_matches_hermes_canonical_dispatch_name():
    runtime, seam, forwarded = _native_dispatch_seam(
        calls=[("todo", {"todos": []}), ("todo_list", {"todos": []})],
        capability_ids=(),
    )
    seam["binding"] = None
    seam["native_tool_call_context"](
        function_name="todo_list",
        function_args={"todos": []},
        task_id="synthetic-task",
        tool_call_id="applied-1",
    )
    assert len(forwarded) == 1
    assert runtime._NATIVE_TOOL_CALL_ID.get() is None


def test_native_todo_validator_canonicalizes_only_the_legacy_alias(monkeypatch):
    import sys
    from types import ModuleType
    from scientist.runtime_adapter import _validate_native_todo_call

    agent_module = ModuleType("agent")
    agent_module.__path__ = []
    executor_module = ModuleType("agent.tool_executor")
    executor_module._unwrap_tool_search_call = lambda _agent, name, arguments: (
        name,
        arguments,
        None,
    )
    monkeypatch.setitem(sys.modules, "agent", agent_module)
    monkeypatch.setitem(sys.modules, "agent.tool_executor", executor_module)
    agent = SimpleNamespace(_scientific_binding=None)
    assert _validate_native_todo_call(
        agent,
        {"function": {"name": "todo", "arguments": "{\"todos\": []}"}},
    ) == ("todo_list", {"todos": []})


def test_native_instruction_dispatch_accepts_issued_identity_and_resets_context():
    runtime, seam, forwarded = _native_dispatch_seam()
    seam["native_tool_call_context"](
        "instruction_view", {"capability_id": "get-available-resources"}, tool_call_id="applied-1"
    )
    assert len(forwarded) == 1
    assert runtime._NATIVE_TOOL_CALL_ID.get() is None


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("terminal", {"command": "synthetic"}),
        ("direct_fetch", {"url": "https://example.invalid"}),
        ("tool_call", {"name": "terminal", "arguments": {"command": "synthetic"}}),
        ("rpc_call", {"method": "tools/call", "name": "terminal"}),
    ],
)
def test_native_dispatch_rejects_unissued_or_unreviewed_calls(name: str, arguments: dict):
    runtime, seam, forwarded = _native_dispatch_seam()
    with pytest.raises(runtime.RuntimeAdapterError):
        seam["native_tool_call_context"](name, arguments, tool_call_id="never-issued")
    assert forwarded == []


def test_native_resource_dispatch_accepts_issued_identity():
    runtime, seam, forwarded = _native_dispatch_seam()
    assert seam["native_tool_call_context"]("scientific_resources", {}, tool_call_id="applied-2")
    assert len(forwarded) == 1
    assert runtime._NATIVE_TOOL_CALL_ID.get() is None


def test_native_dispatch_rejects_issued_identity_with_a_different_tool_name():
    runtime, seam, forwarded = _native_dispatch_seam()
    with pytest.raises(runtime.RuntimeAdapterError):
        seam["native_tool_call_context"]("scientific_resources", {}, tool_call_id="applied-1")
    assert forwarded == []


def test_native_dispatch_rejects_schema_valid_but_different_arguments():
    runtime, seam, forwarded = _native_dispatch_seam(
        capability_ids=("get-available-resources", "another-approved-capability")
    )
    with pytest.raises(runtime.RuntimeAdapterError):
        seam["native_tool_call_context"](
            "instruction_view",
            {"capability_id": "another-approved-capability"},
            tool_call_id="applied-1",
        )
    assert forwarded == []


def test_native_dispatch_consumes_an_issued_id_only_once():
    runtime, seam, forwarded = _native_dispatch_seam()
    arguments = {"capability_id": "get-available-resources"}
    seam["native_tool_call_context"]("instruction_view", arguments, tool_call_id="applied-1")
    with pytest.raises(runtime.RuntimeAdapterError):
        seam["native_tool_call_context"]("instruction_view", arguments, tool_call_id="applied-1")
    assert len(forwarded) == 1


def test_native_scientific_handler_rechecks_exact_arguments_after_middleware():
    runtime, seam, _forwarded = _native_dispatch_seam(
        capability_ids=("get-available-resources", "another-approved-capability")
    )
    seam["original_handle_function_call"] = lambda *args, **kwargs: seam[
        "instruction_view_handler"
    ]({"capability_id": "another-approved-capability"})
    with pytest.raises(runtime.RuntimeAdapterError):
        seam["native_tool_call_context"](
            "instruction_view",
            {"capability_id": "get-available-resources"},
            tool_call_id="applied-1",
        )
    assert runtime._NATIVE_TOOL_CALL_ID.get() is None


def test_legacy_todo_dispatch_rejects_schema_valid_argument_replacement():
    runtime, seam, forwarded = _native_dispatch_seam(
        calls=[
            ("todo_list", {"todos": [{"id": "1", "content": "approved", "status": "pending"}]}),
            ("todo_list", {"todos": []}),
        ],
        capability_ids=(),
    )
    seam["binding"] = None
    with pytest.raises(runtime.RuntimeAdapterError):
        seam["native_tool_call_context"](
            "todo_list",
            {"todos": [{"id": "1", "content": "changed but valid", "status": "completed"}]},
            task_id="synthetic-task",
            tool_call_id="applied-1",
        )
    assert forwarded == []


def test_fully_keyword_todo_call_uses_factory_agent_not_task_id_as_agent():
    _, seam, forwarded = _native_dispatch_seam(
        calls=[("todo_list", {"todos": []}), ("todo_list", {"todos": []})],
        capability_ids=(),
    )
    seam["binding"] = None
    seam["native_tool_call_context"](
        function_name="todo_list",
        function_args={"todos": []},
        task_id="synthetic-task",
        tool_call_id="applied-1",
    )
    assert len(forwarded) == 1


def test_native_dispatch_context_cleans_up_after_underlying_failure():
    runtime, seam, _forwarded = _native_dispatch_seam()

    def fail(*args, **kwargs):
        raise ValueError("synthetic dispatch failure")

    seam["original_handle_function_call"] = fail
    with pytest.raises(ValueError, match="synthetic dispatch failure"):
        seam["native_tool_call_context"](
            "instruction_view", {"capability_id": "get-available-resources"}, tool_call_id="applied-1"
        )
    assert runtime._NATIVE_TOOL_CALL_ID.get() is None


def test_scientific_to_legacy_factory_rebind_keeps_current_adapter_scope():
    runtime, seam, _forwarded = _native_dispatch_seam(
        calls=[("todo_list", {"todos": []}), ("scientific_resources", {})]
    )
    build = next(
        node for node in ast.parse(
            (ROOT / "backend/src/scientist/runtime_adapter.py").read_text()
        ).body
        if isinstance(node, ast.FunctionDef) and node.name == "build_native_agent"
    )
    parent_by_node = {
        child: parent for parent in ast.walk(build) for child in ast.iter_child_nodes(parent)
    }
    wrapper = next(
        node for node in ast.walk(build)
        if isinstance(node, ast.FunctionDef) and node.name == "native_tool_call_context"
    )
    installer = parent_by_node[wrapper]
    model_calls = []
    model_tools = SimpleNamespace(
        handle_function_call=lambda *args, **kwargs: model_calls.append((args, kwargs))
    )

    def install(adapter, binding):
        original = model_tools.handle_function_call
        if getattr(original, "_scientist_native_context", False):
            original = original._scientist_original_dispatcher
        environment = vars(runtime).copy()
        environment.update(
            adapter=adapter,
            binding=binding,
            agent=SimpleNamespace(_scientific_binding=binding),
            model_tools=model_tools,
            original_handle_function_call=original,
            _validate_native_todo_call=lambda _agent, call: (
                call["function"]["name"],
                json.loads(call["function"]["arguments"])
                if isinstance(call["function"]["arguments"], str)
                else call["function"]["arguments"],
            ),
        )
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[installer], type_ignores=[])),
                "backend/src/scientist/runtime_adapter.py",
                "exec",
            ),
            environment,
        )

    install(seam["adapter"], seam["binding"])
    legacy_adapter = SimpleNamespace(
        _native_active_tool_calls={"legacy-1": "raw-1"},
        _native_consumed_tool_calls=set(),
        _native_dispatch_agent=SimpleNamespace(_scientific_binding=None),
        context=seam["adapter"].context,
    )
    install(legacy_adapter, None)
    model_tools.handle_function_call(
        "todo_list", {"todos": []}, "synthetic-task", tool_call_id="legacy-1"
    )
    assert len(model_calls) == 1
    with pytest.raises(runtime.RuntimeAdapterError):
        model_tools.handle_function_call(
            "todo_list", {"todos": []}, "synthetic-task", tool_call_id="applied-1"
        )
    with pytest.raises(runtime.RuntimeAdapterError):
        model_tools.handle_function_call(
            "scientific_resources", {}, "synthetic-task", tool_call_id="legacy-1"
        )
    assert runtime._NATIVE_TOOL_CALL_ID.get() is None


def test_i2_native_schemas_expose_only_approved_ids_and_closed_arguments():
    from scientist.runtime_adapter import _native_scientific_tool_definitions
    from scientist.contracts import (
        ComputeProfilePin,
        CsvDescribeGrantV1,
        CrossrefQueryV1,
        ObjectRef,
        RuntimePins,
        ScientificBindingV2,
    )

    project_id = uuid4()
    digest = "b" * 64
    grant = CsvDescribeGrantV1(
        recipe_id="csv.describe.v1",
        recipe_version="1",
        recipe_manifest_sha256="f" * 64,
        profile_id="prof.csv-stdlib@py3.14.7",
        profile_version="1",
        image_digest="sha256:" + "a" * 64,
        input_ref=ObjectRef(
            project_id=project_id,
            key=str(project_id) + "/" + digest,
            sha256=digest,
            size=100,
            content_type="application/octet-stream",
        ),
        input_sha256=digest,
        numeric_columns=["x"],
    )
    binding = ScientificBindingV2(
        binding_version=2,
        catalog_commit="154988403bb5a18e9d3c0ce4e6d5e2e4b184a298",
        registry_sha256="a" * 64,
        capability_ids=["paper-lookup", "exploratory-data-analysis"],
        instruction_fingerprint="e" * 64,
        agent_runtime_pins=RuntimePins(
            image_digest="sha256:" + "c" * 64,
            skills_digest="d" * 64,
            environment_digest="9" * 64,
        ),
        input_snapshot_digest="8" * 64,
        approved_crossref_queries={
            "search-a": CrossrefQueryV1(
                source_id="crossref", version=1, access_mode="public_read",
                query="first", doi=None, limit=2,
            ),
            "search-b": CrossrefQueryV1(
                source_id="crossref", version=1, access_mode="public_read",
                query="second", doi=None, limit=2,
            ),
        },
        required_compute_profiles=[ComputeProfilePin(
            profile_id=grant.profile_id,
            version=grant.profile_version,
            image_digest=grant.image_digest,
        )],
        csv_describe_grants={"grant-a": grant},
    )
    definitions = {item["name"]: item for item in _native_scientific_tool_definitions(binding)}

    assert definitions["scientific_search"]["parameters"] == {
        "type": "object",
        "properties": {"request_id": {"type": "string", "enum": ["search-a", "search-b"]}},
        "required": ["request_id"],
        "additionalProperties": False,
    }
    assert definitions["scientific_csv_describe"]["parameters"] == {
        "type": "object",
        "properties": {"grant_id": {"type": "string", "enum": ["grant-a"]}},
        "required": ["grant_id"],
        "additionalProperties": False,
    }


def test_native_builder_registers_scientific_schema_closures_on_todo_surface():
    source_path = Path(__file__).resolve().parents[1] / "src" / "scientist" / "runtime_adapter.py"
    module = ast.parse(source_path.read_text())
    builder = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_native_agent"
    )
    handler_maps = [
        node.value
        for node in ast.walk(builder)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "handlers" for target in node.targets)
        and isinstance(node.value, ast.Dict)
    ]
    registered_names = {
        key.value
        for handler_map in handler_maps
        for key in handler_map.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert {"scientific_search", "scientific_csv_describe"} <= registered_names
    schema_loop = any(
        isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and node.target.id == "schema"
        and isinstance(node.iter, ast.Call)
        and isinstance(node.iter.func, ast.Name)
        and node.iter.func.id == "_native_scientific_tool_definitions"
        for node in ast.walk(builder)
    )
    assert schema_loop
    registrations = [
        node for node in ast.walk(builder)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "register"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "registry"
    ]
    assert any(
        {keyword.arg for keyword in call.keywords} >= {"schema", "handler"}
        and any(keyword.arg == "schema" and isinstance(keyword.value, ast.Name) and keyword.value.id == "schema" for keyword in call.keywords)
        and any(keyword.arg == "handler" and isinstance(keyword.value, ast.Name) and keyword.value.id == "handler" for keyword in call.keywords)
        for call in registrations
    )
