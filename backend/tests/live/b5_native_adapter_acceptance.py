"""Native Hermes adapter acceptance checks for the pinned worker image.

Run inside the candidate image with its actual /opt/hermes and /opt/scientist
trees on sys.path. The only substituted boundary is the broker HTTP client;
Hermes, the agent constructor, continuation loop, tool executor, and compressor
are imported from the immutable image.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import httpx

import scientist.runtime_adapter as runtime_adapter_module
from scientist.contracts import PlanSpec
from scientist.runtime_adapter import BrokerChatCompletionsTransport, RuntimeAdapter
from scientist.runtime_contracts import RuntimeContextV1
from runtime import entrypoint


IMAGE_DIGEST, RUNTIME_COMMIT = sys.argv[1], sys.argv[2]
MODEL_OUTPUT = {
    "id": "chatcmpl-native-fixture",
    "object": "chat.completion",
    "created": 1,
    "model": "fixture",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Native fixture synthesis."},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
}



def remaining_tokens(run: dict) -> int:
    # Same formula as the trusted server's scientist.limits.remaining_tokens (ADR-012); limits.py is
    # server-only and not shipped in the worker image this harness runs in.
    return max(0, int(run["token_limit"]) - int(run["usage_tokens"]) - int(run["reserved_tokens"]))

def _checkpoint_ack(payload: dict[str, Any]) -> dict[str, Any]:
    context = payload["context"]
    canonical = json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    checkpoint_id = str(uuid.uuid4())
    revision = payload["expected_checkpoint_revision"] + 1
    manifest = {
        "schema_version": 1,
        "run_id": context["run_id"],
        "revision": revision,
        "plan_digest": context["plan_digest"],
        "runtime_commit": context["runtime_commit"],
        "image_digest": context["image_digest"],
        "skills_digest": context["skills_digest"],
        "context": {
            "project_id": context["project_id"],
            "key": f"runs/{context['run_id']}/{checkpoint_id}",
            "sha256": hashlib.sha256(canonical).hexdigest(),
            "size": len(json.dumps(context, separators=(",", ":"), ensure_ascii=False).encode()),
            "content_type": "application/json",
        },
        "workspace": [],
        "environment_digest": context["environment_digest"],
        "operation_ids": [item["operation_id"] for item in context["operation_mappings"]],
    }
    return {
        "schema_version": 1,
        "boundary_id": payload["boundary_id"],
        "checkpoint_id": checkpoint_id,
        "checkpoint_revision": revision,
        "manifest": manifest,
    }


def _context(*, pending_todo: bool) -> dict[str, Any]:
    provider_id = uuid.uuid4()
    run_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    input_digest = "a" * 64
    plan = PlanSpec(
        input_snapshot_digest=input_digest,
        provider_id=provider_id,
        model="fixture",
        stages=["search"],
        allowed_ops=["llm", "search"],
        data_recipients=["https://research.example"],
        packages=[],
        token_limit=100_000,
        elapsed_limit_ms=60_000,
    )
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Continue the fixture research."}
    ]
    pending = None
    boundary = "before_model"
    todo = {"todos": [], "revision": 0}
    if pending_todo:
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "raw_todo_1",
                        "type": "function",
                        "function": {
                            "name": "todo_list",
                            "arguments": json.dumps(
                                {"todos": [{"id": "native-task", "content": "Resume saved work", "status": "pending"}]},
                                separators=(",", ":"),
                            ),
                        },
                    }
                ],
            }
        )
        pending = {
            "turn_id": str(turn_id),
            "message_index": 1,
            "next_tool_index": 0,
            "applied_tool_ids": [],
        }
        boundary = "model_committed"
    return RuntimeContextV1.model_validate(
        {
            "schema_version": 1,
            "run_id": str(run_id),
            "project_id": str(uuid.uuid4()),
            "generation": 1,
            "revision": 3,
            "input_snapshot_digest": input_digest,
            "plan_digest": hashlib.sha256(
                json.dumps(plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "runtime_commit": RUNTIME_COMMIT,
            "image_digest": IMAGE_DIGEST,
            "skills_digest": "c" * 64,
            "environment_digest": "d" * 64,
            "provider_id": str(provider_id),
            "provider_endpoint": "https://research.example",
            "model": "fixture",
            "plan": plan.model_dump(mode="json"),
            "turn_id": str(turn_id),
            "system_prompt": "Use only the saved research transcript.",
            "messages": messages,
            "native_message_metadata": [
                {"message_index": 0, "timestamp": 1791030000.25}
            ],
            "current_turn_user_index": 0,
            "native_turn_timestamp": 1791030000.25,
            "todo": todo,
            "compacted_context": None,
            "boundary": boundary,
            "pending_assistant": pending,
            "operation_mappings": [],
            "operation_sequence": 0,
            "workspace_manifest": [],
            # Trusted snapshot as the server computes it. This harness has no DB run row, so the inputs are the
            # fixture plan's own token_limit with zero usage and zero held reservations (a fresh run).
            "budget_remaining_tokens": remaining_tokens(
                {"token_limit": plan.token_limit, "usage_tokens": 0, "reserved_tokens": 0}),
        }
    ).model_dump(mode="json")


class BrokerFixture:
    def __init__(self, *, failure: str | None = None) -> None:
        self.failure = failure
        self.project_id: str | None = None
        self.boundaries: list[dict[str, Any]] = []
        self.operations: list[dict[str, Any]] = []
        self.results: dict[str, bytes] = {}
        self.model_requests: list[dict[str, Any]] = []
        self.transport_error: str | None = None
        self.dispatched_body: dict[str, Any] | None = None
        self.dispatch_error: str | None = None
        self.native_client_transport: str | None = None
        self.native_disable_streaming: Any = None
        self.native_request: dict[str, Any] | None = None
        self.wire_errors: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Worker-Capability") == "native-fixture-capability"
        path = request.url.path
        if path == "/control/boundary":
            payload = json.loads(request.content)
            self.boundaries.append(payload)
            return httpx.Response(200, json=_checkpoint_ack(payload))
        if path == "/effects":
            payload = json.loads(request.content)
            self.operations.append(payload)
            operation = payload
            body = operation["payload"]
            self.model_requests.append(body)
            if self.failure == "connect":
                raise httpx.ConnectError("controlled broker failure", request=request)
            if self.failure == "budget":
                return httpx.Response(409, json={"detail": {"code": "budget_exhausted"}})
            if self.failure == "status":
                return httpx.Response(503, json={"detail": "fixture unavailable"})
            raw = json.dumps(MODEL_OUTPUT, separators=(",", ":")).encode()
            result_key = f"results/{operation['operation_id']}"
            self.results[operation["operation_id"]] = raw
            return httpx.Response(
                200,
                json={
                    "operation_id": operation["operation_id"],
                    "state": "committed",
                    "result": {
                        "project_id": self.project_id,
                        "key": result_key,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "size": len(raw),
                        "content_type": "application/json",
                    },
                    "usage_tokens": 11,
                },
            )
        if path.startswith("/effects/") and path.endswith("/result"):
            operation_id = path.split("/")[-2]
            return httpx.Response(
                200,
                content=self.results[operation_id],
                headers={"content-type": "application/json"},
            )
        raise AssertionError(f"unexpected broker request: {request.method} {path}")


def _write_bootstrap(root: Path, context: dict[str, Any]) -> tuple[Path, Path, Path]:
    bootstrap = root / "bootstrap"
    workspace = root / "workspace"
    readiness = root / "ready"
    bootstrap.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    readiness.write_text("ready", encoding="utf-8")
    (bootstrap / "context.json").write_text(json.dumps(context), encoding="utf-8")
    (bootstrap / "workspace.json").write_text("[]", encoding="utf-8")
    (bootstrap / "metadata.json").write_text(
        json.dumps({"schema_version": 1, "checkpoint_revision": 0}), encoding="utf-8"
    )
    capability = root / "capability"
    capability.write_text("native-fixture-capability", encoding="utf-8")
    capability.chmod(0o600)
    return bootstrap, workspace, readiness


def _run_worker_case(
    *, pending_todo: bool, failure: str | None = None, exercise_compression: bool = False
) -> tuple[BrokerFixture, Exception | None]:
    fixture = BrokerFixture(failure=failure)

    def checked_broker(request: httpx.Request) -> httpx.Response:
        try:
            return fixture(request)
        except Exception as exc:
            fixture.transport_error = repr(exc)
            raise

    broker_client = httpx.Client(transport=httpx.MockTransport(checked_broker), trust_env=False)
    original_init = RuntimeAdapter.__init__
    original_build = entrypoint.build_native_agent
    original_dispatch = RuntimeAdapter.dispatch_chat_completion
    original_handle = BrokerChatCompletionsTransport.handle_request
    original_wire = runtime_adapter_module._wire_messages

    def with_fixture(self: RuntimeAdapter, *args: Any, **kwargs: Any) -> None:
        kwargs["broker_client"] = broker_client
        original_init(self, *args, **kwargs)

    def trace_dispatch(
        self: RuntimeAdapter, body: dict[str, Any], *, purpose: str = "model"
    ) -> tuple[bytes, str]:
        fixture.dispatched_body = body
        try:
            return original_dispatch(self, body, purpose=purpose)
        except Exception as exc:
            fixture.dispatch_error = f"{exc!r}; cause={exc.__cause__!r}"
            raise

    def trace_handle(
        self: BrokerChatCompletionsTransport, request: httpx.Request
    ) -> httpx.Response:
        try:
            fixture.native_request = json.loads(request.read())
            return original_handle(self, request)
        except Exception as exc:
            fixture.transport_error = (
                f"{exc!r}; method={request.method}; path={request.url.path}; "
                f"content-length={request.headers.get('content-length')}"
            )
            raise

    def trace_wire(messages: Any) -> list[dict[str, Any]]:
        try:
            return original_wire(messages)
        except Exception as exc:
            cause = exc.__cause__
            fixture.wire_errors.append(
                f"{exc!r}; cause={cause!r}; rows="
                + repr(
                    [
                        {
                            key: (type(value).__name__, repr(value)[:120])
                            for key, value in (item.items() if isinstance(item, dict) else [])
                        }
                        for item in messages
                    ]
                )
            )
            raise

    def build_with_native_compression(adapter: RuntimeAdapter, *, workspace_dir: Path) -> Any:
        agent = original_build(adapter, workspace_dir=workspace_dir)
        native_tool_names = sorted(getattr(agent, "valid_tool_names", set()))
        assert native_tool_names == ["todo_list"], (
            "native Hermes model schemas exceed the reviewed Todo-only surface: "
            f"{native_tool_names!r}"
        )
        fixture.native_tool_names = native_tool_names
        native_client = getattr(agent, "client", None)
        http_client = getattr(native_client, "_client", None)
        native_transport = getattr(http_client, "_transport", None)
        fixture.native_client_transport = type(native_transport).__name__
        fixture.native_disable_streaming = getattr(agent, "_disable_streaming", None)
        if exercise_compression:
            native_history = adapter.native_history()
            native_history.extend(
                [
                    {"role": "user", "content": f"Historical question {index}."}
                    if index % 2 == 0
                    else {"role": "assistant", "content": "Historical answer."}
                    for index in range(4)
                ]
            )
            native_history.append(
                {"role": "assistant", "content": ("fixture evidence " * 7000)}
            )
            native_history.extend(
                [
                    {"role": "user", "content": f"Recent question {index}."}
                    if index % 2 == 0
                    else {"role": "assistant", "content": "Recent answer."}
                    for index in range(22)
                ]
            )
            native_history.append({"role": "user", "content": "Keep the key evidence."})
            # Mirror the state set by Hermes' real turn-context builder before
            # compression runs inside a native continuation.
            agent._current_turn_timestamp = adapter.context.native_turn_timestamp
            compressed = agent._compress_context(
                native_history,
                adapter.context.system_prompt,
                approx_tokens=9000,
                force=True,
            )
            assert isinstance(compressed, tuple) and isinstance(compressed[0], list), (
                "native compression helper did not return the canonical transcript"
            )
            assert any(
                body.get("max_tokens", body.get("max_completion_tokens", 0)) <= 2048
                for body in fixture.model_requests
            ), "native compression helper escaped the 2048-token cap"
        return agent

    RuntimeAdapter.__init__ = with_fixture
    RuntimeAdapter.dispatch_chat_completion = trace_dispatch
    BrokerChatCompletionsTransport.handle_request = trace_handle
    runtime_adapter_module._wire_messages = trace_wire
    entrypoint.build_native_agent = build_with_native_compression
    try:
        with tempfile.TemporaryDirectory(prefix="b5-native-") as temporary:
            root = Path(temporary)
            context = _context(pending_todo=pending_todo)
            fixture.project_id = context["project_id"]
            bootstrap, workspace, readiness = _write_bootstrap(root, context)
            capability = root / "capability"
            os.environ.update(
                {
                    "SCIENTIST_BOOTSTRAP_DIR": str(bootstrap),
                    "SCIENTIST_READINESS_FILE": str(readiness),
                    "SCIENTIST_WORKSPACE": str(workspace),
                    "SCIENTIST_BROKER_URL": "http://172.30.0.2:8000",
                    "SCIENTIST_CAPABILITY_FILE": str(capability),
                }
            )
            try:
                entrypoint.run_worker()
            except Exception as exc:
                # Failure cases deliberately inject one broker error.
                if failure is None or failure == "budget":  # a budget wait must be a clean exit, never an exception
                        raise RuntimeError(
                        "worker failed; "
                        f"broker-fixture exception={fixture.transport_error!r}; "
                        f"boundaries={len(fixture.boundaries)}; "
                        f"effects={len(fixture.operations)}; "
                        f"model-requests={len(fixture.model_requests)}; "
                        f"native-transport={fixture.native_client_transport!r}; "
                        f"disable-streaming={fixture.native_disable_streaming!r}; "
                        f"dispatched-stream={(fixture.dispatched_body or {}).get('stream')!r}; "
                        f"dispatch-error={fixture.dispatch_error!r}; "
                        f"request-keys={sorted(fixture.native_request or {})!r}; "
                        f"request-tools={(fixture.native_request or {}).get('tools')!r}; "
                        f"native-messages={(fixture.native_request or {}).get('messages')!r}; "
                        f"wire-errors={fixture.wire_errors!r}"
                    ) from exc
                return fixture, exc
    finally:
        RuntimeAdapter.__init__ = original_init
        RuntimeAdapter.dispatch_chat_completion = original_dispatch
        BrokerChatCompletionsTransport.handle_request = original_handle
        runtime_adapter_module._wire_messages = original_wire
        entrypoint.build_native_agent = original_build
        broker_client.close()
    return fixture, None


def _assert_native_continuation_and_failure_guard() -> None:
    fixture, error = _run_worker_case(pending_todo=True)
    assert error is None, f"native continuation failed: {error!r}"
    assert fixture.model_requests, "native continuation never dispatched a model request"
    assert len(fixture.model_requests) == 1, "native successful turn issued unexpected model attempts"
    assert all(body.get("stream") is not True for body in fixture.model_requests)
    assert any(
        boundary["context"]["boundary"] == "before_tool"
        and boundary["context"]["pending_assistant"]["applied_tool_ids"]
        for boundary in fixture.boundaries
    ), "native Todo suffix was not checkpointed with its applied tool identity before execution"
    final = next(
        boundary["context"]
        for boundary in reversed(fixture.boundaries)
        if boundary["context"]["boundary"] == "final"
    )
    users = [message for message in final["messages"] if message["role"] == "user"]
    assert len(users) == 1 and users[0]["content"] == "Continue the fixture research.", (
        "saved-turn continuation admitted an extra user message"
    )
    assert final["current_turn_user_index"] == 0
    assert any(message.get("role") == "tool" for message in final["messages"]), (
        "native Todo result was not preserved in primary history"
    )
    assert final["todo"]["todos"][0]["id"] == "native-task", "native Todo state was not captured"
    assert final["messages"][-1].get("content") == "Native fixture synthesis."

    compressed, error = _run_worker_case(
        pending_todo=False, exercise_compression=True
    )
    assert error is None, f"native direct compression helper failed: {error!r}"
    compression_boundaries = [
        boundary["context"] for boundary in compressed.boundaries
        if boundary["context"].get("compacted_context") is not None
    ]
    assert compression_boundaries, "native compression state was not checkpointed"
    assert any(
        body.get("max_tokens", body.get("max_completion_tokens", 0)) <= 2048
        for body in compressed.model_requests
    ), "compression output cap was not present in actual broker request"
    assert len(compressed.model_requests) == 2, (
        "compression and continuation should each dispatch once through the broker"
    )

    waited, error = _run_worker_case(pending_todo=False, failure="budget")
    assert error is None, f"budget wait did not end as a clean worker exit (exit 0): {error!r}"
    assert len(waited.operations) == 1 and len(waited.boundaries) == 1, (
        f"budget wait must stop after one effect and one boundary: "
        f"effects={len(waited.operations)} boundaries={len(waited.boundaries)}"
    )
    assert len(waited.model_requests) == 1 and all(
        boundary["context"]["boundary"] != "final" for boundary in waited.boundaries
    ), "budget wait retried the effect or checkpointed a final transcript"

    failed, error = _run_worker_case(pending_todo=False, failure="connect")
    assert error is not None, "injected model failure was not surfaced"
    assert len(failed.model_requests) == 1, (
        "one-total-attempt Hermes request retried after the broker failure"
    )
    assert failed.boundaries and all(
        boundary["context"]["boundary"] != "final" for boundary in failed.boundaries
    ), "failed/incomplete native envelope was incorrectly checkpointed as final"

    failed_compression, error = _run_worker_case(
        pending_todo=False, failure="status", exercise_compression=True
    )
    assert error is not None, "injected compression broker failure was not surfaced"
    assert len(failed_compression.model_requests) == 1, (
        "native compressor retried or fell back after a single failed auxiliary request"
    )
    assert failed_compression.boundaries and all(
        boundary["context"]["boundary"] != "final"
        for boundary in failed_compression.boundaries
    ), "compression failure incorrectly finalized a partial transcript"


def main() -> None:
    assert Path("/opt/hermes/agent/conversation_loop.py").is_file(), (
        "pinned Hermes source is absent; native acceptance cannot be skipped"
    )
    assert Path("/opt/scientist/scientist/runtime_adapter.py").is_file()
    _assert_native_continuation_and_failure_guard()
    print(
        json.dumps(
            {
                "status": "PASS",
                "image_digest": IMAGE_DIGEST,
                "checks": [
                    "pinned Hermes import and real agent constructor through entrypoint",
                    "native model-visible tool schemas contain only the direct Todo handler",
                    "pending Todo suffix tool execution and raw primary-history preservation",
                    "saved continuation without duplicate user admission",
                    "single actual model attempt, successful and broker-failure paths",
                    "single direct compression attempt without native auxiliary retry/fallback",
                    "failed turn rejected as final checkpoint",
                    "broker budget_exhausted 409 ends the worker cleanly after one effect and one boundary",
                ],
                "compression_direct_helper": "PASS",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
