"""Validated shared API and persistence contracts; run as a module to publish TS/JSON."""

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


ShortText = Annotated[str, Field(min_length=1, max_length=200)]


class Principal(Contract):
    identity: UUID
    kind: Literal["owner", "external", "worker"]


class APIError(Contract):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=2000)
    request_id: str = Field(min_length=1, max_length=200)


class ObjectRef(Contract):
    project_id: UUID
    key: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    size: int = Field(ge=0)
    content_type: str = Field(min_length=1, max_length=255)


class PackageSpec(Contract):
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class PlanSpec(Contract):
    input_snapshot_digest: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    provider_id: UUID
    model: str = Field(min_length=1, max_length=200)
    stages: list[ShortText] = Field(max_length=50)
    allowed_ops: list[ShortText] = Field(max_length=100)
    data_recipients: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(max_length=100)
    packages: list[PackageSpec] = Field(max_length=200)
    token_limit: int = Field(ge=0)
    elapsed_limit_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def bound_plan_size(self):
        if len(self.model_dump_json().encode()) > 256 * 1024:
            raise ValueError("plan exceeds 262144 bytes")
        return self


class ArtifactView(Contract):
    artifact_id: UUID
    project_id: UUID
    run_id: UUID
    title: str = Field(min_length=1, max_length=300)
    kind: Literal["report", "table", "plot", "file"]
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    size: int = Field(ge=0)
    content_type: str = Field(min_length=1, max_length=255)
    partial: bool


class PlanView(Contract):
    run_id: UUID
    revision: int = Field(ge=1)
    plan_digest: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    plan: PlanSpec


class RunView(Contract):
    run_id: UUID
    project_id: UUID
    session_id: UUID
    revision: int = Field(ge=1)
    state: Literal["planning", "awaiting_approval", "queued", "running", "waiting_input", "recovering", "stopping", "completed", "failed", "canceled", "rejected"]
    stage: str | None = Field(default=None, max_length=200)
    waiting_reason: str | None = Field(default=None, max_length=100)
    error_code: str | None = Field(default=None, max_length=100)
    plan_digest: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    latest_cursor: int = Field(ge=0)
    usage_tokens: int = Field(ge=0)
    reserved_tokens: int = Field(ge=0)
    planning_tokens: int = Field(ge=0)
    token_limit: int = Field(ge=0)
    artifacts: list[ArtifactView] = Field(max_length=1000)
    retry_of: UUID | None = None


class OperationRequest(Contract):
    run_id: UUID
    generation: int = Field(ge=0)
    operation_id: str = Field(min_length=1, max_length=200)
    kind: Literal["llm", "search", "package", "peer"]
    payload: dict[str, Any]
    reserve_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def bound_payload(self):
        if len(json.dumps(self.payload, ensure_ascii=False, separators=(",", ":")).encode()) > 256 * 1024:
            raise ValueError("payload exceeds 262144 bytes")
        return self


class OperationResult(Contract):
    operation_id: str = Field(min_length=1, max_length=200)
    state: Literal["committed", "unknown", "denied"]
    result: ObjectRef | None
    usage_tokens: int | None = Field(default=None, ge=0)


class CheckpointManifest(Contract):
    schema_version: int = Field(ge=1)
    run_id: UUID
    revision: int = Field(ge=1)
    plan_digest: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    runtime_commit: str = Field(min_length=1, max_length=100)
    image_digest: str = Field(pattern=r"^sha256:[a-fA-F0-9]{64}$")
    skills_digest: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    context: ObjectRef
    workspace: list[ObjectRef] = Field(max_length=10000)
    environment_digest: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    operation_ids: list[ShortText] = Field(max_length=10000)


class ProjectView(Contract):
    id: UUID
    name: str = Field(min_length=1, max_length=200)
    revision: int = Field(ge=1)
    instructions: str = Field(max_length=100000)


