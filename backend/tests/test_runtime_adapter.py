"""Runtime adapter tests exercise broker-facing Chat Completions behavior."""

from __future__ import annotations

import hashlib
import json
import base64
import ast
import importlib.util
import sys
from types import ModuleType, SimpleNamespace
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from scientist.contracts import OperationRequest
from scientist.contracts import PlanSpec
from scientist import broker
from scientist.model_payload import llm_input_reserve
from scientist.runtime_adapter import (
    BrokerChatCompletionsTransport,
    BudgetExhausted,
    EffectUnresolved,
    RuntimeAdapterError,
    RuntimeAdapter,
    build_native_agent,
    install_saved_turn_continuation,
    _wire_messages,
)
from scientist.auth import DomainError
from scientist.private_worker_api import parse_boundary
from scientist.runtime_contracts import operation_fingerprint
from scientist.runtime_contracts import AppliedToolId, PendingAssistant, RuntimeContextV1
from test_runtime_contracts import context_data

_ENTRYPOINT_PATH = Path(__file__).resolve().parents[2] / "runtime" / "entrypoint.py"
_ENTRYPOINT_SPEC = importlib.util.spec_from_file_location("scientist_runtime_entrypoint", _ENTRYPOINT_PATH)
assert _ENTRYPOINT_SPEC is not None and _ENTRYPOINT_SPEC.loader is not None
runtime_entrypoint = importlib.util.module_from_spec(_ENTRYPOINT_SPEC)
_ENTRYPOINT_SPEC.loader.exec_module(runtime_entrypoint)


def _checkpoint_ack(payload: dict) -> dict:
    context = payload["context"]
    digest = hashlib.sha256(
        json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    project_id = context["project_id"]
    run_id = context["run_id"]
    checkpoint_id = str(uuid4())
    ref = {
        "project_id": project_id,
        "key": f"runs/{run_id}/{checkpoint_id}",
        "sha256": digest,
        "size": len(json.dumps(context, separators=(",", ":")).encode()),
        "content_type": "application/json",
    }
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "revision": payload["expected_checkpoint_revision"] + 1,
        "plan_digest": context["plan_digest"],
        "runtime_commit": context["runtime_commit"],
        "image_digest": context["image_digest"],
        "skills_digest": context["skills_digest"],
        "context": ref,
        "workspace": [],
        "environment_digest": context["environment_digest"],
        "operation_ids": [item["operation_id"] for item in context["operation_mappings"]],
    }
    return {
        "schema_version": 1,
        "boundary_id": payload["boundary_id"],
        "checkpoint_id": checkpoint_id,
        "checkpoint_revision": manifest["revision"],
        "manifest": manifest,
    }


def _pinned_hermes_tool_call(
    call_id: str,
    name: str,
    arguments: str,
    *,
    provider_data: dict[str, Any] | None = None,
) -> Any:
    """Load the exact pinned Hermes ToolCall used by the live transport."""
    source = (
        Path(__file__).resolve().parents[2]
        / ".local/vendor/hermes/agent/transports/types.py"
    )
    spec = importlib.util.spec_from_file_location("agent.transports.types", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.ToolCall(
        id=call_id,
        name=name,
        arguments=arguments,
        provider_data=provider_data,
    )


def _chat_completion(content: str) -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-fixture",
            "object": "chat.completion",
            "created": 1,
            "model": "fixture",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
        },
        separators=(",", ":"),
    ).encode()


def test_chat_completion_checkpoints_exact_request_before_effect_and_returns_committed_bytes(
    tmp_path: Path,
    context_data: dict,
) -> None:
    events: list[tuple[str, dict | None]] = []
    response_bytes = _chat_completion("Fixture result")

    def broker(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        assert request.headers["X-Worker-Capability"] == "fixture-capability"
        if path == "/control/boundary":
            events.append(("boundary", body))
            return httpx.Response(200, json=_checkpoint_ack(body))
        if path == "/effects":
            events.append(("effect", body))
            return httpx.Response(
                200,
                json={
                    "operation_id": body["operation_id"],
                    "state": "committed",
                    "result": {
                        "project_id": context_data["project_id"],
                        "key": "results/fixture",
                        "sha256": hashlib.sha256(response_bytes).hexdigest(),
                        "size": len(response_bytes),
                        "content_type": "application/json",
                    },
                    "usage_tokens": 11,
                },
            )
        if path.endswith("/result"):
            events.append(("result", None))
            return httpx.Response(200, content=response_bytes, headers={"content-type": "application/json"})
        raise AssertionError(f"unexpected broker route: {path}")

    broker_client = httpx.Client(transport=httpx.MockTransport(broker), trust_env=False)
    context_data["compacted_context"] = None
    context_data["operation_sequence"] = 0
    adapter = RuntimeAdapter(
        context_data,
        broker_url="http://172.30.0.2:8000",
        capability="fixture-capability",
        workspace_dir=tmp_path,
        broker_client=broker_client,
    )
    transport = BrokerChatCompletionsTransport(adapter)
    native_client = httpx.Client(transport=transport, trust_env=False)
    body = {
        "model": "fixture",
        "messages": [{"role": "user", "content": "Find one fixture paper"}],
        "max_tokens": 64,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "todo_list",
                    "description": "Track a research task",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                },
            }
        ],
        "tool_choice": "auto",
    }

    response = native_client.post("http://hermes.invalid/v1/chat/completions", json=body)

    assert response.json()["choices"][0]["message"]["content"] == "Fixture result"
    assert [name for name, _ in events] == ["boundary", "effect", "result", "boundary"]
    first_checkpoint = events[0][1]
    operation = OperationRequest.model_validate(events[1][1])
    assert operation.payload["messages"] == body["messages"]
    assert operation.payload["tools"] == body["tools"]
    assert operation.payload["tool_choice"] == "auto"
    assert operation.payload["provider_id"] == context_data["provider_id"]
    assert operation.payload["recipient"] == context_data["provider_endpoint"]
    assert operation.operation_id == first_checkpoint["context"]["operation_mappings"][0]["operation_id"]
    assert first_checkpoint["context"]["operation_sequence"] == 1
    assert first_checkpoint["context"]["boundary"] == "before_model"
    assert operation.payload["max_output_tokens"] == 64
    assert operation.reserve_tokens >= 64
    assert operation.generation == context_data["generation"]
    assert operation_fingerprint(operation) == first_checkpoint["context"]["operation_mappings"][0]["payload_hash"]
    committed_checkpoint = events[-1][1]
    assert committed_checkpoint["context"]["boundary"] == "model_committed"
    assert committed_checkpoint["context"]["messages"][-1] == {
        "role": "assistant",
        "content": "Fixture result",
    }


