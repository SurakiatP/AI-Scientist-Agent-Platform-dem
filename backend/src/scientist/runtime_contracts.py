"""DB-free, non-executable snapshots for the private worker/controller boundary.

These schemas validate representation. The controller must additionally bind
identity, immutable approval, pins and ledger records to authoritative storage.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from scientist.contracts import CheckpointManifest, OperationRequest, PlanSpec
from scientist.model_payload import ChatMessage, validate_messages

RUNTIME_COMMIT = "bd0affe5e5f723579df8902852f5d0c47795f355"
MAX_CONTEXT_BYTES = 1024 * 1024
MAX_WORKSPACE_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_WORKSPACE_FILES = 1024
MAX_BOUNDARY_BYTES = 90 * 1024 * 1024
Digest = Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{64}$")]
Nonnegative = Annotated[StrictInt, Field(ge=0)]
Positive = Annotated[StrictInt, Field(ge=1)]
Text = Annotated[StrictStr, Field(max_length=64 * 1024)]
Identifier = Annotated[StrictStr, Field(min_length=1, max_length=200)]


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def operation_fingerprint(request: OperationRequest) -> str:
    identity = request.model_dump(mode="json")
    identity.pop("generation")
    return hashlib.sha256(canonical_bytes(identity)).hexdigest()


def _bounded_depth(value: Any, depth: int = 0) -> None:
    if depth > 16:
        raise ValueError("JSON exceeds depth 16")
    if isinstance(value, dict):
        for item in value.values():
            _bounded_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _bounded_depth(item, depth + 1)


def validate_workspace_path(path: str) -> str:
    if (not path or len(path.encode("utf-8")) > 240 or "\\" in path
            or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or ":" in path):
        raise ValueError("workspace path must be canonical relative POSIX")
    return path


def _unique_paths(files: list) -> None:
    paths = {item.path for item in files}
    if len(paths) != len(files):
        raise ValueError("duplicate workspace path")
    for path in paths:
        parts = path.split("/")
        if any("/".join(parts[:i]) in paths for i in range(1, len(parts))):
            raise ValueError("workspace file conflicts with parent file")


class RuntimeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def strict_schema_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema version must be an integer")
        return value


class BootstrapMetadata(RuntimeRecord):
    schema_version: Literal[1]
    checkpoint_revision: Nonnegative


class TodoEntry(RuntimeRecord):
    id: Identifier
    content: Annotated[StrictStr, Field(min_length=1, max_length=4000)]
    status: Literal["pending", "in_progress", "completed", "cancelled"]
    parent: Identifier | None = None


class TodoSnapshot(RuntimeRecord):
    todos: list[TodoEntry] = Field(max_length=256)
    revision: Nonnegative

    @model_validator(mode="after")
    def valid_graph(self):
        parents = {item.id: item.parent for item in self.todos}
        if len(parents) != len(self.todos):
            raise ValueError("duplicate Todo id")
        for entry in self.todos:
            seen = {entry.id}
            parent = entry.parent
            while parent is not None:
                if parent not in parents or parent in seen:
                    raise ValueError("invalid Todo parent or cycle")
                seen.add(parent)
                parent = parents[parent]
        return self


class MicroCompactionState(RuntimeRecord):
    passes: Nonnegative = 0
    tokens_saved_total: Nonnegative = 0
    turns_since_pass: Nonnegative = 0
    cursor: Nonnegative = 0
    rolling_summary: Text = ""
    consecutive_failures: Nonnegative = 0
    last_failure_cursor: Annotated[StrictInt, Field(ge=-1)] = -1
    enabled: StrictBool = False
    defrag_threshold_tokens: Positive = 2000


class CompactedContext(RuntimeRecord):
    compression_count: Nonnegative
    previous_summary: Text | None
    summary_has_user_turn: StrictBool | None
    ineffective_compression_count: Nonnegative
    micro: MicroCompactionState


class AppliedToolId(RuntimeRecord):
    raw_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    applied_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]


class PendingAssistant(RuntimeRecord):
    turn_id: UUID
    message_index: Nonnegative
    next_tool_index: Nonnegative
    applied_tool_ids: list[AppliedToolId] = Field(default_factory=list, max_length=128)


class OperationMapping(RuntimeRecord):
    operation_id: Identifier
    turn_id: UUID
    purpose: Literal["model", "compression", "tool"]
    model_sequence: Nonnegative | None
    tool_call_id: Annotated[StrictStr, Field(min_length=1, max_length=128)] | None
    request: OperationRequest
    payload_hash: Digest

    @field_validator("request", mode="before")
    @classmethod
    def strict_request_numbers(cls, value):
        data = value.model_dump() if isinstance(value, OperationRequest) else value
        if isinstance(data, dict):
            for key in ("generation", "reserve_tokens"):
                if type(data.get(key)) is not int or data[key] < 0:
                    raise ValueError("operation numbers must be nonnegative integers")
        return value

    @model_validator(mode="after")
    def bound_request(self):
        if self.operation_id != self.request.operation_id or self.payload_hash != operation_fingerprint(self.request):
            raise ValueError("operation mapping does not match request")
        if self.purpose == "tool":
            if self.tool_call_id is None or self.model_sequence is not None:
                raise ValueError("tool mapping requires only tool call identity")
        elif self.tool_call_id is not None or self.model_sequence is None or self.request.kind != "llm":
            raise ValueError("model/compression mapping requires LLM sequence identity")
        return self


class WorkspaceEntry(RuntimeRecord):
    path: StrictStr
    sha256: Digest
    size: Annotated[StrictInt, Field(ge=0, le=MAX_FILE_BYTES)]

    @field_validator("path")
    @classmethod
    def canonical_path(cls, value):
        return validate_workspace_path(value)


def validate_native_timestamp(value):
    if type(value) in (int, float):
        if value < 0 or (type(value) is float and not math.isfinite(value)):
            raise ValueError("native timestamp must be finite nonnegative seconds")
        return value
    if type(value) is str and 1 <= len(value) <= 64:
        datetime.fromisoformat(value)
        return value
    raise ValueError("native timestamp must be numeric seconds or bounded ISO text")


NativeTimestamp = Annotated[Any, BeforeValidator(validate_native_timestamp)]


class NativeMessageMetadata(RuntimeRecord):
    message_index: Nonnegative
    timestamp: NativeTimestamp


class RuntimeContextV1(RuntimeRecord):
    schema_version: Literal[1]
    run_id: UUID
    project_id: UUID
    generation: Positive
    revision: Nonnegative
    input_snapshot_digest: Digest
    plan_digest: Digest
    runtime_commit: Literal[RUNTIME_COMMIT]
    image_digest: Annotated[StrictStr, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
    skills_digest: Digest
    environment_digest: Digest
    provider_id: UUID
    provider_endpoint: Annotated[StrictStr, Field(min_length=1, max_length=500)]
    model: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    plan: PlanSpec
    turn_id: UUID
    system_prompt: Annotated[StrictStr, Field(max_length=MAX_CONTEXT_BYTES)]
    messages: list[ChatMessage] = Field(max_length=1000)
    native_message_metadata: list[NativeMessageMetadata] = Field(default_factory=list, max_length=1000)
    current_turn_user_index: Nonnegative | None = None
    native_turn_timestamp: NativeTimestamp | None = None
    todo: TodoSnapshot
    compacted_context: CompactedContext | None
    boundary: Literal["before_model", "model_committed", "before_tool", "tool_committed", "final"]
    pending_assistant: PendingAssistant | None
    operation_mappings: list[OperationMapping] = Field(max_length=256)
    operation_sequence: Nonnegative
    workspace_manifest: list[WorkspaceEntry] = Field(max_length=MAX_WORKSPACE_FILES)
    budget_remaining_tokens: Nonnegative | None = None
    """ADR-012 trusted per-generation budget snapshot (advisory to the worker).

    max(0, runs.token_limit - usage_tokens - reserved_tokens) read from the ledger by
    the controller. Only server code sets it: supervisor.start overwrites whatever the
    bootstrap factory supplied, WorkerController.bootstrap_context rebinds it on every
    continuation and boundary() stores the ledger value, so a worker value is never
    trusted or persisted. The approved plan (and plan.token_limit) is never changed.
    Additive optional field, so schema_version stays 1 (same convention as
    native_message_metadata); pre-ADR checkpoints parse as None and are rebound at
    bootstrap. A worker that receives None fails closed before any I/O.
    """

    @model_validator(mode="before")
    @classmethod
    def depth_bound(cls, value):
        _bounded_depth(value)
        if isinstance(value, dict) and type(value.get("schema_version")) is not int:
            raise ValueError("schema version must be an integer")
        if isinstance(value, dict):
            plan = value.get("plan")
            plan = plan.model_dump() if isinstance(plan, PlanSpec) else plan
            if isinstance(plan, dict):
                for key in ("token_limit", "elapsed_limit_ms"):
                    if type(plan.get(key)) is not int:
                        raise ValueError("approval limits must be integers")
        return value

    @field_validator("messages", mode="before")
    @classmethod
    def conversation(cls, value):
        if isinstance(value, list):
            value = [item.model_dump(mode="json", exclude_unset=True) if isinstance(item, ChatMessage) else item for item in value]
        return validate_messages(value)

    @model_validator(mode="after")
    def context_invariants(self):
        plan = self.plan
        if (self.input_snapshot_digest != plan.input_snapshot_digest
                or self.plan_digest != hashlib.sha256(canonical_bytes(plan.model_dump(mode="json"))).hexdigest()
                or self.provider_id != plan.provider_id or self.model != plan.model
                or self.provider_endpoint not in plan.data_recipients):
            raise ValueError("context changed approved plan identity")
        if len(canonical_bytes(self.model_dump(mode="json"))) > MAX_CONTEXT_BYTES:
            raise ValueError("context exceeds 1 MiB")
        metadata_indices = [item.message_index for item in self.native_message_metadata]
        if (len(set(metadata_indices)) != len(metadata_indices)
                or any(index >= len(self.messages) for index in metadata_indices)):
            raise ValueError("native metadata must bind unique canonical message indices")
        if self.current_turn_user_index is not None and (
                self.current_turn_user_index >= len(self.messages)
                or self.messages[self.current_turn_user_index].role != "user"):
            raise ValueError("current turn anchor must reference canonical primary user")
        _unique_paths(self.workspace_manifest)
        if sum(entry.size for entry in self.workspace_manifest) > MAX_WORKSPACE_BYTES:
            raise ValueError("workspace exceeds 64 MiB")
        operations = set()
        continuations = set()
        last_tool_calls = next((message.tool_calls for message in reversed(self.messages)
                                if message.role == "assistant" and message.tool_calls), [])
        active_tool_ids = {call.id for call in last_tool_calls}
        for mapping in self.operation_mappings:
            request = mapping.request
            key = (mapping.turn_id, mapping.purpose, mapping.model_sequence, mapping.tool_call_id)
            if mapping.operation_id in operations or key in continuations:
                raise ValueError("duplicate operation or continuation identity")
            operations.add(mapping.operation_id)
            continuations.add(key)
            if (request.run_id != self.run_id or request.generation > self.generation
                    or request.kind not in plan.allowed_ops or mapping.turn_id != self.turn_id):
                raise ValueError("operation is outside active approved continuation")
            if mapping.model_sequence is not None and mapping.model_sequence >= self.operation_sequence:
                raise ValueError("operation sequence is not allocated")
            if mapping.purpose == "tool" and mapping.tool_call_id not in active_tool_ids:
                raise ValueError("mapped tool call was not issued in the active continuation")
            if request.kind == "llm" and (request.payload.get("model") != self.model
                    or request.payload.get("provider_id") != str(self.provider_id)
                    or request.payload.get("recipient") != self.provider_endpoint):
                raise ValueError("mapped model request changed provider identity")
        self._pending_prefix()
        return self

    def _pending_prefix(self) -> None:
        outstanding = []
        assistant_index = None
        for index, message in enumerate(self.messages):
            if message.role == "assistant" and message.tool_calls:
                outstanding = [call.id for call in message.tool_calls]
                assistant_index = index
            elif message.role == "tool" and message.tool_call_id in outstanding:
                outstanding.remove(message.tool_call_id)
        pending = self.pending_assistant
        if pending is None:
            if outstanding:
                raise ValueError("pending tool calls require a continuation cursor")
            return
        if outstanding and self.boundary in {"before_model", "final"}:
            raise ValueError("model/final boundary cannot have unfinished tools")
        if pending.turn_id != self.turn_id or pending.message_index != assistant_index:
            raise ValueError("pending assistant identity mismatch")
        message = self.messages[pending.message_index]
        calls = message.tool_calls or []
        if pending.applied_tool_ids and (
                [item.raw_id for item in pending.applied_tool_ids] != [call.id for call in calls]
                or len({item.applied_id for item in pending.applied_tool_ids}) != len(calls)):
            raise ValueError("applied tool identities must map the entire ordered raw call batch")
        results = self.messages[pending.message_index + 1:]
        if (pending.next_tool_index > len(calls) or len(results) != pending.next_tool_index
                or any(item.role != "tool" or item.tool_call_id != calls[i].id for i, item in enumerate(results))
                or outstanding != [call.id for call in calls[pending.next_tool_index:]]):
            raise ValueError("pending tool results must form the exact completed prefix")


class WorkspaceFile(WorkspaceEntry):
    data_base64: Annotated[StrictStr, Field(max_length=((MAX_FILE_BYTES + 2) // 3) * 4)]

    def decoded_data(self) -> bytes:
        try:
            return base64.b64decode(self.data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("invalid base64 workspace content") from exc

    @model_validator(mode="after")
    def verify_content(self):
        data = self.decoded_data()
        if (len(data) != self.size or hashlib.sha256(data).hexdigest() != self.sha256
                or base64.b64encode(data).decode("ascii") != self.data_base64):
            raise ValueError("workspace size/hash/base64 mismatch")
        return self


class BoundaryRequest(RuntimeRecord):
    schema_version: Literal[1]
    boundary_id: UUID
    expected_checkpoint_revision: Nonnegative
    context: RuntimeContextV1
    workspace: list[WorkspaceFile] = Field(max_length=MAX_WORKSPACE_FILES)

    @model_validator(mode="before")
    @classmethod
    def depth_bound(cls, value):
        _bounded_depth(value)
        if isinstance(value, dict) and type(value.get("schema_version")) is not int:
            raise ValueError("schema version must be an integer")
        return value

    @model_validator(mode="after")
    def workspace_bounds(self):
        _unique_paths(self.workspace)
        if sum(entry.size for entry in self.workspace) > MAX_WORKSPACE_BYTES:
            raise ValueError("workspace exceeds 64 MiB")
        manifest = self.context.workspace_manifest
        if manifest and manifest != [WorkspaceEntry(path=entry.path, sha256=entry.sha256, size=entry.size)
                                     for entry in self.workspace]:
            raise ValueError("boundary content differs from context workspace manifest")
        return self


class BoundaryAck(RuntimeRecord):
    schema_version: Literal[1]
    boundary_id: UUID
    checkpoint_id: UUID
    checkpoint_revision: Positive
    manifest: CheckpointManifest

    @model_validator(mode="after")
    def matching_revision(self):
        if self.manifest.schema_version != 1 or self.checkpoint_revision != self.manifest.revision:
            raise ValueError("checkpoint acknowledgement revision mismatch")
        return self
