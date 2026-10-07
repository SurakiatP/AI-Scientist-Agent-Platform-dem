"""Broker-backed Chat Completions transport for the pinned Hermes worker."""

from __future__ import annotations

import base64
import contextvars
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

from scientist.contracts import (
    CsvDescribeGrantV1,
    CrossrefQueryV1,
    OperationRequest,
    OperationResult,
    ScientificBinding,
    ScientificBindingV2,
)
from scientist.instruction_loader import InstructionBundle
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
    ComputeResultEnvelopeV2,
    CompactedContext,
    MicroCompactionState,
    OperationMapping,
    NativeMessageMetadata,
    PendingAssistant,
    RuntimeContextV1,
    TodoSnapshot,
    WorkspaceFile,
    WorkspaceEntry,
    ScientificResultReceipt,
    ScientificOutputReceiptV2,
    ScientificResultReceiptV2,
    MAX_COMPUTE_OUTPUT_BYTES,
    MAX_WORKSPACE_BYTES,
    canonical_bytes,
    operation_fingerprint,
    validate_workspace_path,
)
from scientist.scientific_render import render_plot_svg

_MAX_PROVIDER_RESPONSE = 2 * 1024 * 1024
_MAX_BROKER_RESPONSE = 3 * 1024 * 1024
_DEFAULT_MODEL_OUTPUT_TOKENS = 2048
_COMPRESSION_MAX_OUTPUT_TOKENS = 2048
_REVIEWED_NATIVE_TODO_TOOL_NAMES = frozenset({"todo_list", "todo"})
_REVIEWED_NATIVE_TODO_BRIDGE = "tool_call"
_REVIEWED_NATIVE_SCIENTIFIC_TOOL_NAMES = frozenset({
    "instruction_view",
    "scientific_resources",
    "scientific_search",
    "scientific_csv_describe",
    "scientific_plot",
})
_NATIVE_TOOL_CALL_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "scientist_native_tool_call_id", default=None
)
_NATIVE_TOOL_CALL_AUTHORITY: contextvars.ContextVar[tuple[str, str, bytes] | None] = contextvars.ContextVar(
    "scientist_native_tool_call_authority", default=None
)
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


def _native_tool_call_mapping(value: Any) -> dict[str, Any]:
    """Project the pinned Hermes transport ToolCall onto Chat Completions wire shape.

    Hermes' normalized ToolCall is a dataclass whose ``function`` property points
    back to itself. Keep this conversion scoped to that exact native type; runtime
    contexts still require Pydantic models or dictionaries.
    """
    if type(value).__module__ != "agent.transports.types" or type(value).__name__ != "ToolCall":
        return _model_mapping(value)

    try:
        attributes = vars(value)
    except TypeError as exc:
        raise RuntimeAdapterError("invalid native tool call") from exc
    expected = {"id", "name", "arguments", "provider_data"}
    if not expected.issubset(attributes) or set(attributes) - expected - {"args_repaired"}:
        raise RuntimeAdapterError("invalid native tool call")
    if "args_repaired" in attributes and not isinstance(attributes["args_repaired"], bool):
        raise RuntimeAdapterError("invalid native tool call metadata")

    call_id = attributes["id"]
    name = attributes["name"]
    arguments = attributes["arguments"]
    if (
        not isinstance(call_id, str)
        or not call_id
        or call_id.strip() != call_id
        or "|" in call_id
        or not isinstance(name, str)
        or not name
        or not isinstance(arguments, str)
    ):
        raise RuntimeAdapterError("invalid native tool call")

    provider_data = attributes["provider_data"]
    if provider_data is not None:
        if not isinstance(provider_data, dict) or set(provider_data) - {"call_id", "response_item_id"}:
            raise RuntimeAdapterError("unsupported native tool call metadata")
        # Chat Completions calls in this profile pair by id. Hermes uses these
        # aliases for other protocols, where its dispatch pairing differs.
        if any(value is not None for value in provider_data.values()):
            raise RuntimeAdapterError("unsupported native tool call metadata")

    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _validate_native_tool_arguments(name: str, arguments: Any, binding: ScientificBinding | None) -> None:
    if not isinstance(arguments, dict):
        raise RuntimeAdapterError("native tool arguments must be an object")
    if name == "scientific_search":
        requests = getattr(binding, "approved_crossref_queries", None)
        request_id = arguments.get("request_id")
        if (
            binding is None
            or set(arguments) != {"request_id"}
            or not isinstance(requests, dict)
            or not isinstance(request_id, str)
            or request_id not in requests
        ):
            raise RuntimeAdapterError("Crossref request outside approved authority")
    elif name == "scientific_csv_describe":
        grants = getattr(binding, "csv_describe_grants", None)
        grant_id = arguments.get("grant_id")
        if (
            binding is None
            or set(arguments) != {"grant_id"}
            or not isinstance(grants, dict)
            or not isinstance(grant_id, str)
            or grant_id not in grants
        ):
            raise RuntimeAdapterError("CSV grant outside approved authority")
    elif name == "scientific_resources":
        if arguments or binding is None or "get-available-resources" not in binding.capability_ids:
            raise RuntimeAdapterError("scientific resource request outside approved authority")
    elif name == "instruction_view":
        if (
            binding is None
            or set(arguments) != {"capability_id"}
            or arguments.get("capability_id") not in binding.capability_ids
        ):
            raise RuntimeAdapterError("instruction request outside approved authority")
    elif name == "scientific_plot":
        if binding is None or "scientific-visualization" not in binding.capability_ids:
            raise RuntimeAdapterError("plot request outside approved visualization authority")
        try:
            render_plot_svg(arguments)
        except (TypeError, ValueError) as exc:
            raise RuntimeAdapterError("invalid scientific plot arguments") from exc
    else:
        raise RuntimeAdapterError("native tool outside reviewed scientific surface")