def test_checkpoint_wire_round_trips_through_private_boundary_parser(
    tmp_path: Path, context_data: dict[str, Any]
) -> None:
    wire_requests: list[dict[str, Any]] = []

    def broker(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/control/boundary"
        wire_requests.append(json.loads(request.content))
        try:
            parsed = parse_boundary(request.content)
        except DomainError as exc:
            return httpx.Response(exc.status, json={"detail": exc.code})
        return httpx.Response(200, json=_checkpoint_ack(parsed.model_dump(mode="json")))

    adapter = RuntimeAdapter(
        context_data,
        broker_url="http://172.30.0.2:8000",
        capability="fixture-capability",
        workspace_dir=tmp_path,
        broker_client=httpx.Client(transport=httpx.MockTransport(broker), trust_env=False),
    )

    ack = adapter._checkpoint("before_model")

    assert ack.checkpoint_revision == 1
    assert len(wire_requests) == 1
    assert wire_requests[0]["context"]["compacted_context"] is None
    assert wire_requests[0]["context"]["pending_assistant"] is None


def test_generated_model_mapping_passes_real_broker_scope_before_effect(
    tmp_path: Path,
    context_data: dict[str, Any],
    db,
    project_session,
) -> None:
    project_id, _session_id = project_session
    context_data = json.loads(json.dumps(context_data))
    context_data["project_id"] = str(project_id)
    plan = PlanSpec.model_validate(context_data["plan"])
    provider_id = plan.provider_id
    db.execute(
        text(
            "INSERT INTO credentials (id, project_id, label, provider, encrypted_value) "
            "VALUES (:id, :project, 'fixture', 'fixture', :value)"
        ),
        {"id": provider_id, "project": project_id, "value": b"fixture-secret"},
    )
    broker.configure(
        provider_destinations={str(provider_id): "https://research.example"},
        resolver=lambda host, port: ["8.8.8.8"],
    )

    class EffectReached(Exception):
        pass

    checked_requests: list[OperationRequest] = []

    def broker_endpoint(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control/boundary":
            parsed = parse_boundary(request.content)
            mapping = parsed.context.operation_mappings[0]
            try:
                broker._validate_scope(db, project_id, plan, mapping.request)
            except DomainError as exc:
                return httpx.Response(exc.status, json={"detail": exc.code})
            checked_requests.append(mapping.request)
            return httpx.Response(200, json=_checkpoint_ack(parsed.model_dump(mode="json")))
        if request.url.path == "/effects":
            raise EffectReached
        raise AssertionError(f"unexpected broker route: {request.url.path}")

    adapter = RuntimeAdapter(
        context_data,
        broker_url="http://172.30.0.2:8000",
        capability="fixture-capability",
        workspace_dir=tmp_path,
        broker_client=httpx.Client(transport=httpx.MockTransport(broker_endpoint), trust_env=False),
    )
    try:
        with pytest.raises(EffectReached):
            adapter.dispatch_chat_completion(
                {
                    "model": context_data["model"],
                    "messages": [{"role": "user", "content": "Check this fixture."}],
                    "max_tokens": 64,
                }
            )
    finally:
        adapter._broker.close()
        broker.configure()

    assert len(checked_requests) == 1
    assert checked_requests[0].payload["timeout_seconds"] <= 20


def _committing_broker(context_data: dict[str, Any], events: list, response_bytes: bytes):
    def broker(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        if path == "/control/boundary":
            events.append(("boundary", body))
            return httpx.Response(200, json=_checkpoint_ack(body))
        if path == "/effects":
            events.append(("effect", body))
            return httpx.Response(
                200,
                json={
                    "operation_id": body["operation_id"],
                    "state": "committed",
                    "result": {
                        "project_id": context_data["project_id"],
                        "key": "results/bounded",
                        "sha256": hashlib.sha256(response_bytes).hexdigest(),
                        "size": len(response_bytes),
                        "content_type": "application/json",
                    },
                    "usage_tokens": 11,
                },
            )
        if path.endswith("/result"):
            return httpx.Response(200, content=response_bytes)
        raise AssertionError(f"unexpected broker route: {path}")

    return httpx.Client(transport=httpx.MockTransport(broker), trust_env=False)


def test_native_output_limit_defaults_caps_and_rejects_invalid_values(
    tmp_path: Path, context_data: dict[str, Any]
) -> None:
    """ADR-012: output = min(2048, snapshot remaining - broker input reserve), for
    default and explicit limits alike; explicit values above it are clamped."""
    events: list[tuple[str, dict[str, Any] | None]] = []
    response_bytes = _chat_completion("Bounded result")
    context_data["compacted_context"] = None
    context_data["operation_sequence"] = 0

    def posted(remaining: int, content: str, **limit: Any) -> OperationRequest:
        context_data["budget_remaining_tokens"] = remaining
        client = _committing_broker(context_data, events, response_bytes)
        adapter = RuntimeAdapter(
            context_data,
            broker_url="http://172.30.0.2:8000",
            capability="fixture-capability",
            workspace_dir=tmp_path,
            broker_client=client,
        )
        native = httpx.Client(transport=BrokerChatCompletionsTransport(adapter), trust_env=False)
        try:
            native.post(
                "http://hermes.invalid/v1/chat/completions",
                json={"model": "fixture", "messages": [{"role": "user", "content": content}], **limit},
            )
        finally:
            native.close()
            client.close()
        operation = OperationRequest.model_validate([body for name, body in events if name == "effect"][-1])
        assert operation.reserve_tokens == input_reserve(content) + operation.payload["max_output_tokens"]
        return operation

    def input_reserve(content: str) -> int:
        return llm_input_reserve([{"role": "user", "content": content}])

    ample = 10**6
    assert posted(ample, "default").payload["max_output_tokens"] == 2048
    assert posted(ample, "explicit above cap", max_tokens=4096).payload["max_output_tokens"] == 2048
    assert posted(ample, "explicit below cap", max_tokens=100).payload["max_output_tokens"] == 100
    assert posted(ample, "completion alias", max_completion_tokens=4096).payload["max_output_tokens"] == 2048
    # A small snapshot (e.g. an owner extension of a plan approved at 0) bounds every request.
    for content, limit in (("small default", {}), ("small explicit", {"max_tokens": 1500})):
        operation = posted(input_reserve(content) + 700, content, **limit)
        assert operation.payload["max_output_tokens"] == 700
    assert posted(input_reserve("one") + 1, "one").payload["max_output_tokens"] == 1
    assert posted(input_reserve("lower") + 700, "lower", max_tokens=300).payload["max_output_tokens"] == 300

    # Earlier calls in the same generation reduce the advisory snapshot by their committed
    # usage (11 here; the broker released the rest of the reservation).
    context_data["budget_remaining_tokens"] = input_reserve("first") + 900 + input_reserve("second") + 300
    client = _committing_broker(context_data, events, response_bytes)
    adapter = RuntimeAdapter(
        context_data,
        broker_url="http://172.30.0.2:8000",
        capability="fixture-capability",
        workspace_dir=tmp_path,
        broker_client=client,
    )
    native = httpx.Client(transport=BrokerChatCompletionsTransport(adapter), trust_env=False)
    for content, limit in (("first", {"max_tokens": 900}), ("second", {})):
        native.post(
            "http://hermes.invalid/v1/chat/completions",
            json={"model": "fixture", "messages": [{"role": "user", "content": content}], **limit},
        )
    first, second = [OperationRequest.model_validate(body) for name, body in events if name == "effect"][-2:]
    assert first.payload["max_output_tokens"] == 900
    assert second.payload["max_output_tokens"] == input_reserve("first") + 900 + 300 - 11

    event_count = len(events)
    for invalid in (True, False, 1.5, 0, -1, "8"):
        for field in ("max_tokens", "max_completion_tokens"):
            with pytest.raises(RuntimeAdapterError, match="output limit"):
                native.post(
                    "http://hermes.invalid/v1/chat/completions",
                    json={"model": "fixture", "messages": [{"role": "user", "content": "x"}], field: invalid},
                )
    assert len(events) == event_count, "invalid caps must fail before checkpoint or broker I/O"
    native.close()
    client.close()


def test_missing_budget_snapshot_fails_closed_before_io(tmp_path: Path, context_data: dict[str, Any]) -> None:
    events: list = []
    context_data.pop("budget_remaining_tokens", None)
    client = _committing_broker(context_data, events, _chat_completion("unused"))
    adapter = RuntimeAdapter(
        context_data,
        broker_url="http://172.30.0.2:8000",
        capability="fixture-capability",
        workspace_dir=tmp_path,
        broker_client=client,
    )
    with pytest.raises(RuntimeAdapterError, match="budget snapshot"):
        adapter.dispatch_chat_completion({"model": "fixture", "messages": [{"role": "user", "content": "x"}]})
    assert events == []


def test_insufficient_allowance_defers_to_broker_budget_wait_and_never_sends_zero_or_one(
    tmp_path: Path, context_data: dict[str, Any]
) -> None:
    effects: list[dict] = []

    def broker_endpoint(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/control/boundary":
            return httpx.Response(200, json=_checkpoint_ack(body))
        if request.url.path == "/effects":
            effects.append(body)
            return httpx.Response(409, json={"detail": {"code": "budget_exhausted"}})
        raise AssertionError(f"unexpected broker route: {request.url.path}")

    messages = [{"role": "user", "content": "Too little budget."}]
    context_data["budget_remaining_tokens"] = llm_input_reserve(messages)  # allowance 0
    client = httpx.Client(transport=httpx.MockTransport(broker_endpoint), trust_env=False)
    adapter = RuntimeAdapter(
        context_data,
        broker_url="http://172.30.0.2:8000",
        capability="fixture-capability",
        workspace_dir=tmp_path,
        broker_client=client,
    )
    with pytest.raises(BudgetExhausted):
        adapter.dispatch_chat_completion({"model": "fixture", "messages": messages})
    assert len(effects) == 1
    # The broker, not the worker, decides: the unclamped native request, never 0 or a forced 1.
    assert effects[0]["payload"]["max_output_tokens"] == 2048
    assert adapter.budget_exhausted
    with pytest.raises(BudgetExhausted):
        adapter.dispatch_chat_completion({"model": "fixture", "messages": messages})
    assert len(effects) == 1, "a broker budget wait is sticky for the rest of the generation"
    client.close()


def test_generation_two_replay_keeps_journaled_operation_despite_new_snapshot(
    tmp_path: Path, context_data: dict[str, Any]
) -> None:
    messages = [{"role": "user", "content": "Replay me."}]
    journaled = OperationRequest(
        run_id=context_data["run_id"], generation=1, operation_id="journaled-model", kind="llm",
        payload={
            "provider_id": context_data["provider_id"], "model": "fixture",
            "recipient": context_data["provider_endpoint"], "credential_id": context_data["provider_id"],
            "max_output_tokens": 2048, "messages": messages, "timeout_seconds": 20,
        },
        reserve_tokens=llm_input_reserve(messages) + 2048,
    )
    context_data.update(
        generation=2, messages=messages, operation_sequence=1, budget_remaining_tokens=llm_input_reserve(messages) + 5,
        operation_mappings=[{
            "operation_id": journaled.operation_id, "turn_id": context_data["turn_id"], "purpose": "model",
            "model_sequence": 0, "tool_call_id": None, "request": journaled.model_dump(mode="json"),
            "payload_hash": operation_fingerprint(journaled),
        }],
    )
    events: list = []
    client = _committing_broker(context_data, events, _chat_completion("Stored result"))
    adapter = RuntimeAdapter(
        context_data,
        broker_url="http://172.30.0.2:8000",
        capability="fixture-capability",
        workspace_dir=tmp_path,
        broker_client=client,
    )
    adapter.dispatch_chat_completion({"model": "fixture", "messages": messages})
    replayed = OperationRequest.model_validate(next(body for name, body in events if name == "effect"))
    assert replayed == journaled.model_copy(update={"generation": 2})
    assert [item.operation_id for item in adapter.context.operation_mappings] == ["journaled-model"]
    client.close()


def test_unknown_model_effect_pauses_without_fetching_result_or_retrying(
    tmp_path: Path,
    context_data: dict,
) -> None:
    calls: list[str] = []

    def broker(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        body = json.loads(request.content) if request.content else None
        if path == "/control/boundary":
            return httpx.Response(200, json=_checkpoint_ack(body))
        if path == "/effects":
            return httpx.Response(
                200,
                json={"operation_id": body["operation_id"], "state": "unknown", "result": None, "usage_tokens": None},
            )
        raise AssertionError("unresolved effect bytes must remain inaccessible")

    broker_client = httpx.Client(transport=httpx.MockTransport(broker), trust_env=False)
    adapter = RuntimeAdapter(
        context_data,
        broker_url="http://172.30.0.2:8000",
        capability="fixture-capability",
        workspace_dir=tmp_path,
        broker_client=broker_client,
    )
    native_client = httpx.Client(transport=BrokerChatCompletionsTransport(adapter), trust_env=False)

    with pytest.raises(EffectUnresolved):
        native_client.post(
            "http://hermes.invalid/v1/chat/completions",
            json={"model": "fixture", "messages": [{"role": "user", "content": "Find one paper"}], "max_tokens": 64},
        )

    assert calls == ["/control/boundary", "/effects"]
    assert adapter.context.operation_sequence == 1


def test_compression_request_is_capped_and_preserves_primary_transcript(
    tmp_path: Path,
    context_data: dict,
) -> None:
    response_bytes = _chat_completion("Compressed summary")
    events: list[tuple[str, dict]] = []

    def broker(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        if request.url.path == "/control/boundary":
            events.append(("boundary", body))
            return httpx.Response(200, json=_checkpoint_ack(body))
        if request.url.path == "/effects":
            events.append(("effect", body))
            return httpx.Response(
                200,
                json={
                    "operation_id": body["operation_id"],
                    "state": "committed",
                    "result": {
                        "project_id": context_data["project_id"],
                        "key": "results/compression",
                        "sha256": hashlib.sha256(response_bytes).hexdigest(),
                        "size": len(response_bytes),
                        "content_type": "application/json",
                    },
                    "usage_tokens": 11,
                },
            )
        if request.url.path.endswith("/result"):
            return httpx.Response(200, content=response_bytes)
        raise AssertionError(f"unexpected broker route: {request.url.path}")

    context_data["compacted_context"] = None
    context_data["operation_sequence"] = 0
    primary_before = list(context_data["messages"])
    adapter = RuntimeAdapter(
        context_data,
        broker_url="http://172.30.0.2:8000",
        capability="fixture-capability",
        workspace_dir=tmp_path,
        broker_client=httpx.Client(transport=httpx.MockTransport(broker), trust_env=False),
        operation_purpose="compression",
    )
    client = httpx.Client(transport=BrokerChatCompletionsTransport(adapter, purpose="compression"))
    request_body = {
        "model": adapter.context.model,
        "messages": [{"role": "user", "content": "summarize this transcript"}],
    }

    response = client.post("http://hermes.invalid/v1/chat/completions", json=request_body)

    operation = OperationRequest.model_validate(events[1][1])
    assert operation.payload["max_output_tokens"] == 2048
    assert operation.reserve_tokens >= 2048
    assert [message.model_dump(mode="json", exclude_none=True) for message in adapter.context.messages] == primary_before
    assert adapter.context.pending_assistant is None
    assert response.json()["choices"][0]["message"]["content"] == "Compressed summary"


def test_native_wire_projection_keeps_message_name_and_strips_native_timestamps() -> None:
    assert _wire_messages(
        [
            {
                "role": "tool",
                "name": "todo_list",
                "tool_call_id": "tool_1",
                "content": "saved",
                "timestamp": 1791030896.125,
                "_row_id": "database-only",
            }
        ]
    ) == [
        {
            "role": "tool",
            "name": "todo_list",
            "tool_call_id": "tool_1",
            "content": "saved",
        }
    ]

    assert _wire_messages(
        [
            {
                "role": "user",
                "content": "question",
                "name": None,
                "tool_call_id": None,
                "tool_calls": None,
            }
        ]
    ) == [{"role": "user", "content": "question"}]

    assert _wire_messages(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "raw_call_1",
                        "type": "function",
                        "function": {"name": "todo_list", "arguments": "{\"todos\":[]}"},
                    }
                ],
            }
        ]
    ) == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "raw_call_1",
                    "type": "function",
                    "function": {"name": "todo_list", "arguments": "{\"todos\":[]}"},
                }
            ],
        }
    ]


