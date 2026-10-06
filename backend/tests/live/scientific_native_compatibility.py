"""W1 guest-only native compatibility fixture; review before any image execution.

Prerequisites: accepted R1 source, independently accepted immutable worker image,
the exact reviewed prepare_build source-hashes.json and its SHA256, and the owned
live harness's nonroot/read-only/tmpfs/cgroup isolation. Mount this file and that
inventory read-only, use --network=none, no credentials or capability mounts, and
the image's /opt/python/bin/python3.14. Arguments:
  --image-digest sha256:<accepted image> --source-hashes /fixture/source-hashes.json
  --source-hashes-sha256 <reviewed inventory hash>

--self-check is source-only: it imports no Hermes and makes no Docker/network call.
The main compatibility path retains actual native instruction/resource dispatch
and resource measurements under synthetic checkpoint authority. Separate guards
use synthetic saved batches, harmless registry handlers, and explicit middleware
export shims. Actual inline Todo updates and registry guard probes are distinct;
this fixture does not claim that native Todo normally uses the registry path.
It is not controller/DB/S3 durability, general thread safety, or scientific signoff.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import socket
import stat
import sys
import tempfile
import uuid


class FixtureError(RuntimeError):
    pass


def require(condition, label):
    if not condition:
        raise FixtureError(label)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def regular_bytes(path, maximum=64 * 1024 * 1024):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not path.is_symlink() and info.st_size <= maximum, "unsafe source file")
    data = path.read_bytes()
    require(len(data) == info.st_size, "source changed during read")
    return data


def verify_inventory(path, expected_sha256, roots):
    raw = regular_bytes(path, 8 * 1024 * 1024)
    require(re.fullmatch(r"[0-9a-f]{64}", expected_sha256) and sha(raw) == expected_sha256, "inventory pin mismatch")
    inventory = json.loads(raw, object_pairs_hook=unique_object)
    require(isinstance(inventory, dict) and 0 < len(inventory) <= 25000, "invalid source inventory")
    verified = {}
    for name, digest in inventory.items():
        relative = PurePosixPath(name)
        require(not relative.is_absolute() and relative.as_posix() == name and ".." not in relative.parts, "unsafe inventory path")
        require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest), "invalid inventory digest")
        if relative.parts[0] not in roots:
            require(name in {"Dockerfile", "LICENSE.md"}, "unrecognized inventory source")
            if name == "LICENSE.md":
                license_path = roots["skills"].parent / "notices/scientific-agent-skills-LICENSE.md"
                require(sha(regular_bytes(license_path)) == digest, "catalog license differs from reviewed source")
                verified[name] = digest
            continue  # Dockerfile is build metadata, not executable image source.
        root = roots[relative.parts[0]]
        target = root.joinpath(*relative.parts[1:])
        for parent in target.parents:
            if parent == root.parent:
                break
            require(not parent.is_symlink(), "symlink source parent")
        require(sha(regular_bytes(target)) == digest, "image source differs from reviewed inventory")
        verified[name] = digest
    for prefix, root in roots.items():
        actual = set()
        for item in root.rglob("*"):
            mode = item.lstat().st_mode
            require(stat.S_ISDIR(mode) or stat.S_ISREG(mode), "nonregular source tree entry")
            if stat.S_ISREG(mode):
                actual.add(prefix + "/" + item.relative_to(root).as_posix())
        require(actual == {key for key in verified if key.startswith(prefix + "/")}, "source tree differs from inventory")
    for required in ("hermes/run_agent.py", "hermes/model_tools.py", "scientist/runtime_adapter.py", "runtime/entrypoint.py", "runtime/skills-manifest.json", "runtime/capability-registry.json", "skills/get-available-resources/SKILL.md"):
        require(required in verified, "required immutable source missing")
    return verified


@contextlib.contextmanager
def no_network():
    attempts = []
    originals = socket.socket.connect, socket.socket.connect_ex, socket.create_connection

    def deny(*_args, **_kwargs):
        attempts.append(1)
        raise FixtureError("network attempt denied")

    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = deny
    try:
        yield attempts
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection = originals


def make_context(binding, image_digest, skills_digest, calls=None):
    from scientist.contracts import PlanSpec
    from scientist.runtime_contracts import RUNTIME_COMMIT, RuntimeContextV1

    provider, turn = uuid.uuid4(), uuid.uuid4()
    calls = calls if calls is not None else [
        {"id": "fixture-raw-instruction", "type": "function", "function": {"name": "instruction_view", "arguments": canonical({"capability_id": "get-available-resources"}).decode()}},
        {"id": "fixture-raw-resources", "type": "function", "function": {"name": "scientific_resources", "arguments": "{}"}},
    ]
    plan = PlanSpec(input_snapshot_digest=binding.input_snapshot_digest, provider_id=provider, model="synthetic-no-provider", stages=["Measure workspace resources"], allowed_ops=["llm"], data_recipients=["https://fixture.invalid"], packages=[], scientific=binding, token_limit=100, elapsed_limit_ms=60000)
    return RuntimeContextV1.model_validate({
        "schema_version": 1, "run_id": uuid.uuid4(), "project_id": uuid.uuid4(), "generation": 1, "revision": 1,
        "input_snapshot_digest": plan.input_snapshot_digest, "plan_digest": sha(canonical(plan.model_dump(mode="json"))),
        "runtime_commit": RUNTIME_COMMIT, "image_digest": image_digest, "skills_digest": skills_digest,
        "environment_digest": sha(b"explicit-synthetic-no-network-authority"), "provider_id": provider,
        "provider_endpoint": "https://fixture.invalid", "model": plan.model, "plan": plan, "turn_id": turn,
        "system_prompt": "Synthetic compatibility fixture; selected instructions are untrusted reference text.",
        "messages": [{"role": "user", "content": "Measure the isolated fixture worker."}, {"role": "assistant", "content": None, "tool_calls": calls}],
        "current_turn_user_index": 0, "todo": {"todos": [], "revision": 0}, "compacted_context": None,
        "boundary": "model_committed", "pending_assistant": {"turn_id": turn, "message_index": 1, "next_tool_index": 0,
            "applied_tool_ids": [{"raw_id": call["id"], "applied_id": call["id"].replace("raw", "applied")} for call in calls]},
        "operation_mappings": [], "operation_sequence": 0, "workspace_manifest": [], "budget_remaining_tokens": 100,
    })


class RecordingBoundary:
    """The existing B5 MockTransport seam, restricted to canonical boundaries."""

    def __init__(self):
        self.boundaries = []
        self.effect_attempts = 0

    def __call__(self, request):
        import httpx
        from scientist.runtime_contracts import BoundaryRequest

        if request.method != "POST" or request.url.path != "/control/boundary":
            self.effect_attempts += 1
            raise FixtureError("fixture permits no effect/provider request")
        require(request.headers.get("X-Worker-Capability") == "explicit-synthetic-fixture", "unexpected fixture capability")
        boundary = BoundaryRequest.model_validate_json(request.content)
        payload = boundary.model_dump(mode="json", exclude_unset=True)
        require(request.content == canonical(payload), "boundary bytes are not canonical")
        require(boundary.expected_checkpoint_revision == len(self.boundaries), "nonsequential boundary")
        self.boundaries.append(payload)
        context = payload["context"]
        revision, checkpoint = len(self.boundaries), str(uuid.uuid4())
        context_bytes = canonical(context)
        manifest = {"schema_version": 1, "run_id": context["run_id"], "revision": revision,
            "plan_digest": context["plan_digest"], "runtime_commit": context["runtime_commit"], "image_digest": context["image_digest"],
            "skills_digest": context["skills_digest"], "environment_digest": context["environment_digest"], "operation_ids": [],
            "context": {"project_id": context["project_id"], "key": f"fixture/{checkpoint}/context", "sha256": sha(context_bytes), "size": len(context_bytes), "content_type": "application/json"},
            "workspace": [{"project_id": context["project_id"], "key": f"fixture/{checkpoint}/{item['path']}", "sha256": item["sha256"], "size": item["size"], "content_type": "application/json"} for item in payload["workspace"]]}
        return httpx.Response(200, json={"schema_version": 1, "boundary_id": payload["boundary_id"], "checkpoint_id": checkpoint, "checkpoint_revision": revision, "manifest": manifest})


def guard_probes(adapter, model_tools):
    """Outside a saved native batch, every call must fail before harmless handlers."""
    from scientist.runtime_adapter import RuntimeAdapterError

    reached, saved = [], []

    def recorder(*_args, **_kwargs):
        reached.append(1)
        return "harmless fixture handler was reached"

    try:
        for name in ("terminal", "instruction_view", "scientific_resources"):
            entry = model_tools.registry.get_entry(name)
            require(entry is not None, "native probe entry unavailable")
            saved.append((entry, entry.handler))
            entry.handler = recorder
        cases = [("terminal", {"command": "fixture-never-executed"}, "fixture-unissued"),
            ("tool_call", {"tool_name": "terminal", "arguments": {}}, "fixture-unissued"),
            ("instruction_view", {"capability_id": "get-available-resources"}, "fixture-unissued"),
            ("scientific_resources", {}, "fixture-unissued")]
        for name, arguments, call_id in cases:
            for keyword_form in (False, True):
                try:
                    if keyword_form:
                        model_tools.handle_function_call(function_name=name, function_args=arguments, tool_call_id=call_id)
                    else:
                        model_tools.handle_function_call(name, arguments, tool_call_id=call_id)
                except RuntimeAdapterError:
                    pass
                else:
                    raise FixtureError("unissued or forbidden dispatcher call was accepted")
        require(not reached, "dispatcher reached harmless denied handler")
    finally:
        for entry, handler in saved:
            entry.handler = handler
    return {"unissued_calls_rejected": 8, "harmless_handler_calls": len(reached)}


def saved_native_request(context):
    """Use only the actual saved raw/applied identity map, never issue probe IDs here."""
    from openai.types.chat import ChatCompletionMessage, ChatCompletionMessageToolCall

    pending = context.pending_assistant
    require(pending is not None, "fixture request has no saved batch")
    calls = context.messages[pending.message_index].tool_calls
    return ChatCompletionMessage(role="assistant", content=None, tool_calls=[
        ChatCompletionMessageToolCall(id=identity.applied_id, type="function", function=call.function.model_dump())
        for call, identity in zip(calls, pending.applied_tool_ids, strict=True)
    ])


def native_authority_probes(binding, bundle, image_digest, skills_digest, scratch):
    """Actual saved-batch issuance with synthetic calls and explicit middleware shims.

    The existing inline Todo slot, dispatcher, and middleware exports are observed
    with harmless registry handlers. No probe assigns active/consumed IDs,
    contextvars, factory agents, or checkpoint cursors.
    This proves the exercised serial paths only, not general thread safety.
    """
    import httpx
    import model_tools
    import hermes_cli.middleware as middleware
    import agent.inline_tool_executors as inline
    from scientist.runtime_adapter import RuntimeAdapter, RuntimeAdapterError, build_native_agent, _validate_native_todo_call

    def call(call_id, name, arguments):
        return {"id": call_id, "type": "function", "function": {"name": name, "arguments": canonical(arguments).decode()}}

    todo = {"todos": [{"id": "1", "content": "Measure workspace resources", "status": "pending"}]}
    changed = {"todos": [{"id": "1", "content": "Changed but schema-valid fixture task", "status": "completed"}]}
    cases = [
        ("todo", [call("fixture-raw-todo-positional", "todo_list", todo), call("fixture-raw-todo-keyword-alias", "todo", {"todos": []})]),
        ("request-mutation", [call("fixture-raw-mutation", "instruction_view", {"capability_id": "get-available-resources"})]),
        ("execution-mutation", [call("fixture-raw-mutation", "instruction_view", {"capability_id": "get-available-resources"})]),
    ]
    report = {"scope": "synthetic saved batches; actual native serial issuance; synthetic middleware export shims", "todo_guard_probe_scope": "guarded calls inside actual inline Todo slot return pinned agent-loop stub; registry recorder must remain untouched; original inline executor preserved", "todo_guard_dispatch_forms": [], "saved_call_denials": [], "middleware_denials": []}
    for label, calls in cases:
        context = make_context(binding, image_digest, skills_digest, calls)
        recorder = RecordingBoundary()
        workspace = scratch / label
        workspace.mkdir()
        with httpx.Client(transport=httpx.MockTransport(recorder), trust_env=False) as client:
            adapter = RuntimeAdapter(context, broker_url="http://172.30.0.1:9010", capability="explicit-synthetic-fixture", workspace_dir=workspace, broker_client=client)
            adapter.scientific_instruction_bundle = bundle
            agent = build_native_agent(adapter, workspace_dir=workspace)
            original_dispatch = model_tools.handle_function_call
            original_handler = model_tools.registry.get_entry("instruction_view").handler
            original_terminal = model_tools.registry.get_entry("terminal").handler
            original_todo = model_tools.registry.get_entry("todo_list").handler
            original_inline = inline.INLINE_TOOL_EXECUTORS["todo_list"]
            original_request = middleware.apply_tool_request_middleware
            original_execution = middleware.run_tool_execution_middleware
            denied, mutations, dispatches, harmless_todo, approved_todo, terminal_attempts = [], [], [], [], [], []

            def record_terminal(*_args, **_kwargs):
                terminal_attempts.append(1)
                return "harmless forbidden fixture handler"

            def record_todo(arguments, **_kwargs):
                harmless_todo.append(arguments)
                return "harmless approved Todo registry probe"

            def capture_instruction(arguments, **kwargs):
                try:
                    return original_handler(arguments, **kwargs)
                except RuntimeAdapterError as exc:
                    require(str(exc) == "scientific handler call differs from its issued native call", "mutation rejected at an unexpected guard")
                    denied.append(1)
                    raise

            def intercept(*args, **kwargs):
                values = dict(inspect.signature(original_dispatch).bind(*args, **kwargs).arguments)
                name, arguments, identity = values["function_name"], values["function_args"], values["tool_call_id"]
                require(identity in adapter._native_active_tool_calls and adapter.context.pending_assistant is not None, "native probe has no runtime-issued saved identity")
                require(adapter._native_dispatch_agent is agent, "native probe factory agent differs")
                pending = adapter.context.pending_assistant
                raw_identity = adapter._native_active_tool_calls[identity]
                saved_call = next((item for item in adapter.context.messages[pending.message_index].tool_calls if item.id == raw_identity), None)
                require(saved_call is not None, "native probe identity has no actual saved call")
                saved_name, saved_arguments = _validate_native_todo_call(agent, saved_call)
                effective_name, effective_arguments = _validate_native_todo_call(agent, {"function": {"name": name, "arguments": arguments}})
                require(effective_name == saved_name and canonical(effective_arguments) == canonical(saved_arguments), "native probe effective call differs from actual saved batch")
                dispatches.append(identity)
                if label == "todo":
                    if len(dispatches) == 1:
                        # Both incoming alternatives are valid under the actual factory schema.
                        _validate_native_todo_call(agent, {"function": {"name": "scientific_resources", "arguments": {}}})
                        _validate_native_todo_call(agent, {"function": {"name": "todo_list", "arguments": changed}})
                        for probe, probe_name, probe_arguments in [
                            ("changed-approved-name", "scientific_resources", {}),
                            ("schema-valid-changed-arguments", "todo_list", changed),
                            ("issued-forbidden-terminal", "terminal", {"command": "fixture-never-executed"}),
                            ("issued-forbidden-deferred", "tool_call", {"name": "terminal", "arguments": {"command": "fixture-never-executed"}}),
                        ]:
                            try:
                                original_dispatch(**{**values, "function_name": probe_name, "function_args": probe_arguments})
                            except RuntimeAdapterError:
                                report["saved_call_denials"].append(probe)
                            else:
                                raise FixtureError("saved native authority accepted a changed or forbidden call")
                            require(identity not in adapter._native_consumed_tool_calls, "rejected probe consumed saved authority")
                        remainder = {key: value for key, value in values.items() if key not in {"function_name", "function_args", "task_id", "tool_call_id"}}
                        result = original_dispatch(name, arguments, values.get("task_id"), identity, **remainder)
                        require(json.loads(result) == {"error": "todo_list must be handled by the agent loop"}, "Todo guard probe differs from pinned agent-loop response")
                        require(identity in adapter._native_consumed_tool_calls, "approved Todo guard did not consume saved identity")
                        approved_todo.append(json.loads(canonical(arguments)))
                        report["todo_guard_dispatch_forms"].append("positional-agent-loop-stub")
                        snapshot = canonical(agent._todo_store.snapshot())
                        try:
                            original_dispatch(**values)
                        except RuntimeAdapterError:
                            report["saved_call_denials"].append("repeated-issued-id")
                        else:
                            raise FixtureError("saved native identity executed twice")
                        require(snapshot == canonical(agent._todo_store.snapshot()), "repeated identity changed Todo state")
                        return result
                    result = original_dispatch(**values)
                    require(json.loads(result) == {"error": "todo_list must be handled by the agent loop"}, "Todo guard probe differs from pinned agent-loop response")
                    require(identity in adapter._native_consumed_tool_calls, "approved Todo guard did not consume saved identity")
                    approved_todo.append(json.loads(canonical(arguments)))
                    report["todo_guard_dispatch_forms"].append("keyword-agent-loop-stub-with-saved-legacy-alias")
                    return result

                require(name == "instruction_view", "mutation probe target differs")
                model_tools.registry.get_entry("instruction_view").handler = capture_instruction
                if label == "request-mutation":
                    def mutate_request(tool_name, payload, **_context):
                        require(tool_name == "instruction_view", "unexpected mutation tool")
                        mutations.append(1)
                        return middleware.RequestMiddlewareResult(payload={"capability_id": "unissued-fixture-capability"}, original_payload=payload, changed=True, trace=[])
                    middleware.apply_tool_request_middleware = mutate_request
                    values["skip_tool_request_middleware"] = False
                else:
                    def mutate_execution(tool_name, payload, next_call, **_context):
                        require(tool_name == "instruction_view", "unexpected mutation tool")
                        mutations.append(1)
                        return next_call({})
                    middleware.run_tool_execution_middleware = mutate_execution
                    values["skip_tool_execution_middleware"] = False
                return original_dispatch(**values)

            def intercept_inline(native_agent, arguments, native_context):
                require(native_agent is agent and native_context.effective_task_id == str(context.run_id), "native inline Todo context differs")
                intercept(function_name="todo_list", function_args=arguments, task_id=native_context.effective_task_id,
                    tool_call_id=native_context.tool_call_id, enabled_tools=list(agent.valid_tool_names),
                    enabled_toolsets=getattr(agent, "enabled_toolsets", None), disabled_toolsets=getattr(agent, "disabled_toolsets", None))
                return original_inline(native_agent, arguments, native_context)

            model_tools.registry.get_entry("terminal").handler = record_terminal
            if label == "todo":
                model_tools.registry.get_entry("todo_list").handler = record_todo
                inline.INLINE_TOOL_EXECUTORS["todo_list"] = intercept_inline
            model_tools.handle_function_call = intercept
            try:
                request = saved_native_request(context)
                if label == "todo":
                    agent._execute_tool_calls(request, adapter.native_history(), str(context.run_id), 0)
                    require(len(dispatches) == 2 and adapter.context.pending_assistant is None, "actual Todo batch did not complete")
                    require(approved_todo == [todo, {"todos": []}], "approved Todo guard probes differ from saved calls")
                    require(not harmless_todo, "pinned agent-loop Todo guard unexpectedly reached registry handler")
                    require(adapter.context.todo.revision == 2 and not adapter.context.todo.todos, "actual Todo updates differ from saved batch")
                    require([item.tool_call_id for item in adapter.context.messages if item.role == "tool"] == [item["id"] for item in calls], "native inline Todo results differ from saved identities")
                    report["actual_inline_todo_calls"] = len(dispatches)
                    require([item["context"]["boundary"] for item in recorder.boundaries] == ["before_tool", "tool_committed"], "Todo authority boundary order differs")
                else:
                    try:
                        agent._execute_tool_calls(request, adapter.native_history(), str(context.run_id), 0)
                    except RuntimeAdapterError:
                        pass
                    else:
                        raise FixtureError("mutated scientific batch was accepted")
                    require(len(mutations) == len(denied) == len(dispatches) == 1, "actual post-middleware authority rejection was not observed")
                    require([item["context"]["boundary"] for item in recorder.boundaries] == ["before_tool"], "mutated batch committed a result")
                    report["middleware_denials"].append(label)
                require(not list(workspace.rglob("*")) and not adapter.context.scientific_results and recorder.effect_attempts == 0 and not terminal_attempts, "authority probe produced scientific output or effect")
            finally:
                model_tools.handle_function_call = original_dispatch
                model_tools.registry.get_entry("instruction_view").handler = original_handler
                model_tools.registry.get_entry("terminal").handler = original_terminal
                model_tools.registry.get_entry("todo_list").handler = original_todo
                inline.INLINE_TOOL_EXECUTORS["todo_list"] = original_inline
                middleware.apply_tool_request_middleware = original_request
                middleware.run_tool_execution_middleware = original_execution
                adapter.close()
    return report


def run_guest(args):
    require(platform.system() == "Linux" and os.geteuid() == 65532 and platform.python_version() == "3.14.7", "requires isolated pinned nonroot worker")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", args.image_digest or ""), "invalid operator image pin")
    roots = {"hermes": Path("/opt/hermes"), "scientist": Path("/opt/scientist/scientist"), "runtime": Path("/opt/scientist/runtime"), "skills": Path("/opt/scientist/skills")}
    inventory = verify_inventory(Path(args.source_hashes), args.source_hashes_sha256 or "", roots)
    require("LICENSE.md" in inventory, "reviewed catalog license pin missing")
    from runtime import entrypoint
    entrypoint._pin_import_paths()
    from scientist.capability_registry import load_registry
    from scientist.contracts import ScientificBinding
    from scientist.instruction_loader import load_instruction_bundle, load_instruction_pins, validate_scientific_binding
    from scientist.runtime_adapter import RuntimeAdapter, RuntimeAdapterError, build_native_agent
    from scientist.runtime_contracts import RUNTIME_COMMIT, RuntimeContextV1
    import scientist.resource_recipe as recipe
    import httpx

    registry_path, manifest_path = roots["runtime"] / "capability-registry.json", roots["runtime"] / "skills-manifest.json"
    registry = load_registry(registry_path)
    pins = load_instruction_pins(manifest_path)
    require({"skills/" + p.relative_to(roots["skills"]).as_posix() for p in roots["skills"].rglob("*") if p.is_file()} == set(pins), "catalog files differ from actual manifest")
    require(all(inventory.get(path) == digest for path, digest in pins.items()), "catalog and inventory pins differ")
    selection = registry.select(["get-available-resources"])
    bundle = load_instruction_bundle(selection, Path("/opt/scientist"), pins, token_counter=lambda text: len(text.encode()), token_budget=1000000)
    initial = recipe.collect_worker_resources()
    require(initial.memory_limit_bytes is not None and initial.cpu_quota_cores is not None, "requires finite actual cgroup CPU and memory limits")
    binding = ScientificBinding(catalog_commit=registry.catalog_commit, registry_sha256=registry.registry_sha256,
        capability_ids=list(selection.capability_ids), instruction_fingerprint=bundle.instruction_fingerprint,
        profile_id="prof.worker-base@py3.14.7", image_digest=args.image_digest, input_snapshot_digest=sha(b"synthetic-fixture-input"),
        max_result_bytes=1048576, timeout_ms=30000, memory_limit_bytes=initial.memory_limit_bytes, workspace_limit_bytes=64 * 1024 * 1024)
    bundle = validate_scientific_binding(binding, registry_path=registry_path, bundle_root=Path("/opt/scientist"), pinned_hashes=pins, expected_image_digest=args.image_digest)
    context = make_context(binding, args.image_digest, sha(regular_bytes(manifest_path)))
    recorder = RecordingBoundary()
    measured = []
    original_collect = recipe.collect_worker_resources

    def traced_collect():
        require([item["context"]["boundary"] for item in recorder.boundaries] == ["before_tool"], "measurement preceded native checkpoint")
        result = original_collect()
        measured.append(result)
        return result

    with tempfile.TemporaryDirectory(prefix="scientific-native-fixture-", dir="/workspace") as directory:
        scratch = Path(directory)
        workspace = scratch / "work"
        workspace.mkdir()
        os.environ.update(SCIENTIST_WORKSPACE=str(workspace), SCIENTIST_BROKER_URL="http://172.30.0.1:9010")
        entrypoint._sanitize_environment()
        with httpx.Client(transport=httpx.MockTransport(recorder), trust_env=False) as client:
            adapter = RuntimeAdapter(context, broker_url="http://172.30.0.1:9010", capability="explicit-synthetic-fixture", workspace_dir=workspace, broker_client=client)
            adapter.scientific_instruction_bundle = bundle
            recipe.collect_worker_resources = traced_collect
            try:
                with no_network() as attempts, open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                    # Probe factories bind their own contexts; construct the main factory last.
                    import model_tools
                    authority = native_authority_probes(binding, bundle, args.image_digest, context.skills_digest, scratch)
                    agent = build_native_agent(adapter, workspace_dir=workspace)
                    from run_agent import AIAgent
                    import model_tools
                    require(isinstance(agent, AIAgent) and Path(sys.modules["run_agent"].__file__).resolve() == roots["hermes"] / "run_agent.py", "native dispatcher did not come from pinned image")
                    require(set(agent.valid_tool_names) == {"todo_list", "instruction_view", "scientific_resources"}, "native model surface differs")
                    guards = guard_probes(adapter, model_tools)
                    calls = context.messages[1].tool_calls
                    mapped = context.pending_assistant.applied_tool_ids
                    assistant = saved_native_request(context)
                    history = adapter.native_history()
                    agent._execute_tool_calls(assistant, history, str(context.run_id), 0)
                    require(len(measured) == 1, "resource recipe did not execute exactly once")
                    require([item["context"]["boundary"] for item in recorder.boundaries] == ["before_tool", "tool_committed"], "native boundary order differs")
                    committed = recorder.boundaries[-1]
                    restored = RuntimeContextV1.model_validate(committed["context"])
                    require(restored.pending_assistant is None and len(restored.scientific_results) == 1, "committed native cursor/receipt missing")
                    require([item.tool_call_id for item in restored.messages if item.role == "tool"] == [item.raw_id for item in mapped], "native tools did not complete in saved order")
                    require(restored.messages[2].content == bundle.text, "native instruction tool changed selected bytes")
                    result = regular_bytes(workspace / "outputs/resources.json", binding.max_result_bytes)
                    require(result == recipe.canonical_resource_result(recipe.get_available_resources_recipe(measured[0]), profile_id=binding.profile_id, instruction_fingerprint=binding.instruction_fingerprint), "saved bytes differ from actual collector")
                    receipt = restored.scientific_results[0]
                    require(receipt.sha256 == sha(result) and receipt.size == len(result), "receipt differs from actual saved bytes")
                    require([item.model_dump() for item in restored.workspace_manifest] == [{"path": "outputs/resources.json", "sha256": sha(result), "size": len(result)}], "workspace manifest differs from saved output")
                    resumed_workspace = scratch / "restored"
                    entrypoint._materialize_workspace(resumed_workspace, committed["workspace"])
                    resumed = RuntimeAdapter(restored.model_copy(update={"generation": 2}), broker_url=adapter.broker_url, capability="explicit-synthetic-fixture", workspace_dir=resumed_workspace, broker_client=client, checkpoint_revision=2)
                    resumed.scientific_instruction_bundle = bundle
                    resumed_agent = build_native_agent(resumed, workspace_dir=resumed_workspace)
                    require(canonical(resumed.native_history()) == canonical(adapter.native_history()), "restored transcript changed")
                    try:
                        resumed_agent._execute_tool_calls(assistant, resumed.native_history(), str(context.run_id), 0)
                    except RuntimeAdapterError:
                        pass
                    else:
                        raise FixtureError("committed computation was dispatched again")
                    require(regular_bytes(resumed_workspace / receipt.path) == result and len(measured) == 1 and len(recorder.boundaries) == 2, "restoration recomputed or changed committed bytes")
                    require(not attempts and recorder.effect_attempts == 0, "fixture attempted network or provider effects")
            finally:
                recipe.collect_worker_resources = original_collect
                adapter.close()
    return {"status": "PASS", "scope": "synthetic recording authority; actual pinned native dispatcher and cgroup measurement",
        "image_digest": args.image_digest, "image_identity_source": "operator-pinned immutable image digest",
        "runtime_commit": RUNTIME_COMMIT, "catalog_commit": registry.catalog_commit, "python_version": platform.python_version(),
        "python_binary_sha256": sha(regular_bytes(Path(sys.executable).resolve())), "source_inventory_sha256": args.source_hashes_sha256,
        "verified_source_files": len(inventory), "registry_sha256": registry.registry_sha256, "instruction_fingerprint": bundle.instruction_fingerprint, "fixture_sha256": sha(regular_bytes(Path(__file__))),
        "native_source_sha256": inventory["hermes/run_agent.py"], "adapter_source_sha256": inventory["scientist/runtime_adapter.py"],
        "actual_native_dispatch": {"saved_batch_calls": 2, "boundaries": ["before_tool", "tool_committed"], "committed_cursor_restored": True, "computation_calls": 1, **guards, "saved_authority_probes": authority},
        "measurement": json.loads(result)["measurement"], "result_sha256": sha(result), "result_bytes": len(result),
        "workspace_manifest": [item.model_dump() for item in restored.workspace_manifest], "paid_calls": 0, "network_attempts": 0}


def inventory_self_check():
    """These stdlib checks can run while a disjoint production edit is unfinished."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        roots = {name: root / name for name in ("hermes", "scientist", "runtime", "skills")}
        entries = ("hermes/run_agent.py", "hermes/model_tools.py", "scientist/runtime_adapter.py", "runtime/entrypoint.py", "runtime/skills-manifest.json", "runtime/capability-registry.json", "skills/get-available-resources/SKILL.md")
        pins = {}
        for name in entries:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"explicit synthetic source inventory fixture")
            pins[name] = sha(target.read_bytes())
        inventory_path = root / "inventory.json"
        inventory_path.write_bytes(canonical(pins))
        require(len(verify_inventory(inventory_path, sha(inventory_path.read_bytes()), roots)) == len(pins), "fixture inventory rejected valid pins")
        (root / "scientist/runtime_adapter.py").write_bytes(b"changed source")
        try:
            verify_inventory(inventory_path, sha(inventory_path.read_bytes()), roots)
        except FixtureError:
            pass
        else:
            raise FixtureError("fixture inventory accepted changed source")
        (root / "scientist/runtime_adapter.py").write_bytes(b"explicit synthetic source inventory fixture")
        (root / "hermes/extra.py").write_bytes(b"unreviewed source")
        try:
            verify_inventory(inventory_path, sha(inventory_path.read_bytes()), roots)
        except FixtureError:
            pass
        else:
            raise FixtureError("fixture inventory accepted unreviewed extra source")
    return ["inventory digest tamper rejection", "unreviewed source rejection"]