class SessionView(Contract):
    id: UUID
    project_id: UUID
    title: str = Field(min_length=1, max_length=200)


class FileView(Contract):
    id: UUID
    project_id: UUID
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0)
    content_type: str = Field(min_length=1, max_length=255)
    state: Literal["uploading", "preparing", "ready", "failed"]
    error_code: str | None = Field(default=None, max_length=100)


class FindingView(Contract):
    id: UUID
    project_id: UUID
    session_id: UUID
    artifact_id: UUID | None = None
    text: str = Field(min_length=1, max_length=100000)
    citation_ids: list[UUID] = Field(max_length=1000)


class CitationView(Contract):
    id: UUID
    title: str = Field(min_length=1, max_length=1000)
    authors: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(max_length=200)
    year: int | None = Field(default=None, ge=1000, le=9999)
    identifier: str | None = Field(default=None, max_length=500)
    original_url: str | None = Field(default=None, max_length=2048)
    access: Literal["full_text", "abstract", "metadata", "unavailable"] | None = None
    verification: Literal["verified", "unverified", "contradictory"] | None = None


class ConnectionView(Contract):
    id: UUID
    label: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    state: Literal["unconfigured", "checking", "ready", "invalid_credentials", "unavailable_provider", "unavailable_model"]
    has_secret: bool


class PlanReadyPayload(Contract):
    plan_digest: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class RunStatePayload(Contract):
    state: RunView.model_fields["state"].annotation


class StageStartedPayload(Contract):
    stage: str = Field(min_length=1, max_length=200)


class StageCompletedPayload(StageStartedPayload):
    outcome: Literal["completed", "partial", "failed"]


class ArtifactReadyPayload(Contract):
    artifact: ArtifactView


class DecisionRequiredPayload(Contract):
    decision_id: UUID
    reason: Literal["budget_exhausted", "unknown_outcome", "data_scope", "plan_change"]
    # Limit increase that admits the refused request (tokens exact; elapsed a lower bound while the
    # active interval is open); None when not a budget wait.
    required_tokens: int | None = Field(default=None, ge=0)
    required_elapsed_ms: int | None = Field(default=None, ge=0)


class UsageUpdatedPayload(Contract):
    usage_tokens: int = Field(ge=0)
    reserved_tokens: int = Field(ge=0)
    token_limit: int = Field(ge=0)


EventPayload = Union[PlanReadyPayload, RunStatePayload, StageStartedPayload, StageCompletedPayload, ArtifactReadyPayload, DecisionRequiredPayload, UsageUpdatedPayload]
EVENT_PAYLOADS = {
    "plan.ready": PlanReadyPayload,
    "run.state": RunStatePayload,
    "stage.started": StageStartedPayload,
    "stage.completed": StageCompletedPayload,
    "artifact.ready": ArtifactReadyPayload,
    "decision.required": DecisionRequiredPayload,
    "usage.updated": UsageUpdatedPayload,
}


class RunEvent(Contract):
    schema_version: Literal[1]
    run_id: UUID
    sequence: int = Field(ge=1)
    revision: int = Field(ge=1)
    occurred_at: datetime
    kind: Literal["plan.ready", "run.state", "stage.started", "stage.completed", "artifact.ready", "decision.required", "usage.updated"]
    payload: EventPayload

    @model_validator(mode="after")
    def validate_payload_kind(self):
        if not isinstance(self.payload, EVENT_PAYLOADS[self.kind]):
            raise ValueError("payload does not match event kind")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None or self.occurred_at.utcoffset().total_seconds() != 0:
            raise ValueError("occurred_at must be UTC")
        return self


class MessageView(Contract):
    id: UUID
    sequence: int = Field(ge=1)
    role: str = Field(min_length=1, max_length=24)
    content: str
    created_at: datetime
    run_id: UUID | None = None