def test_native_wire_projection_strips_only_hermes_tool_call_alias_metadata() -> None:
    # Pinned Hermes builds native assistant history rows with both the OpenAI
    # `id` and Codex Responses `call_id` / `response_item_id` aliases. The
    # strict platform ChatMessage accepts the canonical `id` only.
    native_message = {
        "role": "assistant",
        "content": None,
        "timestamp": 1791030896.125,
        "tool_calls": [
            {
                "id": "call_native_1",
                "call_id": "call_native_1",
                "response_item_id": "fc_native_1",
                "type": "function",
                "function": {"name": "todo_list", "arguments": '{"todos":[]}'},
            },
            {
                "id": "call_native_2",
                "call_id": "call_native_2",
                "response_item_id": "fc_native_2",
                "type": "function",
                "function": {"name": "todo_list", "arguments": '{"todos":[]}'},
            },
        ],
    }

    projected = _wire_messages([native_message])

    assert projected == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_native_1",
                    "type": "function",
                    "function": {"name": "todo_list", "arguments": '{"todos":[]}'},
                },
                {
                    "id": "call_native_2",
                    "type": "function",
                    "function": {"name": "todo_list", "arguments": '{"todos":[]}'},
                },
            ],
        }
    ]
    assert native_message["tool_calls"][0]["call_id"] == "call_native_1"
    assert native_message["tool_calls"][0]["response_item_id"] == "fc_native_1"

    with pytest.raises(RuntimeAdapterError, match="wire profile"):
        _wire_messages(
            [
                {
                    **native_message,
                    "tool_calls": [
                        {**native_message["tool_calls"][0], "unreviewed": "metadata"}
                    ],
                }
            ]
        )

    with pytest.raises(RuntimeAdapterError, match="identifier"):
        _wire_messages(
            [
                {
                    **native_message,
                    "tool_calls": [
                        {**native_message["tool_calls"][0], "call_id": " "}
                    ],
                }
            ]
        )

    with pytest.raises(RuntimeAdapterError, match="wire profile"):
        _wire_messages(
            [
                {
                    **native_message,
                    "tool_calls": [
                        {**native_message["tool_calls"][0], "id": 7}
                    ],
                }
            ]
        )