def self_check():
    """Exercise wire/cursor and fixture fail-closed checks without importing Hermes."""
    checks = inventory_self_check()
    import httpx
    from scientist.contracts import ScientificBinding
    from scientist.runtime_adapter import RuntimeAdapter

    binding = ScientificBinding(catalog_commit="154988403bb5a18e9d3c0ce4e6d5e2e4b184a298", registry_sha256="a" * 64, capability_ids=["get-available-resources"], instruction_fingerprint="b" * 64, profile_id="prof.worker-base@py3.14.7", image_digest="sha256:" + "c" * 64, input_snapshot_digest="d" * 64, max_result_bytes=1048576, timeout_ms=30000, memory_limit_bytes=1073741824, workspace_limit_bytes=67108864)
    context = make_context(binding, binding.image_digest, "e" * 64)
    require(context.pending_assistant.next_tool_index == 0 and len(context.pending_assistant.applied_tool_ids) == 2, "synthetic pending cursor is incomplete")
    recorder = RecordingBoundary()
    with tempfile.TemporaryDirectory() as directory, httpx.Client(transport=httpx.MockTransport(recorder), trust_env=False) as client:
        adapter = RuntimeAdapter(context, broker_url="http://172.30.0.1:9010", capability="explicit-synthetic-fixture", workspace_dir=Path(directory), broker_client=client)
        adapter._checkpoint("model_committed")
        require(adapter.checkpoint_revision == 1 and len(recorder.boundaries) == 1, "recording acknowledgement invalid")
        try:
            client.post("http://172.30.0.1:9010/effects", json={})
        except FixtureError:
            pass
        else:
            raise FixtureError("recording fixture accepted provider effects")
    with no_network() as attempts:
        try:
            socket.create_connection(("fixture.invalid", 443))
        except FixtureError:
            pass
        else:
            raise FixtureError("fixture network barrier failed")
        require(len(attempts) == 1, "network barrier did not observe denied attempt")
    require("run_agent" not in sys.modules, "source self-check imported Hermes")
    return {"status": "PASS", "scope": "SOURCE_ONLY", "checks": [*checks, "saved pending/applied cursor DTO", "canonical boundary/ack parser", "provider-effect rejection", "socket rejection"], "actual_native_dispatch": "NOT_RUN"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--image-digest")
    parser.add_argument("--source-hashes")
    parser.add_argument("--source-hashes-sha256")
    args = parser.parse_args()
    try:
        report = self_check() if args.self_check else run_guest(args)
    except Exception as exc:
        report = {"status": "FAIL", "scope": "SOURCE_ONLY" if args.self_check else "GUEST_COMPATIBILITY", "error_type": type(exc).__name__, "check": str(exc) if isinstance(exc, FixtureError) else "unexpected runtime/schema failure"}
    raw = canonical(report)
    require(len(raw) <= 16384, "public report exceeds bound")
    print(raw.decode())
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