class DecisionSubmit(Contract):
    decision_id: UUID
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    choice: Literal["verified_result", "retry", "stop", "extend"]
    result: ObjectRef | None = None
    add_tokens: int | None = Field(default=None, ge=0)
    add_elapsed_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_choice_fields(self):
        extend = (self.add_tokens, self.add_elapsed_ms)
        if (self.choice == "verified_result") != (self.result is not None):
            raise ValueError("result is required for, and only valid with, verified_result")
        if self.choice == "extend" and None in extend:
            raise ValueError("extend requires add_tokens and add_elapsed_ms")
        if self.choice != "extend" and extend != (None, None):
            raise ValueError("additions are only valid with extend")
        return self


MODELS = (Principal, APIError, ObjectRef, PackageSpec, PlanSpec, ArtifactView, PlanView, RunView, OperationRequest, OperationResult, CheckpointManifest, ProjectView, SessionView, FileView, FindingView, CitationView, ConnectionView, MessageView, DecisionSubmit, PlanReadyPayload, RunStatePayload, StageStartedPayload, StageCompletedPayload, ArtifactReadyPayload, DecisionRequiredPayload, UsageUpdatedPayload, RunEvent)


def _ts_type(schema: dict[str, Any]) -> str:
    if "const" in schema:
        return json.dumps(schema["const"])
    if schema.get("type") == "null":
        return "null"
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[1]
    if "anyOf" in schema:
        return " | ".join(sorted({_ts_type(item) for item in schema["anyOf"]}))
    if "enum" in schema:
        return " | ".join(json.dumps(item) for item in schema["enum"])
    if schema.get("type") == "array":
        return f"{_ts_type(schema['items'])}[]"
    if schema.get("type") == "object":
        return "Record<string, unknown>"
    return {"string": "string", "integer": "number", "number": "number", "boolean": "boolean"}.get(schema.get("type"), "unknown")


def generate(output_dir: Path | None = None) -> None:
    output_dir = output_dir or Path(__file__).resolve().parents[3] / "contracts"
    output_dir.mkdir(parents=True, exist_ok=True)
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **RunEvent.model_json_schema(mode="serialization"),
    }
    base_properties = schema["properties"]
    required = schema["required"]
    schema["oneOf"] = []
    for kind, payload_model in EVENT_PAYLOADS.items():
        variant = {
            "type": "object",
            "title": f"{payload_model.__name__}Event",
            "additionalProperties": False,
            "properties": {
                **base_properties,
                "kind": {"const": kind, "type": "string"},
                "payload": {"$ref": f"#/$defs/{payload_model.__name__}"},
            },
            "required": required,
        }
        schema["oneOf"].append(variant)
    schema.pop("properties")
    schema.pop("required")
    schema.pop("additionalProperties", None)
    (output_dir / "run-event.schema.json").write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    lines = ["// Generated by python -m scientist.contracts; do not edit.", ""]
    for model in MODELS:
        if model is RunEvent:
            continue
        model_schema = model.model_json_schema(mode="serialization")
        required = set(model_schema.get("required", []))
        lines.append(f"export interface {model.__name__} {{")
        for name, prop in model_schema["properties"].items():
            optional = "" if name in required else "?"
            lines.append(f"  {name}{optional}: {_ts_type(prop)};")
        lines.extend(["}", ""])
    common = {key: value for key, value in RunEvent.model_json_schema(mode="serialization")["properties"].items() if key not in {"kind", "payload"}}
    variants = []
    for kind, payload_model in EVENT_PAYLOADS.items():
        fields = [f"{name}: {_ts_type(prop)}" for name, prop in common.items()]
        fields.extend([f"kind: {json.dumps(kind)}", f"payload: {payload_model.__name__}"])
        variants.append("{ " + "; ".join(fields) + " }")
    lines.extend([f"export type RunEvent = {' | '.join(variants)};", ""])
    (output_dir / "api-types.ts").write_text("\n".join(lines))


if __name__ == "__main__":
    generate()