def test_saved_turn_continuation_reuses_primary_history_without_admitting_a_user_row(
    tmp_path: Path,
    context_data: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_data["current_turn_user_index"] = 0
    context_data["native_turn_timestamp"] = 1791030000.25
    adapter = RuntimeAdapter(
        context_data,
        broker_url="http://172.30.0.2:8000",
        capability="fixture-capability",
        workspace_dir=tmp_path,
        broker_client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
    )

    class FakeTurnContext:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    conversation_loop_module = ModuleType("agent.conversation_loop")
    conversation_loop_module.build_turn_context = lambda *_args, **_kwargs: None
    turn_context_module = ModuleType("agent.turn_context")
    turn_context_module.TurnContext = FakeTurnContext
    turn_context_module._reset_per_turn_agent_state = lambda _agent: None
    agent_package = ModuleType("agent")
    agent_package.__path__ = []
    monkeypatch.setitem(sys.modules, "agent", agent_package)
    monkeypatch.setitem(sys.modules, "agent.conversation_loop", conversation_loop_module)
    monkeypatch.setitem(sys.modules, "agent.turn_context", turn_context_module)
    agent = SimpleNamespace()
    history = adapter.native_history()
    original_count = len(history)

    install_saved_turn_continuation(agent, adapter)
    context = conversation_loop_module.build_turn_context(
        agent,
        "ignored caller text",
        "system",
        history,
        str(adapter.context.run_id),
        None,
        None,
    )

    assert len(context.messages) == original_count
    assert context.messages[-1]["role"] == "user"
    assert context.messages[-1]["content"] == "Find fixture papers"
    assert context.current_turn_user_idx == 0
    assert context.turn_id == str(adapter.context.turn_id)
    assert agent._current_turn_timestamp == 1791030000.25


def _wrapped_budget_wait() -> Exception:
    """A raised wrapper around BudgetExhausted (for paths that do re-raise). Pinned Hermes'
    primary API loop instead returns a failed turn dict; see the failed-turn test below."""
    try:
        try:
            raise BudgetExhausted("broker recorded an owner budget decision")
        except BudgetExhausted as exc:
            raise RuntimeAdapterError("Connection error.") from exc
    except RuntimeAdapterError as wrapped:
        return wrapped


@pytest.mark.parametrize(
    ("flag", "error", "clean"),
    [
        (True, _wrapped_budget_wait, True),
        (True, lambda: BudgetExhausted("direct"), True),
        (False, _wrapped_budget_wait, False),  # never trust the chain without the adapter flag
        (False, lambda: RuntimeAdapterError("Connection error."), False),
        (True, lambda: RuntimeAdapterError("unrelated failure after the wait"), False),
    ],
)
def test_worker_exits_cleanly_only_for_a_broker_budget_wait(
    tmp_path: Path, context_data: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], flag: bool, error: Any, clean: bool,
) -> None:
    capability = tmp_path / "capability"
    capability.write_text("fixture-capability")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    entry = runtime_entrypoint
    monkeypatch.setattr(entry, "wait_until_ready", lambda *_a, **_k: None)
    monkeypatch.setattr(entry, "_sanitize_environment",
                        lambda: (str(tmp_path), str(workspace), "http://172.30.0.2:8000", str(capability)))
    monkeypatch.setattr(entry, "load_bootstrap", lambda _path: (
        RuntimeContextV1.model_validate(context_data), [], SimpleNamespace(checkpoint_revision=0)))
    monkeypatch.setattr(entry, "_pin_import_paths", lambda: None)

    def hermes_raises(adapter: RuntimeAdapter, **_kwargs: Any) -> Any:
        adapter.budget_exhausted = flag
        raise error()

    monkeypatch.setattr(entry, "build_native_agent", hermes_raises)
    monkeypatch.chdir(tmp_path)
    if clean:
        assert entry.run_worker() is None
        logged = capsys.readouterr().err
        assert "BudgetExhausted" in logged or "RuntimeAdapterError" in logged
        assert "fixture-capability" not in logged and "Connection error" not in logged
    else:
        with pytest.raises(Exception) as raised:
            entry.run_worker()
        assert raised.type in (RuntimeAdapterError, BudgetExhausted)


