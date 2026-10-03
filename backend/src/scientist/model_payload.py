"""Typed, bounded Chat Completions payloads shared by trusted runtime code."""

from __future__ import annotations

import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator

_MAX_MESSAGES = 1000
_MAX_MESSAGE_BYTES = 128 * 1024
_MAX_SCHEMA_BYTES = 32 * 1024
_MAX_BODY_BYTES = 240 * 1024
_MAX_SCHEMA_DEPTH = 16
_MAX_SCHEMA_NODES = 4096
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ModelPayloadError(ValueError):
    """Raised when an untrusted model request falls outside the typed wire profile."""


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FunctionCall(_WireModel):
    name: StrictStr = Field(min_length=1, max_length=64)
    arguments: StrictStr = Field(max_length=32 * 1024)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("invalid function name")
        return value


class ToolCall(_WireModel):
    id: StrictStr = Field(min_length=1, max_length=128)
    type: Literal["function"]
    function: FunctionCall

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("invalid tool-call ID")
        return value


class ChatMessage(_WireModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: StrictStr | None = Field(max_length=64 * 1024)
    name: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    tool_call_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    tool_calls: list[ToolCall] | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def role_fields(self) -> ChatMessage:
        if self.content is None and self.role != "assistant":
            raise ValueError("only assistant content may be null")
        if self.role == "tool":
            if self.tool_call_id is None or self.tool_calls is not None:
                raise ValueError("tool messages require only a tool_call_id")
        elif self.tool_call_id is not None:
            raise ValueError("tool_call_id is valid only on tool messages")
        if self.role != "assistant" and self.tool_calls is not None:
            raise ValueError("tool_calls are valid only on assistant messages")
        if self.tool_call_id is not None and not _ID.fullmatch(self.tool_call_id):
            raise ValueError("invalid tool-call ID")
        return self


def validate_messages(messages: list, *, allow_pending: bool = True) -> list[dict[str, Any]]:
    """Validate text-only Chat Completions history and preserve its wire values."""
    if not isinstance(messages, list) or not 1 <= len(messages) <= _MAX_MESSAGES:
        raise ModelPayloadError("messages must contain between 1 and 1000 entries")
    try:
        parsed = [ChatMessage.model_validate(message) for message in messages]
        normalized = [message.model_dump(mode="json", exclude_unset=True) for message in parsed]
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ModelPayloadError("invalid Chat Completions messages") from exc
    if len(encoded) > _MAX_MESSAGE_BYTES:
        raise ModelPayloadError("messages exceed the 131072 byte limit")

    known: set[str] = set()
    answered: set[str] = set()
    pending: list[str] = []
    next_pending = 0
    for message in parsed:
        if next_pending < len(pending):
            if message.role != "tool":
                raise ModelPayloadError("pending tool results must form a contiguous batch")
            if message.tool_call_id != pending[next_pending]:
                raise ModelPayloadError("pending tool results must follow tool-call order")
            next_pending += 1
            answered.add(message.tool_call_id)
            continue
        pending = []
        next_pending = 0
        if message.tool_calls:
            for call in message.tool_calls:
                if call.id in known:
                    raise ModelPayloadError("duplicate tool-call ID")
                known.add(call.id)
                pending.append(call.id)
        if message.role == "tool":
            call_id = message.tool_call_id
            if call_id not in known or call_id in answered or not pending:
                raise ModelPayloadError("orphan or duplicate tool response")
            raise ModelPayloadError("tool responses must follow tool-call order")
    if next_pending < len(pending) and not allow_pending:
        raise ModelPayloadError("outbound messages contain pending tool results")
    return normalized


def _validate_json_value(value: Any, *, depth: int = 0, count: list[int] | None = None) -> None:
    if count is None:
        count = [0]
    count[0] += 1
    if count[0] > _MAX_SCHEMA_NODES or depth > _MAX_SCHEMA_DEPTH:
        raise ModelPayloadError("JSON schema is too deeply nested or too large")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ModelPayloadError("non-finite JSON values are forbidden")
        return
    if isinstance(value, list):
        for child in value:
            _validate_json_value(child, depth=depth + 1, count=count)
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ModelPayloadError("JSON object keys must be strings")
            if key == "$ref" and (not isinstance(child, str) or not (child == "#" or child.startswith("#/"))):
                raise ModelPayloadError("remote JSON schema references are forbidden")
            _validate_json_value(child, depth=depth + 1, count=count)
        return
    raise ModelPayloadError("value is not JSON serializable")


def _validate_schema_shape(value: dict[str, Any], *, root: bool = False) -> None:
    if root and value.get("type") != "object":
        raise ModelPayloadError("function parameters must be a JSON object schema")
    schema_type = value.get("type")
    valid_types = {"null", "boolean", "object", "array", "number", "integer", "string"}
    valid_type = (isinstance(schema_type, str) and schema_type in valid_types) or (
        isinstance(schema_type, list) and schema_type and all(isinstance(item, str) and item in valid_types for item in schema_type)
    )
    if "type" in value and not valid_type:
        raise ModelPayloadError("invalid JSON schema type")
    properties = value.get("properties", {})
    if not isinstance(properties, dict) or any(not isinstance(key, str) or not isinstance(schema, dict) for key, schema in properties.items()):
        raise ModelPayloadError("invalid JSON schema properties")
    required = value.get("required", [])
    if not isinstance(required, list) or any(not isinstance(name, str) for name in required) or len(set(required)) != len(required):
        raise ModelPayloadError("invalid JSON schema required list")
    for child in properties.values():
        _validate_schema_shape(child)
    for key in ("items", "additionalProperties"):
        child = value.get(key)
        if isinstance(child, dict):
            _validate_schema_shape(child)
        elif isinstance(child, list) and key == "items":
            for item in child:
                if not isinstance(item, dict):
                    raise ModelPayloadError("invalid tuple schema")
                _validate_schema_shape(item)
        elif key in value and key == "items" and not isinstance(child, list):
            raise ModelPayloadError("invalid items schema")
        elif key in value and key == "additionalProperties" and not isinstance(child, bool):
            raise ModelPayloadError("invalid additionalProperties schema")
    definitions = value.get("$defs", {})
    if not isinstance(definitions, dict) or any(not isinstance(schema, dict) for schema in definitions.values()):
        raise ModelPayloadError("invalid JSON schema definitions")
    for child in definitions.values():
        _validate_schema_shape(child)
    for key in ("allOf", "anyOf", "oneOf"):
        branches = value.get(key)
        if branches is not None:
            if not isinstance(branches, list) or not branches or any(not isinstance(branch, dict) for branch in branches):
                raise ModelPayloadError("invalid JSON schema branches")
            for branch in branches:
                _validate_schema_shape(branch)
    for key in ("not", "if", "then", "else"):
        child = value.get(key)
        if child is not None:
            if not isinstance(child, dict):
                raise ModelPayloadError("invalid conditional JSON schema")
            _validate_schema_shape(child)


class FunctionDefinition(_WireModel):
    name: StrictStr = Field(min_length=1, max_length=64)
    description: StrictStr | None = Field(default=None, max_length=1024)
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    strict: StrictBool | None = None

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("invalid function name")
        return value

    @field_validator("parameters")
    @classmethod
    def bounded_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_json_value(value)
        _validate_schema_shape(value, root=True)
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ModelPayloadError("invalid JSON schema") from exc
        if len(encoded) > _MAX_SCHEMA_BYTES:
            raise ModelPayloadError("tool schema exceeds the 32768 byte limit")
        return value


class ToolDefinition(_WireModel):
    type: Literal["function"]
    function: FunctionDefinition


class _FunctionChoice(_WireModel):
    name: StrictStr = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("invalid function name")
        return value


class FunctionToolChoice(_WireModel):
    type: Literal["function"]
    function: _FunctionChoice


ToolChoice = Literal["none", "auto", "required"] | FunctionToolChoice


def validate_tools(tools: list) -> list[dict[str, Any]]:
    if not isinstance(tools, list) or not 1 <= len(tools) <= 128:
        raise ModelPayloadError("tools must contain between 1 and 128 definitions")
    try:
        parsed = [ToolDefinition.model_validate(tool) for tool in tools]
        names = [tool.function.name for tool in parsed]
        if len(set(names)) != len(names):
            raise ModelPayloadError("tool names must be unique")
        normalized = [tool.model_dump(mode="json", exclude_unset=True) for tool in parsed]
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ModelPayloadError("invalid tool definitions") from exc
    if len(encoded) > _MAX_BODY_BYTES:
        raise ModelPayloadError("tool definitions exceed the 245760 byte body limit")
    return normalized


def validate_tool_choice(choice: Any) -> str | dict[str, Any]:
    if isinstance(choice, str):
        if choice not in {"none", "auto", "required"}:
            raise ModelPayloadError("invalid tool choice")
        return choice
    try:
        return FunctionToolChoice.model_validate(choice).model_dump(mode="json")
    except (TypeError, ValueError) as exc:
        raise ModelPayloadError("invalid tool choice") from exc


def _check_tool_choice(choice: str | dict[str, Any], tools: list[dict[str, Any]]) -> None:
    if isinstance(choice, dict):
        requested = choice["function"]["name"]
        if requested not in {tool["function"]["name"] for tool in tools}:
            raise ModelPayloadError("tool_choice names an undefined tool")


_CONTROL_ORDER = (
    "temperature",
    "top_p",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "parallel_tool_calls",
    "logprobs",
    "top_logprobs",
)
_CONTROL_KEYS = set(_CONTROL_ORDER)


def _controls(controls: dict[str, Any]) -> dict[str, Any]:
    unknown = set(controls) - _CONTROL_KEYS - {"tools", "tool_choice"}
    if unknown:
        raise ModelPayloadError("unsupported Chat Completions fields")
    result: dict[str, Any] = {}
    for key in _CONTROL_ORDER:
        if key not in controls:
            continue
        value = controls[key]
        if key in {"temperature", "top_p"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ModelPayloadError(f"invalid {key}")
            low, high = (0, 2) if key == "temperature" else (0, 1)
            if not low <= value <= high:
                raise ModelPayloadError(f"invalid {key}")
        elif key in {"presence_penalty", "frequency_penalty"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not -2 <= value <= 2:
                raise ModelPayloadError(f"invalid {key}")
        elif key == "seed":
            if isinstance(value, bool) or not isinstance(value, int) or not -(2**31) <= value < 2**31:
                raise ModelPayloadError("invalid seed")
        elif key == "stop":
            if isinstance(value, str):
                if not 1 <= len(value) <= 1024:
                    raise ModelPayloadError("invalid stop")
            elif isinstance(value, list) and 1 <= len(value) <= 4 and all(isinstance(item, str) and 1 <= len(item) <= 1024 for item in value):
                pass
            else:
                raise ModelPayloadError("invalid stop")
        elif key in {"parallel_tool_calls", "logprobs"}:
            if not isinstance(value, bool):
                raise ModelPayloadError(f"invalid {key}")
        elif key == "top_logprobs":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 20:
                raise ModelPayloadError("invalid top_logprobs")
        result[key] = value
    for key in ("tools", "tool_choice"):
        if key in controls:
            result[key] = controls[key]
    if "top_logprobs" in result and (not result.get("logprobs") or not 0 <= result["top_logprobs"] <= 20):
        raise ModelPayloadError("top_logprobs requires logprobs")
    return result


def build_chat_completion_body(
    model: str,
    messages: list,
    max_output_tokens: int,
    **controls: Any,
) -> dict[str, Any]:
    """Build the native HTTP body from the explicitly supported wire profile."""
    if not isinstance(model, str) or not model or len(model) > 200:
        raise ModelPayloadError("invalid model")
    if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens < 1:
        raise ModelPayloadError("invalid output limit")
    body: dict[str, Any] = {
        "model": model,
        "messages": validate_messages(messages, allow_pending=False),
        "max_tokens": max_output_tokens,
    }
    values = _controls(controls)
    if "tools" in values:
        values["tools"] = validate_tools(values["tools"])
    if "tool_choice" in values:
        values["tool_choice"] = validate_tool_choice(values["tool_choice"])
    if "tool_choice" in values and "tools" not in values:
        raise ModelPayloadError("tool_choice requires tools")
    if "tool_choice" in values:
        _check_tool_choice(values["tool_choice"], values["tools"])
    body.update(values)
    try:
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ModelPayloadError("invalid Chat Completions body") from exc
    if len(encoded) > _MAX_BODY_BYTES:
        raise ModelPayloadError("Chat Completions body exceeds the 245760 byte limit")
    return body


def serialized_input_bytes(messages: list, **controls: Any) -> int:
    """Count serialized message and schema/control bytes for broker reservations."""
    normalized_messages = validate_messages(messages, allow_pending=False)
    normalized_controls = _controls(controls)
    if "tools" in normalized_controls:
        normalized_controls["tools"] = validate_tools(normalized_controls["tools"])
    if "tool_choice" in normalized_controls:
        normalized_controls["tool_choice"] = validate_tool_choice(normalized_controls["tool_choice"])
    if "tool_choice" in normalized_controls and "tools" not in normalized_controls:
        raise ModelPayloadError("tool_choice requires tools")
    if "tool_choice" in normalized_controls:
        _check_tool_choice(normalized_controls["tool_choice"], normalized_controls["tools"])
    value = {"messages": normalized_messages, **normalized_controls}
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ModelPayloadError("invalid Chat Completions input") from exc
    if len(encoded) > _MAX_BODY_BYTES:
        raise ModelPayloadError("Chat Completions input exceeds the 245760 byte limit")
    return len(encoded)