def _native_scientific_tool_definitions(
    binding: ScientificBinding | ScientificBindingV2 | None,
) -> list[dict[str, Any]]:
    if binding is None:
        return []
    definitions = [
        {
            "name": "instruction_view",
            "description": "Read selected approved scientific instruction untrusted reference text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "capability_id": {"type": "string", "enum": list(binding.capability_ids)}
                },
                "required": ["capability_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "scientific_resources",
            "description": "Measure bounded CPU memory limits inside this approved worker.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    ]
    if isinstance(binding, ScientificBindingV2):
        definitions.extend([
            {
                "name": "scientific_search",
                "description": "Search Crossref using one owner-approved request.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request_id": {
                            "type": "string",
                            "enum": list(binding.approved_crossref_queries),
                        }
                    },
                    "required": ["request_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "scientific_csv_describe",
                "description": "Describe an approved CSV with the pinned bounded recipe.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "grant_id": {
                            "type": "string",
                            "enum": list(binding.csv_describe_grants),
                        }
                    },
                    "required": ["grant_id"],
                    "additionalProperties": False,
                },
            },
        ])
    if "scientific-visualization" in binding.capability_ids:
        definitions.append({
            "name": "scientific_plot",
            "description": "Render a bounded line chart from numeric series as inert SVG.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "maxLength": 100},
                    "x_label": {"type": "string", "maxLength": 100},
                    "y_label": {"type": "string", "maxLength": 100},
                    "series": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 3,
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "maxLength": 64},
                                "values": {
                                    "type": "array",
                                    "minItems": 2,
                                    "maxItems": 64,
                                    "items": {"type": "number"},
                                },
                            },
                            "required": ["label", "values"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["title", "x_label", "y_label", "series"],
                "additionalProperties": False,
            },
        })
    enabled = {"instruction_view"}
    if "get-available-resources" in binding.capability_ids:
        enabled.add("scientific_resources")
    if isinstance(binding, ScientificBindingV2):
        if "paper-lookup" in binding.capability_ids and binding.approved_crossref_queries:
            enabled.add("scientific_search")
        if "exploratory-data-analysis" in binding.capability_ids and binding.csv_describe_grants:
            enabled.add("scientific_csv_describe")
    if "scientific-visualization" in binding.capability_ids:
        enabled.add("scientific_plot")
    return [definition for definition in definitions if definition["name"] in enabled]


def _validate_native_todo_call(agent: Any, call: Any) -> tuple[str, dict[str, Any]]:
    """Allow only Todo and exact reviewed scientific handlers, direct or deferred.

    The registry executor can dispatch names regardless of model-visible schemas.
    Deferred Tool Search calls are therefore unwrapped and checked here, then the
    scientific handlers repeat authority validation after middleware transforms.
    """
    value = _native_tool_call_mapping(call)
    function = value.get("function")
    if not isinstance(function, dict):
        raise RuntimeAdapterError("native tool call has no function")
    name = function.get("name")
    # Hermes' pinned native parser canonicalizes this legacy registry alias
    # before dispatch. Apply the same single alias at the saved-call boundary.
    if name == "todo":
        name = "todo_list"
    allowed = _REVIEWED_NATIVE_TODO_TOOL_NAMES | _REVIEWED_NATIVE_SCIENTIFIC_TOOL_NAMES
    if name not in allowed and name != _REVIEWED_NATIVE_TODO_BRIDGE:
        raise RuntimeAdapterError("native tool call is outside the reviewed surface")
    raw_arguments = function.get("arguments")
    if isinstance(raw_arguments, str):
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise RuntimeAdapterError("native Todo arguments are invalid") from exc
    else:
        arguments = raw_arguments
    if not isinstance(arguments, dict):
        raise RuntimeAdapterError("native tool arguments are invalid")

    try:
        # This pinned Hermes helper canonicalizes the legacy alias and, for the
        # Tool Search bridge, validates the underlying name against the agent's
        # enabled toolset and the concrete deferred-tool schema.
        from agent.tool_executor import _unwrap_tool_search_call

        resolved_name, resolved_arguments, scope_block = _unwrap_tool_search_call(
            agent, name, arguments
        )
    except Exception as exc:
        raise RuntimeAdapterError("native Todo scope validation failed") from exc
    if scope_block is not None or resolved_name not in allowed:
        raise RuntimeAdapterError("native tool call is outside the reviewed scope")
    if resolved_name in _REVIEWED_NATIVE_SCIENTIFIC_TOOL_NAMES:
        _validate_native_tool_arguments(resolved_name, resolved_arguments, getattr(agent, "_scientific_binding", None))
    return resolved_name, resolved_arguments