_HERMES_FAILED_TURN = {"completed": False, "failed": True, "error": "Connection error.", "failure_reason": "timeout"}


@pytest.mark.parametrize(
    ("broker_replies", "pending_tools", "clean"),
    [
        (["budget"], False, True),
        (["forbidden"], False, False),  # broker refused for another reason: no flag
        (["unknown", "budget"], False, False),  # an unresolved effect is never a clean pause
        (["budget"], True, False),  # an unfinished native tool batch is never a clean pause
    ],
)
def test_worker_exits_cleanly_when_pinned_hermes_returns_failed_turn_after_budget_wait(
    tmp_path: Path, context_data: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], broker_replies: list[str], pending_tools: bool, clean: bool,
) -> None:
    """Pinned Hermes (bd0affe5) catches transport errors in _run_api_retry_loop and returns
    _HERMES_FAILED_TURN; the BudgetExhausted exception chain never reaches the entrypoint."""
    capability = tmp_path / "capability"
    capability.write_text("fixture-capability")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    replies = iter(broker_replies)

    def broker_endpoint(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/control/boundary":
            return httpx.Response(200, json=_checkpoint_ack(body))
        reply = next(replies)
        if reply == "unknown":
            return httpx.Response(200, json={"operation_id": body["operation_id"], "state": "unknown",
                                             "result": None, "usage_tokens": None})
        code = {"budget": "budget_exhausted", "forbidden": "forbidden"}[reply]
        return httpx.Response(409 if reply == "budget" else 403, json={"detail": {"code": code}})

    client = httpx.Client(transport=httpx.MockTransport(broker_endpoint), trust_env=False)
    entry = runtime_entrypoint
    context_data["current_turn_user_index"] = 0
    monkeypatch.setattr(entry, "wait_until_ready", lambda *_a, **_k: None)
    monkeypatch.setattr(entry, "_sanitize_environment",
                        lambda: (str(tmp_path), str(workspace), "http://172.30.0.2:8000", str(capability)))
    monkeypatch.setattr(entry, "load_bootstrap", lambda _path: (
        RuntimeContextV1.model_validate(context_data), [], SimpleNamespace(checkpoint_revision=0)))
    monkeypatch.setattr(entry, "_pin_import_paths", lambda: None)
    monkeypatch.setattr(entry, "RuntimeAdapter", lambda *a, **k: RuntimeAdapter(*a, broker_client=client, **k))
    monkeypatch.setattr(entry, "install_saved_turn_continuation", lambda *_a: None)

    class PinnedHermesShape:
        def __init__(self, adapter: RuntimeAdapter) -> None:
            self.adapter = adapter

        def run_conversation(self, **_kwargs: Any) -> dict[str, Any]:
            for content in broker_replies:
                try:
                    self.adapter.dispatch_chat_completion(
                        {"model": "fixture", "messages": [{"role": "user", "content": content}]})
                except Exception:
                    pass  # handle_api_error: no re-raise
            if pending_tools:
                self.adapter.context = self.adapter.context.model_copy(update={"pending_assistant": PendingAssistant(
                    turn_id=self.adapter.context.turn_id, message_index=0, next_tool_index=0)})
            return dict(_HERMES_FAILED_TURN)

    monkeypatch.setattr(entry, "build_native_agent", lambda adapter, **_k: PinnedHermesShape(adapter))
    monkeypatch.chdir(tmp_path)
    if clean:
        assert entry.run_worker() is None
        logged = capsys.readouterr().err
        assert "budget" in logged and "Connection error" not in logged and "fixture-capability" not in logged
    else:
        with pytest.raises(RuntimeAdapterError, match="complete, quiescent turn"):
            entry.run_worker()
    client.close()


def test_committed_usage_replaces_reservation_in_advisory_allowance(
    tmp_path: Path, context_data: dict[str, Any]
) -> None:
    """The broker turns a committed reservation into its actual usage; unknown keeps it all."""
    effects: list[OperationRequest] = []
    response_bytes = _chat_completion("ok")

    def broker_endpoint(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        if request.url.path == "/control/boundary":
            return httpx.Response(200, json=_checkpoint_ack(body))
        if request.url.path == "/effects":
            operation = OperationRequest.model_validate(body)
            effects.append(operation)
            if operation.payload["messages"][0]["content"] == "lost":
                return httpx.Response(200, json={"operation_id": body["operation_id"], "state": "unknown",
                                                 "result": None, "usage_tokens": None})
            return httpx.Response(200, json={
                "operation_id": body["operation_id"], "state": "committed", "usage_tokens": 11,
                "result": {"project_id": context_data["project_id"], "key": "results/ok",
                           "sha256": hashlib.sha256(response_bytes).hexdigest(), "size": len(response_bytes),
                           "content_type": "application/json"}})
        return httpx.Response(200, content=response_bytes)

    def reserve(content: str) -> int:
        return llm_input_reserve([{"role": "user", "content": content}])

    context_data["budget_remaining_tokens"] = 2000
    client = httpx.Client(transport=httpx.MockTransport(broker_endpoint), trust_env=False)
    adapter = RuntimeAdapter(context_data, broker_url="http://172.30.0.2:8000", capability="fixture-capability",
                             workspace_dir=tmp_path, broker_client=client)

    def call(content: str, **limit: Any) -> int:
        adapter.dispatch_chat_completion({"model": "fixture", "messages": [{"role": "user", "content": content}], **limit})
        return effects[-1].payload["max_output_tokens"]

    assert call("first", max_tokens=100) == 100
    assert call("second", max_tokens=100) == 100
    assert call("third") == 2000 - 11 - 11 - reserve("third")  # ledger: usage 22, nothing held
    with pytest.raises(EffectUnresolved):
        call("lost", max_tokens=100)
    held = reserve("lost") + 100
    assert call("fourth") == 2000 - 11 * 3 - held - reserve("fourth")
    client.close()


def test_readiness_marker_must_be_literal_ready(tmp_path: Path) -> None:
    marker = tmp_path / "ready"
    marker.write_bytes(b"ready\n")
    with pytest.raises(runtime_entrypoint.BootstrapError):
        runtime_entrypoint.wait_until_ready(marker, timeout_seconds=0.01)
    marker.write_bytes(b"ready")
    runtime_entrypoint.wait_until_ready(marker, timeout_seconds=0.1)


def test_workspace_materialization_verifies_hash_and_refuses_existing_data(tmp_path: Path) -> None:
    data = b"fixture workspace"
    manifest = [
        {
            "path": "nested/input.txt",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "data_base64": base64.b64encode(data).decode("ascii"),
        }
    ]
    workspace = tmp_path / "workspace"
    runtime_entrypoint._materialize_workspace(workspace, manifest)
    assert (workspace / "nested/input.txt").read_bytes() == data
    with pytest.raises(runtime_entrypoint.BootstrapError):
        runtime_entrypoint._materialize_workspace(workspace, manifest)


def test_bootstrap_requires_explicit_checkpoint_revision(
    tmp_path: Path, context_data: dict
) -> None:
    (tmp_path / "context.json").write_text(json.dumps(context_data), encoding="utf-8")
    (tmp_path / "workspace.json").write_text("[]", encoding="utf-8")
    with pytest.raises(runtime_entrypoint.BootstrapError):
        runtime_entrypoint.load_bootstrap(tmp_path)
    (tmp_path / "metadata.json").write_text(
        json.dumps({"schema_version": 1, "checkpoint_revision": 7}), encoding="utf-8"
    )
    context, workspace, metadata = runtime_entrypoint.load_bootstrap(tmp_path)
    assert str(context.run_id) == context_data["run_id"]
    assert workspace == []
    assert metadata.checkpoint_revision == 7


def test_runtime_import_path_excludes_workspace_and_uncontrolled_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    original = list(runtime_entrypoint.sys.path)
    try:
        monkeypatch.setattr(runtime_entrypoint.sys, "path", ["", "/workspace", "/tmp/untrusted"])
        runtime_entrypoint._pin_import_paths()
        assert runtime_entrypoint.sys.path[:2] == ["/opt/scientist", "/opt/hermes"]
        assert all("/workspace" not in item and "/tmp/untrusted" not in item for item in runtime_entrypoint.sys.path)
    finally:
        runtime_entrypoint.sys.path[:] = original


def test_selected_skill_manifest_matches_pinned_catalog_files() -> None:
    repo = Path(__file__).resolve().parents[2]
    manifest = json.loads((repo / "runtime/skills-manifest.json").read_text(encoding="utf-8"))
    digest = manifest.pop("manifest_sha256")
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(encoded.encode()).hexdigest() == digest
    source_root = repo / ".local/build/scientific-server-source-check-20261006a/skills"
    if not source_root.is_dir():
        source_root = Path.home() / "AI-Scientist-Agent-Platform-dem/.local/build/scientific-server-source-check-20261006a/skills"
    if not source_root.is_dir():
        pytest.skip("full pinned skills source archive is not present")
    assert len(manifest["skills"]) == 177
    for skill in manifest["skills"]:
        for item in skill["files"]:
            source = source_root / skill["name"] / item["path"]
            raw = source.read_bytes()
            assert len(raw) == item["size"]
            assert hashlib.sha256(raw).hexdigest() == item["sha256"]


def test_native_tool_round_checkpoints_applied_id_before_execution(
    tmp_path: Path, context_data: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    context_data["messages"].append(
        {
            "role": "assistant",
            "content": "Working through the request.",
            "tool_calls": [
                {
                    "id": "raw_call_1",
                    "type": "function",
                    "function": {"name": "todo_list", "arguments": "{}"},
                }
            ],
        }
    )
    context_data["pending_assistant"] = {
        "turn_id": context_data["turn_id"],
        "message_index": len(context_data["messages"]) - 1,
        "next_tool_index": 0,
        "applied_tool_ids": [],
    }
    context_data["boundary"] = "model_committed"
    events: list[dict] = []

    def broker(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        events.append(body)
        assert request.url.path == "/control/boundary"
        return httpx.Response(200, json=_checkpoint_ack(body))

    class FakeTodoStore:
        def restore(self, todos: list, *, revision: int) -> None:
            self.todos = todos
            self.revision = revision

        def snapshot(self) -> dict:
            return {"todos": self.todos, "revision": self.revision}

    class FakeNativeToolCall:
        def __init__(self, call_id: str, name: str, arguments: str) -> None:
            self.id = call_id
            self.function = SimpleNamespace(name=name, arguments=arguments)

        def model_dump(self, mode: str = "json") -> dict:
            return {
                "id": self.id,
                "type": "function",
                "function": {"name": self.function.name, "arguments": self.function.arguments},
            }

    class FakeAIAgent:
        def __init__(self, **kwargs: dict) -> None:
            self.kwargs = kwargs
            self.enabled_toolsets = kwargs.get("enabled_toolsets")
            import model_tools

            self.tools = model_tools.get_tool_definitions(
                enabled_toolsets=kwargs.get("enabled_toolsets"),
                disabled_toolsets=kwargs.get("disabled_toolsets"),
                quiet_mode=True,
            )
            self.valid_tool_names = {item["function"]["name"] for item in self.tools}
            self._todo_store = FakeTodoStore()
            self.native_dispatches: list[str] = []
            self.context_compressor = SimpleNamespace(
                compression_count=0,
                _previous_summary=None,
                _summary_has_user_turn=None,
                _ineffective_compression_count=0,
            )

        def _compress_context(self, messages: list, *args: object, **kwargs: object) -> tuple:
            return messages, "Compressed system prompt"

        def _execute_tool_calls(self, assistant_message: object, messages: list, task_id: str, count: int = 0) -> None:
            self.native_dispatches.extend(
                getattr(getattr(call, "function", None), "name", "todo_list")
                for call in assistant_message.tool_calls
            )
            messages[-1]["tool_calls"][0]["id"] = "normalized_call_1"
            for call in assistant_message.tool_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": "saved",
                        "timestamp": 1791030896.125,
                    }
                )

    class FakeOpenAI:
        instances: list["FakeOpenAI"] = []

        def __init__(self, **kwargs: dict) -> None:
            self.kwargs = kwargs
            self.completion_kwargs: dict | None = None
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self.create_completion)
            )
            self.instances.append(self)

        def create_completion(self, **kwargs: dict) -> object:
            self.completion_kwargs = kwargs
            return SimpleNamespace(choices=[])

    openai_module = ModuleType("openai")
    openai_module.OpenAI = FakeOpenAI
    run_agent_module = ModuleType("run_agent")
    run_agent_module.AIAgent = FakeAIAgent
    agent_package = ModuleType("agent")
    agent_package.__path__ = []
    auxiliary_module = ModuleType("agent.auxiliary_client")
    conversation_loop_module = ModuleType("agent.conversation_loop")
    context_compressor_module = ModuleType("agent.context_compressor")
    micro_compaction_module = ModuleType("agent.micro_compaction")
    tool_executor_module = ModuleType("agent.tool_executor")
    model_tools_module = ModuleType("model_tools")
    source_tool_definitions = [
        {"function": {"name": name}}
        for name in ("todo_list", "tool_search", "tool_describe", "tool_call", "terminal")
    ]
    tool_definition_calls: list[dict] = []

    def get_source_tool_definitions(**kwargs: dict) -> list[dict]:
        tool_definition_calls.append(kwargs)
        return list(source_tool_definitions)

    model_tools_module.get_tool_definitions = get_source_tool_definitions

    def unwrap_reviewed_tool(agent: object, name: str, args: dict) -> tuple[str, dict, str | None]:
        assert agent.enabled_toolsets == ["todo"]
        if name != "tool_call":
            return name, args, None
        calls = args.get("calls", [])
        if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
            return name, args, "invalid bridge scope"
        return calls[0].get("name", ""), calls[0].get("arguments", {}), None

    tool_executor_module._unwrap_tool_search_call = unwrap_reviewed_tool

    def assemble_api_request(_agent: object, **kwargs: object) -> dict:
        return kwargs

    conversation_loop_module.assemble_api_request = assemble_api_request
    reanchor_calls: list[tuple[list, object]] = []
    turn_context_module = ModuleType("agent.turn_context")

    def reanchor_current_turn_user_idx(messages: list, user_message: object) -> int:
        reanchor_calls.append((messages, user_message))
        return next(
            index
            for index, item in enumerate(messages)
            if item.get("role") == "user" and item.get("content") == user_message
        )

    turn_context_module.reanchor_current_turn_user_idx = reanchor_current_turn_user_idx
    monkeypatch.setitem(sys.modules, "openai", openai_module)
    monkeypatch.setitem(sys.modules, "run_agent", run_agent_module)
    monkeypatch.setitem(sys.modules, "agent", agent_package)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", auxiliary_module)
    monkeypatch.setitem(sys.modules, "agent.conversation_loop", conversation_loop_module)
    monkeypatch.setitem(sys.modules, "agent.turn_context", turn_context_module)
    monkeypatch.setitem(sys.modules, "agent.context_compressor", context_compressor_module)
    monkeypatch.setitem(sys.modules, "agent.micro_compaction", micro_compaction_module)
    monkeypatch.setitem(sys.modules, "agent.tool_executor", tool_executor_module)
    monkeypatch.setitem(sys.modules, "model_tools", model_tools_module)
    client = httpx.Client(transport=httpx.MockTransport(broker), trust_env=False)
    adapter = RuntimeAdapter(
        context_data,
        broker_url="http://172.30.0.2:8000",
        capability="fixture-capability",
        workspace_dir=tmp_path,
        broker_client=client,
    )
    assert adapter.context.pending_assistant is not None
    agent = build_native_agent(adapter, workspace_dir=tmp_path)
    live_history = [dict(message) for message in context_data["messages"]]
    live_history[0]["timestamp"] = 1791030000.25
    conversation_loop_module.assemble_api_request(
        agent,
        messages=live_history,
        current_turn_user_idx=0,
        _ext_prefetch_cache="",
        _plugin_user_context="",
        moa_config=None,
        active_system_prompt="system",
        original_user_message="resume this",
        pending_moa_prepared_request=None,
        request_logger=None,
    )
    assert adapter.context.messages[0].content == "Find fixture papers"
    assert adapter.context.native_message_metadata[0].message_index == 0
    assert agent.kwargs["enabled_toolsets"] == ["todo"]
    assert agent.valid_tool_names == {"todo_list"}
    assert [item["function"]["name"] for item in agent.tools] == ["todo_list"]
    assert tool_definition_calls[-1]["skip_tool_search_assembly"] is True
    assert tool_definition_calls[-1]["enabled_toolsets"] == ["todo"]
    assert model_tools_module.get_tool_definitions is get_source_tool_definitions
    with pytest.raises(RuntimeAdapterError):
        context_compressor_module.call_llm(task="title", messages=[])
    route_info: dict = {}
    context_compressor_module.call_llm(
        task="compression",
        messages=[{"role": "user", "content": "bounded summary prompt"}],
        main_runtime={"model": adapter.context.model},
        route_info=route_info,
    )
    assert FakeOpenAI.instances[-1].completion_kwargs == {
        "model": adapter.context.model,
        "messages": [{"role": "user", "content": "bounded summary prompt"}],
        "max_tokens": 2048,
        "stream": False,
        "timeout": 90,
    }
    assert isinstance(
        FakeOpenAI.instances[-1].kwargs["http_client"]._transport,
        BrokerChatCompletionsTransport,
    )
    assert route_info == {"provider": "custom", "model": adapter.context.model}
    assert agent._api_max_retries == 1
    assert agent._auto_recovery_cycles == 0
    assert _pinned_hermes_attempt_count(agent._api_max_retries) == 1
    request_client = agent._create_openai_client({}, reason="request_scope", shared=False)
    assert isinstance(request_client.kwargs["http_client"]._transport, BrokerChatCompletionsTransport)
    call = _pinned_hermes_tool_call("normalized_call_1", "todo_list", "{}")
    assistant = SimpleNamespace(tool_calls=[call])
    conversation = [dict(message) for message in live_history]
    conversation[-1]["timestamp"] = 1791030200.5

    applied_pending = adapter.context.pending_assistant.model_copy(
        update={
            "applied_tool_ids": [
                AppliedToolId(raw_id="raw_call_1", applied_id="normalized_call_1")
            ]
        }
    )
    invalid_calls = [
        _pinned_hermes_tool_call("normalized_call_1", "terminal", "{}"),
        _pinned_hermes_tool_call("other_call", "todo_list", "{}"),
        _pinned_hermes_tool_call("normalized_call_1", "todo_list", '{"todos":[]}'),
        _pinned_hermes_tool_call(
            "normalized_call_1", "todo_list", "{}", provider_data={"surprise": "x"}
        ),
        _pinned_hermes_tool_call(
            "normalized_call_1", "todo_list", "{}", provider_data={"call_id": "other_call"}
        ),
    ]
    unexpected_attribute = _pinned_hermes_tool_call(
        "normalized_call_1", "todo_list", "{}"
    )
    unexpected_attribute.unreviewed = "x"
    invalid_calls.append(unexpected_attribute)
    for invalid_call in invalid_calls:
        adapter.context = adapter.context.model_copy(
            update={"pending_assistant": applied_pending}
        )
        with pytest.raises(RuntimeAdapterError):
            agent._execute_tool_calls(
                SimpleNamespace(tool_calls=[invalid_call]),
                conversation,
                str(context_data["run_id"]),
            )
        assert events == []
        assert agent.native_dispatches == []

    adapter.context = adapter.context.model_copy(update={"pending_assistant": applied_pending})
    agent._execute_tool_calls(assistant, conversation, str(context_data["run_id"]))
    assert len(events) == 2
    before = events[0]["context"]
    assert before["boundary"] == "before_tool"
    assert "pending_assistant" in before, before
    assert before["pending_assistant"]["applied_tool_ids"] == [
        {"raw_id": "raw_call_1", "applied_id": "normalized_call_1"}
    ]
    committed = events[1]["context"]
    assert committed["boundary"] == "tool_committed"
    assert committed["pending_assistant"] is None
    assert committed["messages"][-1]["tool_call_id"] == "raw_call_1"
    assert committed["messages"][-2]["tool_calls"][0]["id"] == "raw_call_1"
    assert committed["messages"][-2]["tool_calls"][0]["function"] == {
        "name": "todo_list",
        "arguments": "{}",
    }
    assert {item["message_index"]: item["timestamp"] for item in committed["native_message_metadata"]} == {
        0: 1791030000.25,
        1: 1791030200.5,
        2: 1791030896.125,
    }
    assert committed["native_message_metadata"][-1] == {
        "message_index": len(committed["messages"]) - 1,
        "timestamp": 1791030896.125,
    }
    assert "timestamp" not in committed["messages"][-1]
    compressed_messages, compressed_system = agent._compress_context(conversation, "before")
    assert compressed_messages is conversation
    assert compressed_system == "Compressed system prompt"
    assert len(reanchor_calls) == 1
    assert reanchor_calls[0] == (conversation, "Find fixture papers")

    # Hermes' enabled_toolsets controls schemas, but its native registry executor
    # still accepts hidden names. The adapter must reject them before checkpoint
    # or dispatch, while retaining only the Todo tool and its reviewed bridge.
    bridge_name = "tool_call"
    bridge_arguments = '{"calls":[{"name":"todo_list","arguments":{}}]}'
    bridge_context = adapter.context.model_dump(mode="json")
    bridge_context["messages"].append({
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "todo-bridge", "type": "function",
                        "function": {"name": bridge_name, "arguments": bridge_arguments}}],
    })
    bridge_context["pending_assistant"] = {
        "turn_id": bridge_context["turn_id"],
        "message_index": len(bridge_context["messages"]) - 1,
        "next_tool_index": 0,
        "applied_tool_ids": [],
    }
    bridge_context["boundary"] = "model_committed"
    adapter.context = RuntimeContextV1.model_validate(bridge_context)
    bridge_call = FakeNativeToolCall("todo-bridge", bridge_name, bridge_arguments)
    agent._execute_tool_calls(
        SimpleNamespace(tool_calls=[bridge_call]),
        adapter.native_history(),
        str(adapter.context.run_id),
    )
    assert agent.native_dispatches[-1] == bridge_name

    safe_context = adapter.context.model_dump(mode="json")
    for name, arguments in (
        ("terminal", '{"command":":"}'),
        ("tool_call", '{"calls":[{"name":"terminal","arguments":{"command":":"}}]}'),
    ):
        next_context = json.loads(json.dumps(safe_context))
        next_context["messages"].append({
            "role": "assistant", "content": None,
            "tool_calls": [{"id": f"hidden-{name}", "type": "function",
                            "function": {"name": name, "arguments": arguments}}],
        })
        next_context["pending_assistant"] = {
            "turn_id": next_context["turn_id"],
            "message_index": len(next_context["messages"]) - 1,
            "next_tool_index": 0,
            "applied_tool_ids": [],
        }
        next_context["boundary"] = "model_committed"
        adapter.context = RuntimeContextV1.model_validate(next_context)
        assistant = SimpleNamespace(tool_calls=[FakeNativeToolCall(f"hidden-{name}", name, arguments)])
        dispatch_count = len(agent.native_dispatches)
        checkpoint_count = len(events)
        with pytest.raises(RuntimeAdapterError):
            agent._execute_tool_calls(assistant, adapter.native_history(), str(adapter.context.run_id))
        assert len(agent.native_dispatches) == dispatch_count
        assert len(events) == checkpoint_count
    assert adapter.context.system_prompt == "Compressed system prompt"
    assert len(events) == 5
    assert events[-1]["context"]["boundary"] == "tool_committed"


