"""Broker-backed Chat Completions transport for the pinned Hermes worker."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from scientist.contracts import OperationRequest, OperationResult
from scientist.model_payload import (
    ChatMessage,
    ModelPayloadError,
    build_chat_completion_body,
    llm_input_reserve,
)
from scientist.runtime_contracts import (
    AppliedToolId,
    BoundaryAck,
    BoundaryRequest,
    CompactedContext,
    MicroCompactionState,
    OperationMapping,
    NativeMessageMetadata,
    PendingAssistant,
    RuntimeContextV1,
    TodoSnapshot,
    WorkspaceFile,
    canonical_bytes,
    operation_fingerprint,
    validate_workspace_path,
)

_MAX_PROVIDER_RESPONSE = 2 * 1024 * 1024
_MAX_BROKER_RESPONSE = 3 * 1024 * 1024
_DEFAULT_MODEL_OUTPUT_TOKENS = 2048
_COMPRESSION_MAX_OUTPUT_TOKENS = 2048
_REVIEWED_NATIVE_TODO_TOOL_NAMES = frozenset({"todo_list", "todo"})
_REVIEWED_NATIVE_TODO_BRIDGE = "tool_call"
_SUPPORTED_CONTROLS = {
    "temperature",
    "top_p",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "parallel_tool_calls",
    "logprobs",
    "top_logprobs",
    "tools",
    "tool_choice",
}


class RuntimeAdapterError(RuntimeError):
    """A fail-closed worker-side runtime error."""


class EffectUnresolved(RuntimeAdapterError):
    """An operation has no known result and must be reconciled by the controller."""


class EffectDenied(RuntimeAdapterError):
    """The broker rejected an effect; no provider bytes may be returned."""


class BudgetExhausted(EffectDenied):
    """The broker refused the reservation and recorded the owner budget decision."""


class BoundaryNotDurable(RuntimeAdapterError):
    """A checkpoint could not be acknowledged, so dispatch must not proceed."""


def _without_output_limit(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "max_output_tokens"}


def _model_mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    raise RuntimeAdapterError("invalid runtime context")


def _validate_native_todo_call(agent: Any, call: Any) -> None:
    """Allow only pinned Todo handlers, including a scope-checked Todo bridge.

    At RUNTIME_COMMIT the native schema exposes ``todo_list`` (with ``todo`` as
    its legacy alias). Hermes' registry executor can still dispatch arbitrary
    names passed directly to ``_execute_tool_calls``, regardless of those
    schemas, so every call is checked before the native executor is entered.
    """
    value = _model_mapping(call)
    function = value.get("function")
    if not isinstance(function, dict):
        raise RuntimeAdapterError("native tool call has no function")
    name = function.get("name")
    if name not in _REVIEWED_NATIVE_TODO_TOOL_NAMES and name != _REVIEWED_NATIVE_TODO_BRIDGE:
        raise RuntimeAdapterError("native tool call is outside the reviewed Todo surface")
    raw_arguments = function.get("arguments")
    if isinstance(raw_arguments, str):
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise RuntimeAdapterError("native Todo arguments are invalid") from exc
    else:
        arguments = raw_arguments
    if not isinstance(arguments, dict):
        raise RuntimeAdapterError("native Todo arguments are invalid")

    try:
        # This pinned Hermes helper canonicalizes the legacy alias and, for the
        # Tool Search bridge, validates the underlying name against the agent's
        # enabled toolset and the concrete deferred-tool schema.
        from agent.tool_executor import _unwrap_tool_search_call

        resolved_name, _resolved_arguments, scope_block = _unwrap_tool_search_call(
            agent, name, arguments
        )
    except Exception as exc:
        raise RuntimeAdapterError("native Todo scope validation failed") from exc
    if scope_block is not None or resolved_name not in _REVIEWED_NATIVE_TODO_TOOL_NAMES:
        raise RuntimeAdapterError("native tool call is outside the reviewed Todo scope")


def _wire_messages(messages: Any) -> list[dict[str, Any]]:
    """Project Hermes transcript rows onto the strict platform wire profile.

    Hermes adds timestamps and persistence/display bookkeeping to live rows.
    Those are not Chat Completions fields and must not be forwarded as arbitrary
    provider input.
    """
    if not isinstance(messages, list):
        raise RuntimeAdapterError("native messages must be a list")
    allowed = {"role", "content", "name", "tool_call_id", "tool_calls"}
    projected: list[dict[str, Any]] = []
    for message in messages:
        value = _model_mapping(message) if hasattr(message, "model_dump") else message
        if not isinstance(value, dict):
            raise RuntimeAdapterError("native message must be an object")
        wire = {
            key: value[key]
            for key in allowed
            if key in value and (key == "content" or value[key] is not None)
        }
        if wire.get("role") == "assistant" and "content" not in wire:
            wire["content"] = None
        try:
            parsed = ChatMessage.model_validate(wire)
        except ValidationError as exc:
            raise RuntimeAdapterError("native message is outside approved wire profile") from exc
        projected.append(parsed.model_dump(mode="json", exclude_unset=True))
    return projected


def _native_message_metadata(messages: list[Any]) -> list[NativeMessageMetadata]:
    """Keep native transcript timestamps outside the strict provider message schema."""
    metadata: list[NativeMessageMetadata] = []
    for index, message in enumerate(messages):
        value = _model_mapping(message)
        timestamp = value.get("timestamp")
        if timestamp is None:
            continue
        try:
            metadata.append(NativeMessageMetadata(message_index=index, timestamp=timestamp))
        except (ValidationError, ValueError, TypeError) as exc:
            raise RuntimeAdapterError("native message timestamp is invalid") from exc
    return metadata


def _compacted_context(compressor: Any) -> CompactedContext:
    return CompactedContext(
        compression_count=getattr(compressor, "compression_count", 0),
        previous_summary=getattr(compressor, "_previous_summary", None),
        summary_has_user_turn=getattr(compressor, "_summary_has_user_turn", None),
        ineffective_compression_count=getattr(compressor, "_ineffective_compression_count", 0),
        micro=MicroCompactionState(
            passes=getattr(compressor, "_micro_compact_passes", 0),
            tokens_saved_total=getattr(compressor, "_micro_compact_tokens_saved_total", 0),
            turns_since_pass=getattr(compressor, "_micro_compact_turns_since_pass", 0),
            cursor=getattr(compressor, "_micro_compact_cursor", 0),
            rolling_summary=getattr(compressor, "_micro_compact_rolling_summary", ""),
            consecutive_failures=getattr(compressor, "_micro_compact_consecutive_failures", 0),
            last_failure_cursor=getattr(compressor, "_micro_compact_last_failure_cursor", -1),
            enabled=getattr(compressor, "_micro_compact_enabled", False),
            defrag_threshold_tokens=getattr(compressor, "_micro_compact_defrag_threshold_tokens", 2000),
        ),
    )


def _json_object(data: bytes, *, maximum: int, label: str) -> dict[str, Any]:
    if len(data) > maximum:
        raise RuntimeAdapterError(f"{label} exceeds its byte limit")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeAdapterError(f"invalid {label} JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeAdapterError(f"{label} must be a JSON object")
    return value


def _safe_json_bytes(response: httpx.Response, *, maximum: int) -> bytes:
    raw = response.content
    if len(raw) > maximum:
        raise RuntimeAdapterError("broker response exceeds its byte limit")
    return raw


class RuntimeAdapter:
    """Own operation identities, checkpoints, and the private broker client.

    `context` is the effective, fenced generation supplied by the controller. Its
    existing operation mappings retain the original generation and payload. A
    replay request is rebound only in the generation field before POST /effects;
    the stable fingerprint remains generation-independent.
    """

    def __init__(
        self,
        context: RuntimeContextV1 | dict[str, Any],
        *,
        broker_url: str,
        capability: str,
        workspace_dir: Path,
        broker_client: httpx.Client | None = None,
        checkpoint_revision: int = 0,
        operation_purpose: Literal["model", "compression"] = "model",
    ) -> None:
        self.context = RuntimeContextV1.model_validate(_model_mapping(context))
        parsed = urlsplit(broker_url)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise RuntimeAdapterError("broker URL must be a bare private HTTP origin")
        try:
            import ipaddress

            address = ipaddress.ip_address(parsed.hostname)
        except ValueError as exc:
            raise RuntimeAdapterError("broker URL must use a numeric private address") from exc
        if not address.is_private or address.is_loopback or address.is_link_local:
            raise RuntimeAdapterError("broker address is outside the worker bridge range")
        if not capability or len(capability) > 4096:
            raise RuntimeAdapterError("worker capability is missing or oversized")
        if checkpoint_revision < 0:
            raise RuntimeAdapterError("invalid checkpoint revision")
        self.broker_url = broker_url.rstrip("/")
        self._capability = capability
        self.workspace_dir = workspace_dir
        self.checkpoint_revision = checkpoint_revision
        self.operation_purpose = operation_purpose
        # Charges of operations allocated by this generation against the bootstrap snapshot:
        # the full reservation until committed, then the broker-reported usage.
        self._charges: dict[str, int] = {}
        # Set once the broker records a budget wait; no further effects this generation.
        self.budget_exhausted = False
        # Effects posted this generation whose outcome is not known to be committed or
        # refused before any journal row (budget 409). A clean budget pause requires none.
        self.unresolved_effects: set[str] = set()
        self._owns_broker_client = broker_client is None
        self._broker = broker_client or httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(15.0, connect=2.0),
        )

    def close(self) -> None:
        if self._owns_broker_client:
            self._broker.close()

    def __repr__(self) -> str:
        return f"RuntimeAdapter(run_id={self.context.run_id}, generation={self.context.generation}, capability=<redacted>)"

    def transport(self, *, purpose: Literal["model", "compression"] | None = None) -> "BrokerChatCompletionsTransport":
        return BrokerChatCompletionsTransport(self, purpose=purpose or self.operation_purpose)

    def _workspace_files(self) -> list[WorkspaceFile]:
        root = self.workspace_dir
        if not root.is_dir() or root.is_symlink():
            raise RuntimeAdapterError("workspace root is unavailable or unsafe")
        files: list[WorkspaceFile] = []
        total = 0
        for directory, names, filenames in os.walk(root, topdown=True, followlinks=False):
            current = Path(directory)
            for name in tuple(names):
                path = current / name
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise RuntimeAdapterError("workspace contains a non-directory path")
            for name in filenames:
                path = current / name
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
                    raise RuntimeAdapterError("workspace contains a non-regular file")
                relative = path.relative_to(root).as_posix()
                validate_workspace_path(relative)
                if info.st_size > 20 * 1024 * 1024:
                    raise RuntimeAdapterError("workspace file exceeds its byte limit")
                content = path.read_bytes()
                if len(content) != info.st_size:
                    raise RuntimeAdapterError("workspace file changed while snapshotting")
                total += len(content)
                if total > 64 * 1024 * 1024 or len(files) >= 1024:
                    raise RuntimeAdapterError("workspace snapshot exceeds its limit")
                files.append(
                    WorkspaceFile(
                        path=relative,
                        sha256=hashlib.sha256(content).hexdigest(),
                        size=len(content),
                        data_base64=base64.b64encode(content).decode("ascii"),
                    )
                )
        files.sort(key=lambda item: item.path)
        return files

    def sync_primary_history(
        self,
        messages: list[Any],
        *,
        current_turn_user_index: int | None = None,
        native_turn_timestamp: int | float | str | None = None,
    ) -> None:
        """Checkpoint canonical Hermes messages and their persistence timestamps."""
        self.context = self.context.model_copy(
            update={
                "messages": [ChatMessage.model_validate(item) for item in _wire_messages(messages)],
                "native_message_metadata": _native_message_metadata(messages),
                "current_turn_user_index": current_turn_user_index,
                "native_turn_timestamp": native_turn_timestamp,
            }
        )

    def native_history(self) -> list[dict[str, Any]]:
        """Rehydrate the canonical native transcript with its separate timestamps."""
        messages = [item.model_dump(mode="json", exclude_unset=True) for item in self.context.messages]
        for item in self.context.native_message_metadata:
            timestamp = item.timestamp
            if isinstance(timestamp, str):
                try:
                    timestamp = datetime.fromisoformat(timestamp).timestamp()
                except ValueError as exc:
                    raise RuntimeAdapterError("native checkpoint timestamp is invalid") from exc
            messages[item.message_index]["timestamp"] = timestamp
        return messages

    def _checkpoint(self, boundary: str) -> BoundaryAck:
        context = self.context.model_copy(update={"boundary": boundary})
        workspace = self._workspace_files()
        context = context.model_copy(
            update={
                "workspace_manifest": [
                    {"path": item.path, "sha256": item.sha256, "size": item.size} for item in workspace
                ]
            }
        )
        try:
            context_data = context.model_dump(mode="json")
            context_data["messages"] = [
                message.model_dump(mode="json", exclude_unset=True)
                for message in context.messages
            ]
            context = RuntimeContextV1.model_validate(context_data)
            request = BoundaryRequest(
                schema_version=1,
                boundary_id=uuid.uuid4(),
                expected_checkpoint_revision=self.checkpoint_revision,
                context=context,
                workspace=workspace,
            )
        except (ValidationError, ValueError) as exc:
            raise BoundaryNotDurable("runtime boundary failed schema validation") from exc
        # Retry only the same idempotent boundary bytes. A new UUID or any changed
        # context after an ambiguous response would violate the durable contract.
        request_bytes = canonical_bytes(request.model_dump(mode="json", exclude_unset=True))
        last_error: Exception | None = None
        for _ in range(2):
            try:
                response = self._broker.post(
                    f"{self.broker_url}/control/boundary",
                    content=request_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Worker-Capability": self._capability,
                    },
                )
                response.raise_for_status()
                ack = BoundaryAck.model_validate(_json_object(response.content, maximum=_MAX_BROKER_RESPONSE, label="boundary acknowledgement"))
                if ack.boundary_id != request.boundary_id:
                    raise BoundaryNotDurable("boundary acknowledgement identity mismatch")
                if ack.checkpoint_revision != request.expected_checkpoint_revision + 1:
                    raise BoundaryNotDurable("unexpected checkpoint sequence")
                self.context = context
                self.checkpoint_revision = ack.checkpoint_revision
                return ack
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
            except (httpx.HTTPStatusError, ValidationError, ValueError, RuntimeAdapterError) as exc:
                raise BoundaryNotDurable("boundary acknowledgement was not valid") from exc
        raise BoundaryNotDurable("boundary acknowledgement unavailable") from last_error

    def _next_operation(
        self,
        *,
        purpose: Literal["model", "compression"],
        payload: dict[str, Any],
        reserve_tokens: int,
    ) -> tuple[OperationRequest, OperationMapping]:
        sequence = self.context.operation_sequence
        # An identical request at the last uncommitted boundary is a recovery
        # replay. Reuse its id, payload and reservation instead of creating a second
        # spend. The output limit is excluded from the comparison because it follows
        # the per-generation budget snapshot (ADR-012); the journaled value wins.
        for mapping in reversed(self.context.operation_mappings):
            if (
                mapping.turn_id == self.context.turn_id
                and mapping.purpose == purpose
                and mapping.model_sequence == sequence - 1
                and _without_output_limit(mapping.request.payload) == _without_output_limit(payload)
            ):
                rebound = mapping.request.model_copy(update={"generation": self.context.generation})
                if operation_fingerprint(rebound) != mapping.payload_hash:
                    raise RuntimeAdapterError("replayed operation fingerprint changed")
                return rebound, mapping
        operation_id = str(
            uuid.uuid5(self.context.run_id, f"{self.context.turn_id}:{purpose}:{sequence}")
        )
        request = OperationRequest(
            run_id=self.context.run_id,
            generation=self.context.generation,
            operation_id=operation_id,
            kind="llm",
            payload=payload,
            reserve_tokens=reserve_tokens,
        )
        mapping = OperationMapping(
            operation_id=operation_id,
            turn_id=self.context.turn_id,
            purpose=purpose,
            model_sequence=sequence,
            tool_call_id=None,
            request=request,
            payload_hash=operation_fingerprint(request),
        )
        self.context = self.context.model_copy(
            update={
                "operation_mappings": [*self.context.operation_mappings, mapping],
                "operation_sequence": sequence + 1,
            }
        )
        self._charges[operation_id] = reserve_tokens
        self.context = RuntimeContextV1.model_validate(self.context.model_dump(mode="json"))
        return request, mapping

    def dispatch_chat_completion(
        self,
        request_body: dict[str, Any],
        *,
        purpose: Literal["model", "compression"] = "model",
    ) -> tuple[bytes, str]:
        if request_body.get("stream") is True:
            raise RuntimeAdapterError("streaming is disabled for checkpointed Chat Completions")
        if request_body.get("model") != self.context.model:
            raise RuntimeAdapterError("request model differs from the approved runtime model")
        messages = _wire_messages(request_body.get("messages"))
        max_tokens = request_body.get("max_tokens", request_body.get("max_completion_tokens"))
        if max_tokens is None:
            max_tokens = _DEFAULT_MODEL_OUTPUT_TOKENS
        elif type(max_tokens) is not int or max_tokens <= 0:
            raise RuntimeAdapterError("invalid native model output limit")
        if self.context.budget_remaining_tokens is None:
            raise RuntimeAdapterError("runtime context has no trusted budget snapshot")
        if self.budget_exhausted:
            raise BudgetExhausted("run is waiting for an owner budget decision")
        if purpose == "compression":
            request_body = {**request_body, "stream": False}
        controls = {key: value for key, value in request_body.items() if key in _SUPPORTED_CONTROLS}
        try:
            input_reserve = llm_input_reserve(messages, **controls)
            # ADR-012: output = min(2048, snapshot - this generation's reservations - the
            # broker's input reserve), for default and explicit limits (explicit values
            # above it are clamped, as the 2048 cap already was). Below 1 nothing can
            # be clamped lawfully: the unclamped request goes to the broker, whose
            # atomic reservation check records the owner budget wait (zero provider
            # calls). Never send 0 and never force 1.
            allowance = self.context.budget_remaining_tokens - sum(self._charges.values()) - input_reserve
            max_tokens = min(max_tokens, _DEFAULT_MODEL_OUTPUT_TOKENS)
            if allowance >= 1:
                max_tokens = min(max_tokens, allowance)
            canonical_body = build_chat_completion_body(
                self.context.model,
                messages,
                max_tokens,
                **controls,
            )
            normalized_body = canonical_body
        except (ModelPayloadError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeAdapterError("native model request is outside the approved wire profile") from exc
        payload = {
            "provider_id": str(self.context.provider_id),
            "model": self.context.model,
            "recipient": self.context.provider_endpoint,
            "credential_id": str(self.context.provider_id),
            "max_output_tokens": normalized_body["max_tokens"],
            "messages": normalized_body["messages"],
            "timeout_seconds": 20,
            **{key: value for key, value in normalized_body.items() if key not in {"model", "messages", "max_tokens"}},
        }
        reserve = input_reserve + normalized_body["max_tokens"]
        request, _mapping = self._next_operation(purpose=purpose, payload=payload, reserve_tokens=reserve)
        self.context = self.context.model_copy(
            update={
                "boundary": "before_model",
            }
        )
        self._checkpoint("before_model")
        self.unresolved_effects.add(request.operation_id)
        posted = self._broker.post(
            f"{self.broker_url}/effects",
            content=canonical_bytes(request.model_dump(mode="json")),
            headers={"Content-Type": "application/json", "X-Worker-Capability": self._capability},
        )
        if len(posted.content) > _MAX_BROKER_RESPONSE:
            raise RuntimeAdapterError("effect response exceeds its byte limit")
        if posted.status_code == 409:
            try:
                code = posted.json()["detail"]["code"]
            except (ValueError, KeyError, TypeError):
                code = None
            if code == "budget_exhausted":
                self.unresolved_effects.discard(request.operation_id)
                self.budget_exhausted = True
                raise BudgetExhausted("broker refused the reservation and recorded an owner budget decision")
        try:
            result = OperationResult.model_validate_json(posted.content)
        except ValidationError as exc:
            raise RuntimeAdapterError("invalid effect response") from exc
        if result.operation_id != request.operation_id:
            raise RuntimeAdapterError("effect response operation identity mismatch")
        if result.state == "unknown":
            raise EffectUnresolved("provider outcome is unknown; controller reconciliation is required")
        if result.state == "denied":
            raise EffectDenied("broker denied the provider operation")
        if result.state != "committed" or result.result is None:
            raise RuntimeAdapterError("committed operation has no typed result reference")
        self.unresolved_effects.discard(request.operation_id)
        if request.operation_id in self._charges and result.usage_tokens is not None:
            # The broker released reserve - usage on commit; unknown/denied keep the full reservation.
            self._charges[request.operation_id] = result.usage_tokens
        fetched = self._broker.get(
            f"{self.broker_url}/effects/{request.operation_id}/result",
            headers={"X-Worker-Capability": self._capability},
        )
        fetched.raise_for_status()
        raw = _safe_json_bytes(fetched, maximum=_MAX_PROVIDER_RESPONSE)
        assistant = self._assistant_message(raw)
        current_messages = list(self.context.messages)
        message_index = len(current_messages)
        calls = assistant.get("tool_calls") or []
        if purpose == "model":
            current_messages.append(ChatMessage.model_validate(assistant))
            self.context = self.context.model_copy(
                update={
                    "messages": current_messages,
                    "boundary": "model_committed",
                    "pending_assistant": (
                        {
                            "turn_id": self.context.turn_id,
                            "message_index": message_index,
                            "next_tool_index": 0,
                        }
                        if calls
                        else None
                    ),
                }
            )
        self._checkpoint("model_committed")
        return raw, fetched.headers.get("content-type", "application/json")

    def _assistant_message(self, raw: bytes) -> dict[str, Any]:
        response = _json_object(raw, maximum=_MAX_PROVIDER_RESPONSE, label="Chat Completions result")
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise RuntimeAdapterError("Chat Completions result has no choice")
        message = choices[0].get("message")
        try:
            from scientist.model_payload import ChatMessage

            checked = ChatMessage.model_validate(message)
        except (ValidationError, TypeError) as exc:
            raise RuntimeAdapterError("Chat Completions assistant message is outside the checkpoint profile") from exc
        if checked.role != "assistant":
            raise RuntimeAdapterError("Chat Completions result is not an assistant message")
        return checked.model_dump(mode="json", exclude_unset=True)


class BrokerChatCompletionsTransport(httpx.BaseTransport):
    """Sync httpx transport installed into every Hermes OpenAI client."""

    def __init__(self, adapter: RuntimeAdapter, *, purpose: Literal["model", "compression"] = "model") -> None:
        self._adapter = adapter
        self._purpose = purpose

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or request.url.path.rstrip("/") not in {
            "/v1/chat/completions",
            "/chat/completions",
        }:
            raise RuntimeAdapterError("unbrokered HTTP request denied")
        try:
            body = _json_object(request.read(), maximum=256 * 1024, label="native Chat Completions request")
        except (httpx.RequestError, ValueError) as exc:
            raise RuntimeAdapterError("invalid native Chat Completions request") from exc
        raw, content_type = self._adapter.dispatch_chat_completion(body, purpose=self._purpose)
        return httpx.Response(
            200,
            headers={"content-type": content_type, "content-length": str(len(raw))},
            content=raw,
            request=request,
        )

    def close(self) -> None:
        return None


def install_saved_turn_continuation(agent: Any, adapter: RuntimeAdapter) -> None:
    """Route one pinned Hermes turn through its phase loop without user admission."""
    import agent.conversation_loop as conversation_loop
    import agent.turn_context as turn_context

    original_build_turn_context = conversation_loop.build_turn_context
    saved_history = adapter.native_history()
    user_index = adapter.context.current_turn_user_index
    if user_index is None or user_index >= len(saved_history):
        raise RuntimeAdapterError("checkpoint has no safe current-turn user anchor")
    saved_user = saved_history[user_index]
    if saved_user.get("role") != "user" or not isinstance(saved_user.get("content"), str):
        raise RuntimeAdapterError("checkpoint current-turn anchor is not a text user message")
    saved_timestamp = adapter.context.native_turn_timestamp
    if saved_timestamp is None:
        raise RuntimeAdapterError("checkpoint has no saved native turn timestamp")
    if isinstance(saved_timestamp, str):
        try:
            saved_timestamp = datetime.fromisoformat(saved_timestamp).timestamp()
        except ValueError as exc:
            raise RuntimeAdapterError("checkpoint native turn timestamp is invalid") from exc

    def build_saved_context(
        target_agent: Any,
        user_message: Any,
        system_message: str | None,
        conversation_history: list[dict[str, Any]] | None,
        task_id: str | None,
        stream_callback: Any,
        persist_user_message: Any,
        persist_user_timestamp: float | None = None,
        persist_user_platform_id: str | None = None,
        *,
        persist_user_display_kind: str | None = None,
        persist_user_display_metadata: dict[str, Any] | None = None,
        turn_author: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        if target_agent is not agent:
            return original_build_turn_context(
                target_agent,
                user_message,
                system_message,
                conversation_history,
                task_id,
                stream_callback,
                persist_user_message,
                persist_user_timestamp,
                persist_user_platform_id,
                persist_user_display_kind=persist_user_display_kind,
                persist_user_display_metadata=persist_user_display_metadata,
                turn_author=turn_author,
                **kwargs,
            )
        history = saved_history if conversation_history is None else conversation_history
        if (
            user_index >= len(history)
            or history[user_index].get("role") != "user"
            or history[user_index].get("content") != saved_user["content"]
        ):
            raise RuntimeAdapterError("saved native transcript lost its current-turn anchor")
        turn_context._reset_per_turn_agent_state(agent)
        effective_task_id = str(adapter.context.run_id)
        turn_id = str(adapter.context.turn_id)
        agent._stream_callback = stream_callback
        agent._persist_user_message_idx = user_index
        agent._persist_user_message_override = saved_user["content"]
        agent._persist_user_message_timestamp = saved_user.get("timestamp")
        agent._persist_user_message_platform_id = None
        agent._current_task_id = effective_task_id
        agent._current_turn_id = turn_id
        agent._relay_pending_turn_id = None
        agent._current_api_request_id = f"{turn_id}:api:0"
        agent._current_turn_timestamp = saved_timestamp
        agent._cached_system_prompt = adapter.context.system_prompt
        agent._is_user_initiated_turn = True
        return turn_context.TurnContext(
            user_message=saved_user["content"],
            original_user_message=saved_user["content"],
            messages=list(history),
            conversation_history=history,
            active_system_prompt=adapter.context.system_prompt,
            effective_task_id=effective_task_id,
            turn_id=turn_id,
            current_turn_user_idx=user_index,
            should_review_memory=False,
            plugin_user_context="",
            ext_prefetch_cache="",
            preflight_compression_blocked=False,
        )

    conversation_loop.build_turn_context = build_saved_context


def build_native_agent(adapter: RuntimeAdapter, *, workspace_dir: Path) -> Any:
    """Construct pinned Hermes with only Todo available and broker-only clients.

    The caller must establish private HOME/HERMES_HOME and immutable import paths
    before invoking this function.
    """
    from openai import OpenAI
    from run_agent import AIAgent
    import agent.auxiliary_client as auxiliary_client
    import agent.conversation_loop as conversation_loop
    import agent.context_compressor as context_compressor
    import agent.micro_compaction as micro_compaction
    import model_tools

    original_assemble_api_request = conversation_loop.assemble_api_request

    @wraps(original_assemble_api_request)
    def capture_primary_history(agent: Any, **kwargs: Any) -> Any:
        native_messages = kwargs.get("messages")
        if not isinstance(native_messages, list):
            raise RuntimeAdapterError("Hermes request has no canonical message history")
        current_user_index = kwargs.get("current_turn_user_idx")
        if (
            type(current_user_index) is not int
            or current_user_index < 0
            or current_user_index >= len(native_messages)
            or _model_mapping(native_messages[current_user_index]).get("role") != "user"
        ):
            raise RuntimeAdapterError("Hermes request has no safe current-turn user anchor")
        adapter.sync_primary_history(
            native_messages,
            current_turn_user_index=current_user_index,
            native_turn_timestamp=getattr(agent, "_current_turn_timestamp", None),
        )
        return original_assemble_api_request(agent, **kwargs)

    conversation_loop.assemble_api_request = capture_primary_history

    class BrokerAIAgent(AIAgent):
        def _try_recover_primary_transport(
            self,
            api_error: Exception,
            *,
            retry_count: int,
            max_retries: int,
        ) -> bool:
            # Hermes has a separate transport-rebuild retry after its normal
            # API attempt budget. Broker dispatch must never replay it.
            return False

        def _create_openai_client(self, client_kwargs: dict, *, reason: str, shared: bool) -> Any:
            kwargs = dict(client_kwargs)
            kwargs.update(
                api_key="worker-transport-only",
                base_url="http://hermes.invalid/v1",
                max_retries=0,
                http_client=httpx.Client(
                    transport=adapter.transport(purpose="model"),
                    trust_env=False,
                    follow_redirects=False,
                ),
            )
            return OpenAI(**kwargs)

        def _execute_tool_calls(
            self, assistant_message: Any, messages: list, effective_task_id: str, api_call_count: int = 0
        ) -> None:
            """Checkpoint normalized tool identity before native Todo execution."""
            pending = adapter.context.pending_assistant
            if pending is None:
                raise RuntimeAdapterError("native tools require a durable pending assistant")
            stored = adapter.context.messages[pending.message_index]
            raw_calls = stored.tool_calls or []
            current_calls = list(getattr(assistant_message, "tool_calls", []) or [])
            if pending.applied_tool_ids:
                applied = pending.applied_tool_ids
            else:
                if len(raw_calls) != len(current_calls):
                    raise RuntimeAdapterError("native tool batch differs from saved assistant response")
                applied = [
                    AppliedToolId(raw_id=raw.id, applied_id=call.id)
                    for raw, call in zip(raw_calls, current_calls, strict=True)
                ]
                pending = PendingAssistant(
                    turn_id=pending.turn_id,
                    message_index=pending.message_index,
                    next_tool_index=pending.next_tool_index,
                    applied_tool_ids=applied,
                )
                adapter.context = adapter.context.model_copy(update={"pending_assistant": pending})
            raw_to_applied = {item.raw_id: item.applied_id for item in applied}
            if set(raw_to_applied) != {call.id for call in raw_calls}:
                raise RuntimeAdapterError("applied tool identity map is incomplete")
            suffix = applied[pending.next_tool_index :]
            suffix_calls = raw_calls[pending.next_tool_index :]
            if len(current_calls) != len(suffix) or len(suffix_calls) != len(suffix):
                raise RuntimeAdapterError("native tool suffix differs from durable cursor")
            for saved_call, call in zip(suffix_calls, current_calls, strict=True):
                if call.id not in {item.applied_id for item in suffix}:
                    raise RuntimeAdapterError("normalized tool identity is not checkpointed")
                saved = _model_mapping(saved_call)
                live = _model_mapping(call)
                saved_function = saved.get("function") or {}
                live_function = live.get("function") or {}
                if (
                    saved_function.get("name") != live_function.get("name")
                    or saved_function.get("arguments") != live_function.get("arguments")
                ):
                    raise RuntimeAdapterError("native tool suffix differs from its saved assistant call")
                _validate_native_todo_call(self, live)
            adapter.context = adapter.context.model_copy(
                update={"boundary": "before_tool", "pending_assistant": pending}
            )
            adapter._checkpoint("before_tool")
            super()._execute_tool_calls(assistant_message, messages, effective_task_id, api_call_count)
            dumped = [
                item.model_dump(mode="json", exclude_none=True)
                if hasattr(item, "model_dump")
                else dict(item)
                for item in messages
            ]
            reverse = {item.applied_id: item.raw_id for item in applied}
            completed = {
                reverse[item["tool_call_id"]]
                for item in dumped
                if item.get("role") == "tool" and item.get("tool_call_id") in reverse
            }
            for item in dumped:
                if item.get("role") == "tool" and item.get("tool_call_id") in reverse:
                    item["tool_call_id"] = reverse[item["tool_call_id"]]
            # Hermes may normalize the assistant call object in place. Keep the
            # exact broker-owned call IDs/arguments as the durable source of truth.
            if pending.message_index >= len(dumped):
                raise RuntimeAdapterError("Hermes tool history lost the saved assistant message")
            live_assistant = dumped[pending.message_index]
            live_calls = live_assistant.get("tool_calls") or []
            if live_assistant.get("role") != "assistant" or len(live_calls) != len(raw_calls):
                raise RuntimeAdapterError("Hermes tool history no longer matches saved assistant batch")
            for raw_call, live_call in zip(raw_calls, live_calls, strict=True):
                live_function = live_call.get("function") or {}
                if (
                    live_function.get("name") != raw_call.function.name
                    or live_function.get("arguments") != raw_call.function.arguments
                ):
                    raise RuntimeAdapterError("Hermes changed saved tool name or argument JSON")
            restored_assistant = stored.model_dump(mode="json", exclude_none=True)
            if "timestamp" in live_assistant:
                restored_assistant["timestamp"] = live_assistant["timestamp"]
            dumped[pending.message_index] = restored_assistant
            next_index = len(completed)
            todo = TodoSnapshot.model_validate(self._todo_store.snapshot())
            adapter.context = adapter.context.model_copy(
                update={
                "messages": [ChatMessage.model_validate(item) for item in _wire_messages(dumped)],
                "native_message_metadata": _native_message_metadata(dumped),
                    "todo": todo,
                    "pending_assistant": (
                        None
                        if next_index >= len(raw_calls)
                        else PendingAssistant(
                            turn_id=pending.turn_id,
                            message_index=pending.message_index,
                            next_tool_index=next_index,
                            applied_tool_ids=applied,
                        )
                    ),
                    "boundary": "tool_committed",
                }
            )
            adapter._checkpoint("tool_committed")

    def create_auxiliary_client(*, api_key: str, base_url: str, **kwargs: Any) -> Any:
        kwargs.update(
            api_key="worker-transport-only",
            base_url="http://hermes.invalid/v1",
            max_retries=0,
            http_client=httpx.Client(
                transport=adapter.transport(purpose="compression"),
                trust_env=False,
                follow_redirects=False,
            ),
        )
        return OpenAI(**kwargs)

    def deny_async_auxiliary(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeAdapterError("asynchronous auxiliary clients are disabled in the worker")

    def call_compression_once(
        *,
        task: str,
        messages: list[dict[str, Any]],
        route_info: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        if task != "compression":
            raise RuntimeAdapterError("only context compression is available as an auxiliary call")
        main_runtime = kwargs.get("main_runtime") or {}
        if main_runtime.get("model") not in (None, adapter.context.model):
            raise RuntimeAdapterError("compression model differs from the approved run model")
        if route_info is not None:
            route_info.update(provider="custom", model=adapter.context.model)
        client = OpenAI(
            api_key="worker-transport-only",
            base_url="http://hermes.invalid/v1",
            max_retries=0,
            http_client=httpx.Client(
                transport=adapter.transport(purpose="compression"),
                trust_env=False,
                follow_redirects=False,
            ),
        )
        return client.chat.completions.create(
            model=adapter.context.model,
            messages=messages,
            max_tokens=_COMPRESSION_MAX_OUTPUT_TOKENS,
            stream=False,
            timeout=kwargs.get("timeout", 90),
        )

    # The sync compressor remains broker-backed. Async auxiliary requests are
    # outside this worker profile and fail closed rather than using SDK defaults.
    auxiliary_client._create_openai_client = create_auxiliary_client
    auxiliary_client._to_async_client = deny_async_auxiliary
    auxiliary_client.call_llm = call_compression_once
    context_compressor.call_llm = call_compression_once
    micro_compaction.call_llm = call_compression_once

    original_tool_definitions = model_tools.get_tool_definitions

    def reviewed_native_tool_definitions(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        parameter_names = (
            "enabled_toolsets", "disabled_toolsets", "quiet_mode", "skip_tool_search_assembly"
        )
        call_kwargs = dict(zip(parameter_names, args))
        call_kwargs.update(kwargs)
        call_kwargs.update({
            "enabled_toolsets": ["todo"],
            "disabled_toolsets": [],
            "quiet_mode": True,
            "skip_tool_search_assembly": True,
        })
        definitions = original_tool_definitions(**call_kwargs)
        # Pinned Hermes normally adds tool_search/tool_describe/tool_call bridge
        # schemas after toolset filtering. Expose only the reviewed direct Todo
        # handler; legacy bridge continuations are checked separately below.
        return [
            item for item in definitions
            if (item.get("function") or {}).get("name") == "todo_list"
        ]

    model_tools.get_tool_definitions = reviewed_native_tool_definitions
    try:
        agent = BrokerAIAgent(
            base_url="http://hermes.invalid/v1",
            api_key="worker-transport-only",
            provider="custom",
            api_mode="chat_completions",
            model=adapter.context.model,
            enabled_toolsets=["todo"],
            disabled_toolsets=["memory", "terminal", "execute_code", "browser", "web", "delegate", "mcp"],
            quiet_mode=True,
            save_trajectories=False,
            skip_context_files=True,
            load_soul_identity=False,
            skip_memory=True,
            skip_background_review=True,
            fallback_model=None,
            credential_pool=None,
            user_id=None,
            user_id_alt=None,
            user_name=None,
            chat_id=None,
            chat_name=None,
            chat_type=None,
            thread_id=None,
            gateway_session_key=None,
            session_db=None,
            cwd=str(workspace_dir),
        )
    finally:
        model_tools.get_tool_definitions = original_tool_definitions
    agent._persist_disabled = True
    # Hermes interprets this as TOTAL attempts, not retries-after-first.
    # Keep one broker request per turn; the OpenAI SDK itself uses 0 retries.
    agent._api_max_retries = 1
    agent._auto_recovery_cycles = 0
    agent._disable_streaming = True
    agent._fallback_activated = False
    agent.fallback_model = None
    agent._credential_pool = None
    agent._todo_store.restore(
        [entry.model_dump(mode="json", exclude_none=True) for entry in adapter.context.todo.todos],
        revision=adapter.context.todo.revision,
    )
    compressor = getattr(agent, "context_compressor", None)
    if compressor is not None and adapter.context.compacted_context is not None:
        state = adapter.context.compacted_context
        compressor.compression_count = state.compression_count
        compressor._previous_summary = state.previous_summary
        compressor._summary_has_user_turn = state.summary_has_user_turn
        compressor._ineffective_compression_count = state.ineffective_compression_count
        micro = state.micro
        compressor._micro_compact_passes = micro.passes
        compressor._micro_compact_tokens_saved_total = micro.tokens_saved_total
        compressor._micro_compact_turns_since_pass = micro.turns_since_pass
        compressor._micro_compact_cursor = micro.cursor
        compressor._micro_compact_rolling_summary = micro.rolling_summary
        compressor._micro_compact_consecutive_failures = micro.consecutive_failures
        compressor._micro_compact_last_failure_cursor = micro.last_failure_cursor
        compressor._micro_compact_enabled = micro.enabled
        compressor._micro_compact_defrag_threshold_tokens = micro.defrag_threshold_tokens
    if compressor is not None:
        original_compress_context = agent._compress_context

        @wraps(original_compress_context)
        def checkpoint_compression(*args: Any, **kwargs: Any) -> Any:
            result = original_compress_context(*args, **kwargs)
            if not isinstance(result, tuple) or len(result) < 2 or not isinstance(result[0], list):
                raise RuntimeAdapterError("Hermes compression returned an invalid primary transcript")
            native_messages, active_system_prompt = result[0], result[1]
            old_index = adapter.context.current_turn_user_index
            old_history = adapter.native_history()
            if old_index is None or old_index >= len(old_history):
                raise RuntimeAdapterError("compression checkpoint lost the current-turn anchor")
            from agent.turn_context import reanchor_current_turn_user_idx

            user_content = old_history[old_index].get("content")
            new_index = reanchor_current_turn_user_idx(native_messages, user_content)
            if new_index < 0:
                raise RuntimeAdapterError("compression could not re-anchor the current user turn")
            adapter.sync_primary_history(
                native_messages,
                current_turn_user_index=new_index,
                native_turn_timestamp=getattr(agent, "_current_turn_timestamp", None),
            )
            adapter.context = adapter.context.model_copy(
                update={
                    "system_prompt": active_system_prompt or adapter.context.system_prompt,
                    "compacted_context": _compacted_context(compressor),
                    "boundary": "before_model",
                }
            )
            adapter._checkpoint("before_model")
            return result

        agent._compress_context = checkpoint_compression
    return agent