def _write_scientific_result(workspace_dir: Path, path: str, content: bytes) -> None:
    relative = validate_workspace_path(path)
    root_info = workspace_dir.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise RuntimeAdapterError("scientific workspace root is unsafe")
    target = workspace_dir / relative
    current = workspace_dir
    for part in PurePosixPath(relative).parts[:-1]:
        current = current / part
        current.mkdir(mode=0o700, exist_ok=True)
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise RuntimeAdapterError("scientific result parent is unsafe")
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError as exc:
        raise RuntimeAdapterError("scientific result path already exists") from exc
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeAdapterError("scientific result write failed")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _persist_scientific_plot(
    workspace_dir: Path,
    binding: ScientificBinding | ScientificBindingV2,
    tool_call_id: str,
    svg: str,
) -> tuple[ScientificResultReceipt, WorkspaceEntry]:
    content = svg.encode("utf-8")
    path = f"outputs/plots/{hashlib.sha256(tool_call_id.encode('utf-8')).hexdigest()[:24]}.svg"
    max_bytes = binding.max_result_bytes if isinstance(binding, ScientificBinding) else MAX_COMPUTE_OUTPUT_BYTES
    if len(content) > max_bytes:
        raise RuntimeAdapterError("scientific plot exceeds approved output limits")
    _write_or_verify_scientific_result(workspace_dir, path, content)
    digest = hashlib.sha256(content).hexdigest()
    receipt = ScientificResultReceipt(
        tool_call_id=tool_call_id,
        capability_id="scientific-visualization",
        binding_sha256=hashlib.sha256(canonical_bytes(binding.model_dump(mode="json"))).hexdigest(),
        recipe_version="1",
        path=path,
        sha256=digest,
        size=len(content),
    )
    return receipt, WorkspaceEntry(path=path, sha256=digest, size=len(content))


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
        if isinstance(wire.get("tool_calls"), list):
            native_calls = []
            for raw_call in wire["tool_calls"]:
                call = _model_mapping(raw_call)
                if isinstance(call, dict):
                    call = dict(call)
                    for key in ("call_id", "response_item_id"):
                        if key in call:
                            identifier = call.pop(key)
                            if not isinstance(identifier, str) or not identifier.strip():
                                raise RuntimeAdapterError(
                                    "native tool-call metadata identifier is invalid"
                                )
                native_calls.append(call)
            wire["tool_calls"] = native_calls
        if wire.get("role") == "assistant" and "content" not in wire:
            wire["content"] = None
        try:
            parsed = ChatMessage.model_validate(wire)
        except ValidationError as exc:
            raise RuntimeAdapterError("native message is outside approved wire profile") from exc
        projected.append(parsed.model_dump(mode="json", exclude_unset=True))
    return projected