def _pinned_hermes_attempt_count(max_retries: int) -> int:
    repo = Path(__file__).resolve().parents[2]
    source_path = repo / ".local/vendor/hermes/agent/conversation_loop.py"
    if not source_path.is_file():
        pytest.skip("pinned Hermes checkout is not present")
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    function = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_run_api_retry_loop"
    )
    calls = {"api": 0}

    class State:
        def __init__(self, max_retries: int) -> None:
            self.retry_count = 0
            self.max_retries = max_retries
            self.api_call_count = 0

    phase_names = (
        "nous_rate_limit_guard",
        "build_api_request",
        "perform_api_call",
        "check_api_response",
        "handle_api_interrupt",
        "handle_api_error",
    )
    namespace: dict = {
        "Optional": Optional,
        "Dict": Dict,
        "Any": Any,
        "_LoopState": State,
    }
    namespace.update({name: object() for name in phase_names})

    def run_phase(phase: object, agent: object, state: State, **extra: object) -> SimpleNamespace:
        if phase is namespace["perform_api_call"]:
            calls["api"] += 1
            state.api_call_count += 1
            raise TimeoutError("fixture broker timeout")
        if phase is namespace["handle_api_error"]:
            state.retry_count += 1
        return SimpleNamespace(action="continue", result=None)

    namespace["_run_phase"] = run_phase
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(source_path), "exec"), namespace)
    agent = SimpleNamespace(_api_max_retries=max_retries)
    state = State(agent._api_max_retries)
    namespace["_run_api_retry_loop"](agent, state)
    return calls["api"]


def test_pinned_hermes_conversation_loop_attempt_limit_semantics() -> None:
    assert _pinned_hermes_attempt_count(1) == 1
    assert _pinned_hermes_attempt_count(0) == 0
