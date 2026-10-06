"""Synthetic provider and one-shot lost-boundary-ACK barrier for W1 live acceptance."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import socket
import sys
import time
from pathlib import Path

import uvicorn

_run = uvicorn.run


def _messages(request):
    payload = request.payload
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        raise RuntimeError("scientific fixture expected a model transcript")
    tool_messages = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
    ids = [m.get("tool_call_id") for m in tool_messages]
    if any(not isinstance(value, str) for value in ids) or len(ids) != len(set(ids)):
        raise RuntimeError("scientific fixture received an invalid tool transcript")
    return set(ids)


def _stage(request, target):
    if (request.kind != "llm" or target.url != "https://research.example" or
            request.payload.get("model") != "fixture"):
        raise RuntimeError("scientific fixture rejected an unexpected provider operation")
    ids = _messages(request)
    calls = [call for message in request.payload["messages"]
             if isinstance(message, dict) and message.get("role") == "assistant"
             for call in message.get("tool_calls", [])]
    if not ids and not calls:
        return "batch"
    expected = [("w1-instruction", "instruction_view", {"capability_id": "get-available-resources"}),
                ("w1-resources", "scientific_resources", {})]
    actual = []
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            raise RuntimeError("scientific fixture received an invalid assistant tool batch")
        try:
            arguments = function.get("arguments")
            arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        except (TypeError, json.JSONDecodeError):
            raise RuntimeError("scientific fixture received invalid tool arguments") from None
        actual.append((call.get("id"), function.get("name"), arguments))
    if ids == {"w1-instruction", "w1-resources"} and actual == expected:
        return "final"
    raise RuntimeError("scientific fixture rejected an unexpected provider transcript")


def _response(stage):
    if stage == "batch":
        message = {
            "role": "assistant", "content": None,
            "tool_calls": [
                {"id": "w1-instruction", "type": "function", "function": {
                    "name": "instruction_view", "arguments": '{"capability_id":"get-available-resources"}'}},
                {"id": "w1-resources", "type": "function", "function": {
                    "name": "scientific_resources", "arguments": "{}"}},
            ],
        }
        finish = "tool_calls"
    elif stage == "final":
        message = {"role": "assistant", "content": "Synthetic resource measurements recorded."}
        finish = "stop"
    else:
        raise RuntimeError("scientific fixture rejected an unexpected stage")
    return json.dumps({"id": "w1-completion", "object": "chat.completion", "created": 1,
                       "model": "fixture", "choices": [{"index": 0, "message": message,
                       "finish_reason": finish}],
                       "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
                      separators=(",", ":")).encode(), 2


def _hold_boundary(status, receipt_committed, first_for_run, targeted):
    return status == 200 and receipt_committed and first_for_run and targeted


_SAFE_ERROR_CODES = frozenset({
    "profile_configuration_unavailable",
    "scientific_binding_unavailable", "storage_unavailable", "forbidden", "invalid_request",
    "generation_conflict", "checkpoint_conflict", "context_mismatch", "run_inactive",
    "invalid_boundary", "expired",
})


def _error_code(status, response):
    if status == 422:
        return "validation_error"
    chunks = []
    remaining = 4096
    for message in response:
        if message.get("type") != "http.response.body":
            continue
        chunk = message.get("body", b"")
        if isinstance(chunk, bytes) and chunk:
            chunks.append(chunk[:remaining])
            remaining -= min(remaining, len(chunk))
            if not remaining:
                break
    body = b"".join(chunks)
    try:
        payload = json.loads(body)
        if isinstance(payload, dict):
            candidate = payload.get("code") if "code" in payload else payload.get("detail")
        else:
            candidate = None
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        candidate = None
    return candidate if isinstance(candidate, str) and candidate in _SAFE_ERROR_CODES else "other"


def _error_shape(response):
    chunks = []
    remaining = 4096
    for message in response:
        if message.get("type") != "http.response.body":
            continue
        chunk = message.get("body", b"")
        if isinstance(chunk, bytes) and chunk:
            chunks.append(chunk[:remaining])
            remaining -= min(remaining, len(chunk))
        if not remaining:
            break
    try:
        payload = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return "candidate_field=unavailable candidate_kind=invalid nested_code=none"
    if not isinstance(payload, dict):
        return "candidate_field=none candidate_kind=non_object nested_code=none"
    if "code" in payload:
        field = "code"
        candidate = payload["code"]
    elif "detail" in payload:
        field = "detail"
        candidate = payload["detail"]
    else:
        return "candidate_field=none candidate_kind=missing nested_code=none"
    if isinstance(candidate, dict):
        kind = "object"
        nested = candidate.get("code")
        nested_code = nested if isinstance(nested, str) and nested in _SAFE_ERROR_CODES else (
            "other" if isinstance(nested, str) else "none"
        )
    elif isinstance(candidate, str):
        kind, nested_code = "string", "none"
    elif isinstance(candidate, list):
        kind, nested_code = "array", "none"
    elif candidate is None:
        kind, nested_code = "null", "none"
    elif isinstance(candidate, bool):
        kind, nested_code = "boolean", "none"
    elif isinstance(candidate, (int, float)):
        kind, nested_code = "number", "none"
    else:
        kind, nested_code = "other", "none"
    return f"candidate_field={field} candidate_kind={kind} nested_code={nested_code}"


def _diagnose(status, code, response=None, *, controller_called=None, observed_domain_error=None):
    if type(status) is not int or not 100 <= status <= 599:
        status = 0
    if not isinstance(code, str) or (code not in _SAFE_ERROR_CODES and code not in ("validation_error", "other")):
        code = "other"
    line = f"scientific-fixture-response status={status} code={code}"
    if status >= 500 and code == "other":
        line += " " + _error_shape(response or [])
        if controller_called is not None:
            line += f" controller_called={'true' if controller_called else 'false'}"
        if observed_domain_error is not None:
            line += f" observed_domain_error={'true' if observed_domain_error else 'false'}"
    print(line, file=sys.stderr, flush=True)


def _synthetic_model(request, target):
    _stall_targeted_operation(request)
    tools = request.payload.get("tools") if isinstance(request.payload, dict) else None
    names = {
        item.get("function", {}).get("name")
        for item in tools or []
        if isinstance(item, dict) and isinstance(item.get("function"), dict)
    }
    if _W2_TOOL_NAMES <= names:
        stage, message, finish = _w2_stage(request, target)
        return _w2_response(request, stage, message, finish)
    stage = _stage(request, target)
    from sqlalchemy import text
    from scientist import db

    with db.session() as session:
        session.execute(text("""CREATE TABLE IF NOT EXISTS w1_fixture_attempts (
            run_id uuid NOT NULL, stage text NOT NULL, operation_id text NOT NULL,
            attempted_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (run_id, stage))"""))
        inserted = session.execute(text("""INSERT INTO w1_fixture_attempts(run_id,stage,operation_id)
            VALUES (:run,:stage,:operation) ON CONFLICT DO NOTHING"""),
            {"run": request.run_id, "stage": stage, "operation": request.operation_id}).rowcount
        session.commit()
    if inserted != 1:
        raise RuntimeError("scientific fixture rejected a repeated stage identity")
    return _response(stage)


def _stall_targeted_operation(request):
    from sqlalchemy import text
    from scientist import db

    with db.session() as session:
        target_table = session.execute(text("SELECT to_regclass('w2_fixture_stall_targets')")).scalar_one()
        if target_table is None:
            return
        targeted = session.execute(
            text("SELECT 1 FROM w2_fixture_stall_targets WHERE run_id=:run FOR UPDATE"),
            {"run": request.run_id},
        ).scalar_one_or_none()
        if targeted is None:
            return
        session.execute(text("""CREATE TABLE IF NOT EXISTS w2_fixture_stalls (
            run_id uuid PRIMARY KEY, operation_id text NOT NULL,
            started_at timestamptz NOT NULL DEFAULT clock_timestamp())"""))
        inserted = session.execute(text("""INSERT INTO w2_fixture_stalls(run_id, operation_id)
            VALUES (:run, :operation) ON CONFLICT (run_id) DO NOTHING"""),
            {"run": request.run_id, "operation": request.operation_id},
        ).rowcount
        session.commit()
        if inserted != 1:
            raise RuntimeError("W2 fixture stall target was already consumed")
    time.sleep(120)
    raise TimeoutError("W2 fixture stopped before the provider response")


class _BoundaryAckBarrier:
    """Pause once after a committed scientific receipt and before its HTTP ACK is sent."""

    def __init__(self, app, executor, boundary_state=None):
        self.app, self.executor = app, executor
        self.boundary_state = boundary_state

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") != "/control/boundary":
            await self.app(scope, receive, send)
            return
        calls_before = self.boundary_state["calls"] if self.boundary_state is not None else 0
        observed_before = self.boundary_state["observed"] if self.boundary_state is not None else 0
        response = []
        async def capture(message):
            response.append(message)
        try:
            await self.app(scope, receive, capture)
        except Exception as exc:
            name = type(exc).__name__
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name):
                name = "Exception"
            print(f"scientific-fixture-exception status=500 code=other exception_class={name}",
                  file=sys.stderr, flush=True)
            raise
        start = next((m for m in response if m.get("type") == "http.response.start"), None)
        if start is None or start.get("status") != 200:
            status = start.get("status", 0) if start is not None else 0
            state = self.boundary_state
            _diagnose(
                status,
                _error_code(status, response),
                response,
                controller_called=(state["calls"] > calls_before) if state is not None else None,
                observed_domain_error=(state["observed"] > observed_before) if state is not None else None,
            )
            for message in response:
                await send(message)
            return

        from sqlalchemy import text
        from scientist import db, objects
        from scientist.contracts import ObjectRef
        run_id, generation = self.executor.run_id, self.executor.generation
        with db.session() as session:
            contexts = session.execute(text("""SELECT c.manifest->'context' AS ref
                FROM scientific_artifact_receipts r JOIN checkpoints c ON c.id=r.checkpoint_id
                WHERE r.run_id=:run"""), {"run": run_id}).mappings().all()
        committed = False
        for row in contexts:
            ref = ObjectRef.model_validate(row["ref"])
            with objects.open_verified(ref) as source:
                context = json.loads(source.read(1_048_577))
            committed = committed or context.get("boundary") == "tool_committed"
        if not committed:
            for message in response:
                await send(message)
            return
        # The owner acceptance harness arms one recovery run before approval.
        # Ordinary resource runs, including the browser run, pass through untouched.
        with db.session() as session:
            targeted = session.execute(text(
                "SELECT 1 FROM w1_boundary_fault_targets WHERE run_id=:run"
            ), {"run": run_id}).scalar_one_or_none()
        if targeted is None:
            for message in response:
                await send(message)
            return
        with db.session() as session:
            session.execute(text("""CREATE TABLE IF NOT EXISTS w1_boundary_barriers (
                run_id uuid NOT NULL, generation bigint NOT NULL, released boolean NOT NULL DEFAULT false,
                PRIMARY KEY (run_id))"""))
            claimed = session.execute(text("""INSERT INTO w1_boundary_barriers(run_id,generation)
                VALUES (:run,:generation) ON CONFLICT (run_id) DO NOTHING"""),
                {"run": run_id, "generation": generation}).rowcount
            session.commit()
        if _hold_boundary(start["status"], committed, claimed == 1, targeted is not None):
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                with db.session() as session:
                    released = session.execute(text("""SELECT released FROM w1_boundary_barriers
                        WHERE run_id=:run AND generation=:generation"""),
                        {"run": run_id, "generation": generation}).scalar_one()
                if released:
                    break
                await asyncio.sleep(0.05)
            else:
                raise TimeoutError("scientific boundary-ACK barrier expired")
        for message in response:
            await send(message)


def _synthetic_resolver(host, port):
    if type(host) is not str or type(port) is not int or port != 443:
        raise OSError("scientific fixture resolver rejects target")
    if host == "research.example":
        return ["93.184.216.34"]
    if host == "api.crossref.org":
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(item[4][0] for item in addresses))
    raise OSError("scientific fixture resolver rejects target")


def _with_fixture(app, *args, **kwargs):
    from scientist import broker
    from scientist.dispatch_authority import BoundDispatchTransport
    from scientist.dispatch_runtime import parse_dispatch_config

    identity = parse_dispatch_config(Path("/run/scientist/dispatch/config.json").read_bytes())
    original_resolver = broker._resolver
    broker._transport = BoundDispatchTransport(identity.executor_id, identity.process_incarnation, _synthetic_model)
    broker._resolver = _synthetic_resolver
    boundary_state = {"calls": 0, "observed": 0}
    controller, original = None, None
    try:
        from scientist.auth import DomainError
        from scientist.private_worker_api import WorkerController

        routes = [route for route in getattr(app, "routes", ())
                  if getattr(route, "path", None) == "/control/boundary"
                  and "POST" in getattr(route, "methods", ())]
        if len(routes) == 1:
            endpoint = getattr(routes[0], "endpoint", None)
            freevars = tuple(getattr(getattr(endpoint, "__code__", None), "co_freevars", ()))
            cells = getattr(endpoint, "__closure__", None) or ()
            captured = dict(zip(freevars, (cell.cell_contents for cell in cells)))
            candidates = [value for value in captured.values() if isinstance(value, WorkerController)]
            if freevars == ("controller",) and len(candidates) == 1:
                controller = candidates[0]
                original = controller.boundary

                def observed_boundary(*call_args, **call_kwargs):
                    boundary_state["calls"] += 1
                    try:
                        return original(*call_args, **call_kwargs)
                    except DomainError as exc:
                        if exc.status == 503:
                            boundary_state["observed"] += 1
                            source, line, tb = "other", 0, exc.__traceback__
                            files = {
                                "private_worker_api.py": "private_worker_api",
                                "profile_preparation.py": "profile_preparation",
                                "scientific_authority.py": "scientific_authority",
                                "checkpoints.py": "checkpoints",
                                "objects.py": "objects",
                                "broker.py": "broker",
                            }
                            while tb is not None:
                                filename = Path(tb.tb_frame.f_code.co_filename).name
                                if filename in files and type(tb.tb_lineno) is int:
                                    source = files[filename]
                                    line = min(max(tb.tb_lineno, 0), 65535)
                                tb = tb.tb_next
                            try:
                                print(f"scientific-fixture-boundary-origin observed=true source={source} line={line}",
                                      file=sys.stderr, flush=True)
                            except Exception:
                                pass


                        raise
                controller.boundary = observed_boundary
                if controller.boundary is not observed_boundary:
                    controller.boundary = original
                    controller, original = None, None
    except Exception:
        controller, original = None, None
    ready = controller is not None
    print(f"scientific-fixture-boundary-origin safe_wrap_ready={'true' if ready else 'false'}",
          file=sys.stderr, flush=True)
    barrier = _BoundaryAckBarrier(app, identity, boundary_state if ready else None)
    try:
        return _run(barrier, *args, **kwargs)
    finally:
        if controller is not None:
            controller.boundary = original
        broker._resolver = original_resolver


uvicorn.run = _with_fixture

_W2_TOOL_NAMES = {"scientific_search", "scientific_csv_describe"}
_W2_INSTRUCTION = "instruction_view"
_W2_ALL_TOOL_NAMES = _W2_TOOL_NAMES | {_W2_INSTRUCTION}
_W2_INSTRUCTION_CAPABILITIES = ["paper-lookup", "exploratory-data-analysis"]
_W2_MIN_INSTRUCTION_CHARS = 256


def _w2_tool_ids(request):
    """Require both V2 tools in the bytes sent to the synthetic provider."""
    payload = request.payload
    tools = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(tools, list):
        raise RuntimeError("W2 fixture expected serialized native tools")
    definitions = {}
    for item in tools:
        if not isinstance(item, dict) or item.get("type") != "function":
            continue
        function = item.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            definitions[function["name"]] = function
    if not _W2_ALL_TOOL_NAMES <= set(definitions):
        raise RuntimeError("W2 serialized native or instruction tools are incomplete")
    expected = {
        "instruction_view": ("capability_id", _W2_INSTRUCTION_CAPABILITIES),
        "scientific_search": ("request_id", ["crossref"]),
        "scientific_csv_describe": ("grant_id", ["csv_describe"]),
    }
    for name, (argument, identifiers) in expected.items():
        schema = definitions[name].get("parameters")
        if (
            not isinstance(schema, dict)
            or schema.get("type") != "object"
            or schema.get("additionalProperties") is not False
            or schema.get("required") != [argument]
            or schema.get("properties") != {argument: {"type": "string", "enum": identifiers}}
        ):
            raise RuntimeError("W2 serialized native tool schema differs approved binding")
    return definitions


def _w2_stage(request, target):
    if (
        request.kind != "llm"
        or target.url != "https://research.example"
        or request.payload.get("model") != "fixture"
    ):
        raise RuntimeError("W2 fixture rejected an unexpected provider operation")
    _w2_tool_ids(request)
    messages = request.payload.get("messages")
    if not isinstance(messages, list):
        raise RuntimeError("W2 fixture expected serialized native message history")
    calls = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls", []):
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if name in _W2_ALL_TOOL_NAMES:
                calls.append((call.get("id"), name))
    tool_messages = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
    tool_ids = [m.get("tool_call_id") for m in tool_messages]
    if len(tool_ids) != len(set(tool_ids)) or any(not isinstance(item, str) for item in tool_ids):
        raise RuntimeError("W2 fixture rejected invalid native tool response identities")
    run_id = str(request.run_id)
    instruction_id = f"call_w2_instruction_{run_id.replace('-', '')}"
    search_id = f"call_w2_search_{run_id.replace('-', '')}"
    compute_id = f"call_w2_compute_{run_id.replace('-', '')}"
    all_tool_ids = [item for item in tool_ids if item in {instruction_id, search_id, compute_id}]
    if instruction_id in all_tool_ids:
        instruction = next((m.get("content") for m in tool_messages if m.get("tool_call_id") == instruction_id), None)
        if not isinstance(instruction, str) or len(instruction.strip()) < _W2_MIN_INSTRUCTION_CHARS:
            raise RuntimeError("W2 fixture did not receive the selected EDA instruction text")
    if not calls:
        stage = "instruction"
        message = {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": instruction_id, "type": "function",
                "function": {
                    "name": "instruction_view",
                    "arguments": '{"capability_id":"exploratory-data-analysis"}',
                },
            }],
        }
        finish = "tool_calls"
    elif calls == [(instruction_id, _W2_INSTRUCTION)] and all_tool_ids == [instruction_id]:
        stage = "search"
        message = {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": search_id, "type": "function",
                "function": {"name": "scientific_search", "arguments": '{"request_id":"crossref"}'},
            }],
        }
        finish = "tool_calls"
    elif (
        calls == [(instruction_id, _W2_INSTRUCTION), (search_id, "scientific_search")]
        and all_tool_ids == [instruction_id, search_id]
    ):
        stage = "compute"
        message = {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": compute_id, "type": "function",
                "function": {"name": "scientific_csv_describe", "arguments": '{"grant_id":"csv_describe"}'},
            }],
        }
        finish = "tool_calls"
    elif (
        calls == [
            (instruction_id, _W2_INSTRUCTION),
            (search_id, "scientific_search"),
            (compute_id, "scientific_csv_describe"),
        ]
        and all_tool_ids == [instruction_id, search_id, compute_id]
    ):
        stage = "final"
        message = {
            "role": "assistant",
            "content": "Crossref metadata and the approved CSV summary are ready for review.",
        }
        finish = "stop"
    else:
        raise RuntimeError("W2 fixture rejected duplicate or out-of-order bridge calls")
    return stage, message, finish


def _w2_response(request, stage, message, finish):
    from scientist.db import session
    from sqlalchemy import text

    descriptors = _w2_tool_ids(request)
    descriptor_names = sorted(_W2_TOOL_NAMES & set(descriptors))
    tool_digest = hashlib.sha256(json.dumps(
        [{"name": name, "parameters": descriptors[name].get("parameters")}
         for name in descriptor_names], sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    with session() as db:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS w2_scientific_fixture_attempts (
                run_id uuid NOT NULL,
                stage text NOT NULL,
                operation_id text NOT NULL,
                native_tools_sha256 char(64) NOT NULL,
                attempted_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (run_id, stage)
            )
        """))
        inserted = db.execute(text("""
            INSERT INTO w2_scientific_fixture_attempts
                (run_id, stage, operation_id, native_tools_sha256)
            VALUES (:run, :stage, :operation, :tools_hash)
            ON CONFLICT DO NOTHING
        """), {
            "run": request.run_id,
            "stage": stage,
            "operation": request.operation_id,
            "tools_hash": tool_digest,
        }).rowcount
        db.commit()
    if inserted != 1:
        raise RuntimeError("W2 fixture refused a repeated provider stage")
    return json.dumps({
        "id": f"w2-{stage}-{str(request.run_id).replace('-', '')}",
        "object": "chat.completion",
        "created": 1,
        "model": "fixture",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }, separators=(",", ":")).encode(), 2