def _write_or_verify_scientific_result(workspace_dir: Path, path: str, content: bytes) -> None:
    try:
        _write_scientific_result(workspace_dir, path, content)
        return
    except RuntimeAdapterError as exc:
        original_error = exc
    relative = validate_workspace_path(path)
    root_info = workspace_dir.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise RuntimeAdapterError("scientific workspace root is unsafe") from original_error
    target = workspace_dir / relative
    current = workspace_dir
    for part in PurePosixPath(relative).parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise RuntimeAdapterError("existing scientific result path is unsafe") from exc
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise RuntimeAdapterError("existing scientific result parent is unsafe") from original_error
    try:
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise RuntimeAdapterError("existing scientific result is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size != len(content)
            or info.st_size > 256 * 1024
        ):
            raise RuntimeAdapterError("existing scientific result differs from its verified output")
        chunks = bytearray()
        while len(chunks) <= len(content):
            block = os.read(descriptor, min(65536, len(content) + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        if bytes(chunks) != content:
            raise RuntimeAdapterError("existing scientific result differs from its verified output")
    finally:
        os.close(descriptor)


def _collect_native_v2_receipts(
    outputs: dict[str, dict[str, Any]],
    applied_to_raw: dict[str, str],
    completed_raw_ids: set[str],
) -> tuple[list[ScientificResultReceiptV2], list[WorkspaceEntry]]:
    receipts: list[ScientificResultReceiptV2] = []
    workspace: list[WorkspaceEntry] = []
    for applied_id, output in outputs.items():
        receipt = output.get("receipt_v2")
        if receipt is None:
            continue
        raw_id = applied_to_raw.get(applied_id)
        entries = output.get("workspace_entries_v2")
        if (
            not isinstance(receipt, ScientificResultReceiptV2)
            or not isinstance(raw_id, str)
            or raw_id not in completed_raw_ids
            or not isinstance(entries, list)
            or len(entries) != 4
            or any(not isinstance(entry, WorkspaceEntry) for entry in entries)
        ):
            raise RuntimeAdapterError("V2 scientific receipt is not bound to a completed native call")
        if [(item.path, item.sha256, item.size) for item in receipt.outputs] != [
            (entry.path, entry.sha256, entry.size) for entry in entries
        ]:
            raise RuntimeAdapterError("V2 scientific receipt differs from its workspace outputs")
        receipts.append(receipt.model_copy(update={"tool_call_id": raw_id}))
        workspace.extend(entries)
    return receipts, workspace

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
                    WorkspaceEntry(path=item.path, sha256=item.sha256, size=item.size)
                    for item in workspace
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

    def _next_tool_operation(
        self,
        *,
        kind: Literal["search", "compute"],
        tool_call_id: str,
        payload: dict[str, Any],
    ) -> tuple[OperationRequest, OperationMapping]:
        if not tool_call_id or tool_call_id.strip() != tool_call_id or "|" in tool_call_id:
            raise RuntimeAdapterError("invalid raw tool-call identity")
        if kind not in {"search", "compute"}:
            raise RuntimeAdapterError("unreviewed scientific operation")
        for mapping in reversed(self.context.operation_mappings):
            if (
                mapping.turn_id == self.context.turn_id
                and mapping.purpose == "tool"
                and mapping.tool_call_id == tool_call_id
            ):
                request = mapping.request.model_copy(update={"generation": self.context.generation})
                if (
                    request.kind != kind
                    or request.payload != payload
                    or request.reserve_tokens != 0
                    or operation_fingerprint(request) != mapping.payload_hash
                ):
                    raise RuntimeAdapterError("replayed scientific operation fingerprint changed")
                return request, mapping
        operation_id = str(
            uuid.uuid5(self.context.run_id, f"{self.context.turn_id}:tool:{tool_call_id}")
        )
        request = OperationRequest(
            run_id=self.context.run_id,
            generation=self.context.generation,
            operation_id=operation_id,
            kind=kind,
            payload=payload,
            reserve_tokens=0,
        )
        mapping = OperationMapping(
            operation_id=operation_id,
            turn_id=self.context.turn_id,
            purpose="tool",
            model_sequence=None,
            tool_call_id=tool_call_id,
            request=request,
            payload_hash=operation_fingerprint(request),
        )
        self.context = RuntimeContextV1.model_validate(
            self.context.model_copy(
                update={"operation_mappings": [*self.context.operation_mappings, mapping]}
            ).model_dump(mode="json")
        )
        return request, mapping

    def _execute_tool_effect(self, request: OperationRequest) -> bytes:
        self._checkpoint("before_tool")
        self.unresolved_effects.add(request.operation_id)
        try:
            posted = self._broker.post(
                f"{self.broker_url}/effects",
                content=canonical_bytes(request.model_dump(mode="json")),
                headers={
                    "Content-Type": "application/json",
                    "X-Worker-Capability": self._capability,
                },
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise EffectUnresolved(
                "scientific operation outcome unknown; controller reconciliation required"
            ) from exc
        if len(posted.content) > _MAX_BROKER_RESPONSE:
            raise RuntimeAdapterError("scientific operation response exceeds its limit")
        if posted.status_code == 409:
            try:
                code = posted.json()["detail"]["code"]
            except (ValueError, KeyError, TypeError):
                code = None
            if code == "budget_exhausted":
                self.unresolved_effects.discard(request.operation_id)
                self.budget_exhausted = True
                raise BudgetExhausted("broker refused scientific operation under the approved budget")
        try:
            posted.raise_for_status()
            result = OperationResult.model_validate_json(posted.content)
        except (httpx.HTTPStatusError, ValidationError) as exc:
            raise RuntimeAdapterError("scientific operation was not acknowledged") from exc
        if result.operation_id != request.operation_id:
            raise RuntimeAdapterError("scientific operation identity mismatch")
        if result.state == "unknown":
            raise EffectUnresolved("scientific operation outcome unknown; controller reconciliation required")
        if result.state == "denied":
            self.unresolved_effects.discard(request.operation_id)
            raise EffectDenied("broker denied scientific operation")
        if result.state != "committed" or result.result is None:
            raise RuntimeAdapterError("committed scientific operation has no result reference")
        if (
            result.result.project_id != self.context.project_id
            or result.result.content_type != "application/octet-stream"
        ):
            raise RuntimeAdapterError("scientific result reference outside the approved project")
        self.unresolved_effects.discard(request.operation_id)
        try:
            fetched = self._broker.get(
                f"{self.broker_url}/effects/{request.operation_id}/result",
                headers={"X-Worker-Capability": self._capability},
            )
            fetched.raise_for_status()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
            raise EffectUnresolved("committed scientific result is not yet available") from exc
        if (
            len(fetched.content) > _MAX_BROKER_RESPONSE
            or len(fetched.content) != result.result.size
            or hashlib.sha256(fetched.content).hexdigest() != result.result.sha256.lower()
        ):
            raise RuntimeAdapterError("scientific result bytes differ from the committed reference")
        return fetched.content

    def _run_approved_search(self, request_id: str, tool_call_id: str) -> dict[str, Any]:
        binding = self.context.plan.scientific
        if not isinstance(binding, ScientificBindingV2):
            raise RuntimeAdapterError("approved Crossref binding is unavailable")
        _validate_native_tool_arguments("scientific_search", {"request_id": request_id}, binding)
        query = binding.approved_crossref_queries[request_id]
        request, mapping = self._next_tool_operation(
            kind="search",
            tool_call_id=tool_call_id,
            payload={"request_id": request_id, "query": query.model_dump(mode="json")},
        )
        try:
            result = json.loads(self._execute_tool_effect(request))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeAdapterError("invalid Crossref result JSON") from exc
        expected_request = {"query": query.query, "doi": query.doi, "limit": query.limit}
        provenance = result.get("provenance") if isinstance(result, dict) else None
        records = result.get("records") if isinstance(result, dict) else None
        if (
            not isinstance(result, dict)
            or result.get("source_id") != "crossref"
            or result.get("access_mode") != "public_read"
            or not isinstance(provenance, dict)
            or provenance.get("source_id") != "crossref"
            or provenance.get("access_mode") != "public_read"
            or provenance.get("request") != expected_request
            or not isinstance(records, list)
            or len(records) > query.limit
            or result.get("record_count") != len(records)
        ):
            raise RuntimeAdapterError("Crossref result provenance differs from its approved request")
        return result

    def _run_approved_csv_describe(
        self,
        grant_id: str,
        tool_call_id: str,
    ) -> tuple[str, ScientificResultReceiptV2, list[WorkspaceEntry]]:
        binding = self.context.plan.scientific
        if not isinstance(binding, ScientificBindingV2):
            raise RuntimeAdapterError("approved CSV grant is unavailable")
        _validate_native_tool_arguments("scientific_csv_describe", {"grant_id": grant_id}, binding)
        grant = binding.csv_describe_grants[grant_id]
        request, _mapping = self._next_tool_operation(
            kind="compute",
            tool_call_id=tool_call_id,
            payload={"grant_id": grant_id, "grant": grant.model_dump(mode="json")},
        )
        raw = self._execute_tool_effect(request)
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeAdapterError("CSV compute envelope exceeds its limit")
        try:
            envelope = ComputeResultEnvelopeV2.model_validate_json(raw)
        except ValidationError as exc:
            raise RuntimeAdapterError("invalid CSV compute result envelope") from exc
        from scientist.runtime_contracts import compute_input_manifest_sha256

        binding_sha256 = hashlib.sha256(canonical_bytes(binding.model_dump(mode="json"))).hexdigest()
        if (
            canonical_bytes(envelope.model_dump(mode="json")) != raw
            or envelope.binding_sha256 != binding_sha256
            or envelope.grant_id != grant_id
            or envelope.recipe_manifest_sha256 != grant.recipe_manifest_sha256
            or envelope.profile_id != grant.profile_id
            or envelope.profile_version != grant.profile_version
            or envelope.image_digest != grant.image_digest
            or envelope.input_manifest_sha256 != compute_input_manifest_sha256(grant)
            or envelope.input_ref != grant.input_ref
            or envelope.input_sha256 != grant.input_sha256
            or any(output.object_ref.project_id != self.context.project_id for output in envelope.outputs)
        ):
            raise RuntimeAdapterError("CSV compute result differs from its approved grant")
        result_outputs: list[ScientificOutputReceiptV2] = []
        workspace_entries: list[WorkspaceEntry] = []
        decoded_outputs: list[bytes] = []
        for output in envelope.outputs:
            try:
                data = base64.b64decode(output.data_base64, validate=True)
            except (ValueError, TypeError) as exc:
                raise RuntimeAdapterError("invalid CSV compute output encoding") from exc
            if (
                len(data) != output.object_ref.size
                or hashlib.sha256(data).hexdigest() != output.object_ref.sha256.lower()
                or output.object_ref.content_type != "application/octet-stream"
            ):
                raise RuntimeAdapterError("CSV compute output differs from its durable reference")
            path = f"outputs/{output.name}"
            _write_or_verify_scientific_result(self.workspace_dir, path, data)
            result_outputs.append(
                ScientificOutputReceiptV2(
                    index=output.index,
                    name=output.name,
                    content_type=output.content_type,
                    path=path,
                    sha256=output.object_ref.sha256,
                    size=len(data),
                    object_ref=output.object_ref,
                )
            )
            workspace_entries.append(
                WorkspaceEntry(path=path, sha256=output.object_ref.sha256, size=len(data))
            )
            decoded_outputs.append(data)
        if (
            sum(entry.size for entry in self.context.workspace_manifest)
            + sum(entry.size for entry in workspace_entries)
            > grant.workspace_limit_bytes
        ):
            raise RuntimeAdapterError("CSV compute outputs exceed the approved workspace limit")
        receipt = ScientificResultReceiptV2(
            receipt_version=2,
            tool_call_id=tool_call_id,
            grant_id=grant_id,
            binding_sha256=binding_sha256,
            operation_id=request.operation_id,
            effective_operation_id=envelope.operation_id,
            operation_fingerprint=envelope.operation_fingerprint,
            input_manifest_sha256=compute_input_manifest_sha256(grant),
            recipe_manifest_sha256=grant.recipe_manifest_sha256,
            profile_id=grant.profile_id,
            profile_version=grant.profile_version,
            image_digest=grant.image_digest,
            input_sha256=grant.input_sha256,
            outputs=result_outputs,
        )
        try:
            report = decoded_outputs[3].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeAdapterError("CSV compute report is not UTF-8") from exc
        return report, receipt, workspace_entries

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
        if not isinstance(message, dict):
            raise RuntimeAdapterError("Chat Completions assistant message outside checkpoint profile")
        # OpenRouter/GLM includes provider-only metadata that Hermes can ignore.
        # Strip only those documented response keys before applying our strict
        # checkpoint schema; unknown fields remain a validation error.
        normalized = {
            key: value
            for key, value in message.items()
            if key not in {"refusal", "reasoning", "reasoning_details"}
        }
        tool_calls = normalized.get("tool_calls")
        if isinstance(tool_calls, list):
            normalized["tool_calls"] = [
                {key: value for key, value in call.items() if key != "index"}
                if isinstance(call, dict)
                else call
                for call in tool_calls
            ]
        try:
            from scientist.model_payload import ChatMessage

            checked = ChatMessage.model_validate(normalized)
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
    """Construct pinned Hermes with Todo and approved scientific tools only.

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

    adapter._native_active_tool_calls = {}
    binding = adapter.context.plan.scientific
    instruction_bundle = getattr(adapter, "scientific_instruction_bundle", None)
    if binding is not None:
        if not isinstance(instruction_bundle, InstructionBundle):
            raise RuntimeAdapterError("approved scientific instructions were not loaded")
        if instruction_bundle.capability_ids != tuple(binding.capability_ids):
            raise RuntimeAdapterError("loaded scientific instructions differ from approved capabilities")
        adapter._native_scientific_outputs = {}

        def require_active_call(tool_name: str, arguments: dict[str, Any]) -> str:
            tool_call_id = _NATIVE_TOOL_CALL_ID.get()
            if not isinstance(tool_call_id, str) or tool_call_id not in adapter._native_active_tool_calls:
                raise RuntimeAdapterError("scientific handler has no issued native tool-call identity")
            authority = _NATIVE_TOOL_CALL_AUTHORITY.get()
            if authority is None or authority[0] != tool_call_id:
                raise RuntimeAdapterError("scientific handler is outside native dispatcher authority")
            if tool_call_id not in adapter._native_consumed_tool_calls:
                raise RuntimeAdapterError("scientific handler has no consumed native tool-call identity")
            if tool_name not in _REVIEWED_NATIVE_SCIENTIFIC_TOOL_NAMES:
                raise RuntimeAdapterError("scientific handler is outside the reviewed native surface")
            if authority[1] != tool_name or authority[2] != canonical_bytes(arguments):
                raise RuntimeAdapterError("scientific handler call differs from its issued native call")
            _validate_native_tool_arguments(tool_name, arguments, binding)
            return tool_call_id

        def instruction_view_handler(arguments: dict[str, Any], **_kwargs: Any) -> str:
            call_id = require_active_call("instruction_view", arguments)
            adapter._native_scientific_outputs[call_id] = {"kind": "instruction_view"}
            return instruction_bundle.text

        def scientific_resources_handler(arguments: dict[str, Any], **_kwargs: Any) -> str:
            call_id = require_active_call("scientific_resources", arguments)
            from scientist.resource_recipe import (
                build_artifact_descriptor,
                canonical_resource_result,
                collect_worker_resources,
                get_available_resources_recipe,
                validate_resource_result,
            )

            resources = collect_worker_resources()
            if resources.memory_limit_bytes is not None and resources.memory_limit_bytes > binding.memory_limit_bytes:
                raise RuntimeAdapterError("worker memory limit exceeds approved scientific binding")
            result = canonical_resource_result(
                get_available_resources_recipe(resources),
                profile_id=binding.profile_id,
                instruction_fingerprint=binding.instruction_fingerprint,
            )
            validate_resource_result(
                result,
                profile_id=binding.profile_id,
                instruction_fingerprint=binding.instruction_fingerprint,
                max_bytes=binding.max_result_bytes,
            )
            descriptor = build_artifact_descriptor(
                result,
                profile_id=binding.profile_id,
                instruction_fingerprint=binding.instruction_fingerprint,
                max_bytes=binding.max_result_bytes,
            )
            path = "outputs/resources.json"
            if sum(entry.size for entry in adapter.context.workspace_manifest) + descriptor.size_bytes > binding.workspace_limit_bytes:
                raise RuntimeAdapterError("scientific result exceeds approved workspace limit")
            _write_scientific_result(workspace_dir, path, result)
            raw_call_id = adapter._native_active_tool_calls[call_id]
            binding_hash = hashlib.sha256(canonical_bytes(binding.model_dump(mode="json"))).hexdigest()
            receipt = ScientificResultReceipt(
                tool_call_id=raw_call_id,
                capability_id=descriptor.recipe_id,
                binding_sha256=binding_hash,
                recipe_version="1",
                path=path,
                sha256=descriptor.result_sha256,
                size=descriptor.size_bytes,
            )
            entry = WorkspaceEntry(path=path, sha256=descriptor.result_sha256, size=descriptor.size_bytes)
            adapter._native_scientific_outputs[call_id] = {"receipt": receipt, "workspace_entry": entry}
            return "Measured approved worker resource limits and saved outputs/resources.json."

        def scientific_search_handler(arguments: dict[str, Any], **_kwargs: Any) -> str:
            call_id = require_active_call("scientific_search", arguments)
            if not isinstance(binding, ScientificBindingV2):
                raise RuntimeAdapterError("approved Crossref binding is unavailable")
            raw_call_id = adapter._native_active_tool_calls[call_id]
            result = adapter._run_approved_search(arguments["request_id"], raw_call_id)
            adapter._native_scientific_outputs[call_id] = {
                "kind": "scientific_search",
                "operation_id": next(
                    item.operation_id for item in reversed(adapter.context.operation_mappings)
                    if item.tool_call_id == raw_call_id and item.purpose == "tool"
                ),
            }
            return json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

        def scientific_csv_describe_handler(arguments: dict[str, Any], **_kwargs: Any) -> str:
            call_id = require_active_call("scientific_csv_describe", arguments)
            if not isinstance(binding, ScientificBindingV2):
                raise RuntimeAdapterError("approved CSV grant is unavailable")
            raw_call_id = adapter._native_active_tool_calls[call_id]
            report, receipt, workspace_entries = adapter._run_approved_csv_describe(
                arguments["grant_id"], raw_call_id,
            )
            adapter._native_scientific_outputs[call_id] = {
                "receipt_v2": receipt,
                "workspace_entries_v2": workspace_entries,
            }
            return report

        def scientific_plot_handler(arguments: dict[str, Any], **_kwargs: Any) -> str:
            call_id = require_active_call("scientific_plot", arguments)
            svg = render_plot_svg(arguments)
            if binding is None:
                raise RuntimeAdapterError("approved visualization binding is unavailable")
            raw_call_id = adapter._native_active_tool_calls[call_id]
            if (
                sum(entry.size for entry in adapter.context.workspace_manifest) + len(svg.encode("utf-8"))
                > (binding.workspace_limit_bytes if isinstance(binding, ScientificBinding) else MAX_WORKSPACE_BYTES)
            ):
                raise RuntimeAdapterError("scientific plot exceeds approved output limits")
            receipt, entry = _persist_scientific_plot(workspace_dir, binding, raw_call_id, svg)
            adapter._native_scientific_outputs[call_id] = {
                "receipt": receipt,
                "workspace_entry": entry,
            }
            return json.dumps(
                {
                    "chart_spec": arguments,
                    "content_type": "image/svg+xml",
                    "path": receipt.path,
                    "sha256": receipt.sha256,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )

        handlers = {
            "instruction_view": instruction_view_handler,
            "scientific_resources": scientific_resources_handler,
            "scientific_search": scientific_search_handler,
            "scientific_csv_describe": scientific_csv_describe_handler,
            "scientific_plot": scientific_plot_handler,
        }
        for schema in _native_scientific_tool_definitions(binding):
            name = schema["name"]
            handler = handlers[name]
            existing = model_tools.registry.get_entry(name)
            if existing is not None and not getattr(existing.handler, "_scientist_native_handler", False):
                raise RuntimeAdapterError("scientific native tool name is already registered")
            handler._scientist_native_handler = True
            model_tools.registry.register(
                name=name,
                toolset="todo",
                schema=schema,
                handler=handler,
                description=schema["description"],
                max_result_size_chars=1_000_000,
            )

    original_handle_function_call = getattr(model_tools, "handle_function_call", None)
    if getattr(original_handle_function_call, "_scientist_native_context", False):
        original_handle_function_call = getattr(
            original_handle_function_call, "_scientist_original_dispatcher", None
        )
    if binding is not None and not callable(original_handle_function_call):
        raise RuntimeAdapterError("pinned Hermes tool dispatcher is unavailable")
    if callable(original_handle_function_call):
        @wraps(original_handle_function_call)
        def native_tool_call_context(*args: Any, **kwargs: Any) -> Any:
            tool_call_id = kwargs.get("tool_call_id")
            if tool_call_id is None and len(args) > 3:
                tool_call_id = args[3]
            tool_name = kwargs.get("function_name")
            if tool_name is None and args:
                tool_name = args[0]
            arguments = kwargs.get("function_args")
            if arguments is None and len(args) > 1:
                arguments = args[1]
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise RuntimeAdapterError("native tool arguments are invalid") from exc
            if (
                not isinstance(tool_call_id, str)
                or tool_call_id not in adapter._native_active_tool_calls
            ):
                raise RuntimeAdapterError("native dispatcher has no issued tool-call identity")
            if tool_call_id in adapter._native_consumed_tool_calls:
                raise RuntimeAdapterError("native tool-call identity was already dispatched")
            allowed_names = _REVIEWED_NATIVE_TODO_TOOL_NAMES | {_REVIEWED_NATIVE_TODO_BRIDGE}
            if binding is not None:
                allowed_names |= _REVIEWED_NATIVE_SCIENTIFIC_TOOL_NAMES
            if not isinstance(tool_name, str) or tool_name not in allowed_names:
                raise RuntimeAdapterError("native dispatcher call is outside the reviewed surface")
            agent = getattr(adapter, "_native_dispatch_agent", None)
            if agent is None:
                raise RuntimeAdapterError("native dispatcher has no factory agent")
            raw_call_id = adapter._native_active_tool_calls[tool_call_id]
            pending = adapter.context.pending_assistant
            if pending is None or pending.message_index >= len(adapter.context.messages):
                raise RuntimeAdapterError("native dispatcher has no saved assistant batch")
            saved_calls = adapter.context.messages[pending.message_index].tool_calls or []
            saved_call = next(
                (call for call in saved_calls if _native_tool_call_mapping(call).get("id") == raw_call_id), None
            )
            if saved_call is None:
                raise RuntimeAdapterError("native dispatcher identity is not in the saved assistant batch")
            saved_name, saved_arguments = _validate_native_todo_call(agent, saved_call)
            incoming_name, incoming_arguments = _validate_native_todo_call(
                agent, {"function": {"name": tool_name, "arguments": arguments}}
            )
            if (
                incoming_name != saved_name
                or canonical_bytes(incoming_arguments) != canonical_bytes(saved_arguments)
            ):
                raise RuntimeAdapterError("native dispatcher call differs from its saved assistant call")
            adapter._native_consumed_tool_calls.add(tool_call_id)
            authority = (tool_call_id, incoming_name, canonical_bytes(incoming_arguments))
            token = _NATIVE_TOOL_CALL_ID.set(tool_call_id)
            authority_token = _NATIVE_TOOL_CALL_AUTHORITY.set(authority)
            try:
                return original_handle_function_call(*args, **kwargs)
            finally:
                _NATIVE_TOOL_CALL_AUTHORITY.reset(authority_token)
                _NATIVE_TOOL_CALL_ID.reset(token)

        native_tool_call_context._scientist_native_context = True
        native_tool_call_context._scientist_original_dispatcher = original_handle_function_call
        model_tools.handle_function_call = native_tool_call_context

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
                saved = _native_tool_call_mapping(saved_call)
                live = _native_tool_call_mapping(call)
                saved_function = saved.get("function") or {}
                live_function = live.get("function") or {}
                if (
                    saved_function.get("name") != live_function.get("name")
                    or saved_function.get("arguments") != live_function.get("arguments")
                ):
                    raise RuntimeAdapterError("native tool suffix differs from its saved assistant call")
                self._scientific_binding = binding
                _validate_native_todo_call(self, live)
                if sum(1 for item in suffix_calls if item.function.name in {"scientific_resources", "scientific_csv_describe"}) > 1:
                    raise RuntimeAdapterError("native tool batch repeats scientific computation")
            if adapter.context.scientific_results and any(
                item.function.name == "scientific_resources" for item in suffix_calls
            ):
                raise RuntimeAdapterError("scientific computation already committed receipt")
            if adapter.context.scientific_results_v2 and any(
                item.function.name == "scientific_csv_describe" for item in suffix_calls
            ):
                raise RuntimeAdapterError("CSV computation already committed receipt")
            adapter.context = adapter.context.model_copy(
                update={"boundary": "before_tool", "pending_assistant": pending}
                )
            adapter._checkpoint("before_tool")
            adapter._native_active_tool_calls = {
                item.applied_id: item.raw_id for item in applied[pending.next_tool_index :]
            }
            adapter._native_consumed_tool_calls = set()
            adapter._native_dispatch_agent = self
            adapter._native_scientific_outputs = {}
            try:
                super()._execute_tool_calls(assistant_message, messages, effective_task_id, api_call_count)
            finally:
                adapter._native_active_tool_calls = {}
                adapter._native_consumed_tool_calls = set()
                adapter._native_dispatch_agent = None
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
            for item in suffix_calls:
                if item.function.name in _REVIEWED_NATIVE_SCIENTIFIC_TOOL_NAMES:
                    applied_id = raw_to_applied[item.id]
                    if applied_id not in adapter._native_scientific_outputs or item.id not in completed:
                        raise RuntimeAdapterError("scientific tool result was not produced and completed")
            new_receipts = []
            new_workspace = []
            for applied_id, output in adapter._native_scientific_outputs.items():
                if output.get("receipt") is None:
                    continue
                raw_id = adapter._native_active_tool_calls.get(applied_id) or next(
                    (item.raw_id for item in applied if item.applied_id == applied_id), None
                )
                if raw_id is None or raw_id not in completed:
                    raise RuntimeAdapterError("scientific receipt is not bound to a completed tool call")
                receipt = output["receipt"].model_copy(update={"tool_call_id": raw_id})
                new_receipts.append(receipt)
                new_workspace.append(output["workspace_entry"])
            new_receipts_v2, new_workspace_v2 = _collect_native_v2_receipts(
                adapter._native_scientific_outputs,
                reverse,
                completed,
            )
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
                    "scientific_results": [*adapter.context.scientific_results, *new_receipts],
                    "scientific_results_v2": [
                        *adapter.context.scientific_results_v2, *new_receipts_v2,
                    ],
                    "workspace_manifest": [
                        *adapter.context.workspace_manifest, *new_workspace, *new_workspace_v2,
                    ],
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
        reviewed_names = set(_REVIEWED_NATIVE_TODO_TOOL_NAMES)
        if binding is not None:
            reviewed_names.update(_REVIEWED_NATIVE_SCIENTIFIC_TOOL_NAMES)
        # Keep Tool Search/RPC schemas out of the model-visible surface. If a
        # saved/deferred call reaches dispatch anyway, the preflight and handler
        # checks independently scope it to these same concrete tools.
        return [
            item for item in definitions
            if (item.get("function") or {}).get("name") in reviewed_names
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
    agent._scientific_binding = binding
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
