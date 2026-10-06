"""Validated shared API and persistence contracts; run as a module to publish TS/JSON."""

import json
import re
import unicodedata
from hashlib import sha256
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


RUNTIME_COMMIT = "bd0affe5e5f723579df8902852f5d0c47795f355"


class RuntimePins(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    runtime_commit: str = RUNTIME_COMMIT
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    skills_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


ShortText = Annotated[str, Field(min_length=1, max_length=200)]


def _has_control(value: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in value)


def normalize_doi(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    raw = str(value)
    if _has_control(raw):
        return None
    doi = raw.strip().lower()
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi)
    return doi.strip() or None


def canonical_peer_parameters_bytes(parameters: dict[str, Any]) -> bytes:
    """Canonical UTF-8 JSON bytes for the immutable SDK parameter object."""
    if not isinstance(parameters, dict) or any(not isinstance(key, str) for key in parameters):
        raise ValueError("approved peer parameters must be a JSON object")
    try:
        return json.dumps(parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("approved peer parameters must contain only finite JSON values") from exc


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


class PeerDataRef(Contract):
    """One immutable, project-scoped input captured in a run snapshot."""

    kind: Literal["file", "finding"]
    record_id: UUID
    version_digest: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class PeerReleaseSpec(Contract):
    """Narrow owner-approved release and bounded A2A request parameters."""

    release_id: UUID
    peer_id: UUID
    endpoint_fingerprint: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    purpose: str = Field(min_length=1, max_length=1000)
    input_snapshot_digest: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    data_refs: list[PeerDataRef] = Field(default_factory=list, max_length=100)
    approved_parameters: dict[str, Any]
    parameters_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    message_id: str = Field(min_length=1, max_length=200)
    method: Literal["SendMessage"] = "SendMessage"
    allow_get_task: bool = False
    request_bytes_limit: int = Field(ge=1, le=1_048_576)
    timeout_ms: int = Field(ge=1, le=30_000)
    reserved_tokens: int = Field(ge=0, le=1_000_000)
    reconciliation_limit: int = Field(ge=1, le=10)

    @model_validator(mode="after")
    def validate_release(self):
        if not self.purpose.strip() or not self.message_id.strip():
            raise ValueError("peer release purpose and message_id must not be blank")
        identities = [(ref.kind, ref.record_id) for ref in self.data_refs]
        if len(identities) != len(set(identities)):
            raise ValueError("peer release data references must be unique")
        body = canonical_peer_parameters_bytes(self.approved_parameters)
        if len(body) > self.request_bytes_limit:
            raise ValueError("approved peer parameters exceed request_bytes_limit")
        if sha256(body).hexdigest() != self.parameters_sha256.lower():
            raise ValueError("approved peer parameters sha256 does not match parameters_sha256")
        return self


class ScientificBinding(Contract):
    """Owner-approved instructions and bounded local computation identity."""
    catalog_commit: Literal["154988403bb5a18e9d3c0ce4e6d5e2e4b184a298"]
    registry_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    capability_ids: list[Annotated[StrictStr, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]] = Field(min_length=1, max_length=177)
    instruction_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_id: str = Field(pattern=r"^prof\.[a-z0-9-]+@py[0-9.]+$", max_length=100)
    profile_version: Literal["1"] = "1"
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    tool_version: Literal["1"] = "1"
    input_snapshot_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    parameters: dict[str, Any] = Field(default_factory=dict)
    max_result_bytes: Annotated[StrictInt, Field(ge=1, le=1048576)]
    timeout_ms: Annotated[StrictInt, Field(ge=1, le=30000)]
    memory_limit_bytes: Annotated[StrictInt, Field(ge=1, le=1073741824)]
    workspace_limit_bytes: Annotated[StrictInt, Field(ge=1, le=67108864)]

    @model_validator(mode="after")
    def bounded_parameters(self):
        if len(set(self.capability_ids)) != len(self.capability_ids):
            raise ValueError("scientific capabilities must be unique")
        parameters = canonical_peer_parameters_bytes(self.parameters)
        if len(parameters) > 65536:
            raise ValueError("scientific parameters exceed 64 KiB")
        if self.capability_ids == ["get-available-resources"] and self.parameters:
            raise ValueError("resource measurement accepts no parameters")
        return self


class CrossrefQueryV1(Contract):
    source_id: Literal["crossref"]
    version: Literal[1]
    access_mode: Literal["public_read"]
    query: StrictStr | None
    doi: StrictStr | None
    limit: Annotated[StrictInt, Field(ge=1, le=20)]

    @model_validator(mode="after")
    def exactly_one_bounded_request(self):
        if (self.query is None) == (self.doi is None):
            raise ValueError("exactly one Crossref query or DOI is required")
        if self.query is not None:
            if not self.query.strip() or len(self.query) > 512 or _has_control(self.query):
                raise ValueError("Crossref query is empty, oversized, or contains controls")
        else:
            normalized = normalize_doi(self.doi)
            if (normalized is None or len(normalized) > 255 or _has_control(normalized)
                    or not re.fullmatch(r"10\.[0-9]{4,9}/[^\s<>\"']+", normalized)):
                raise ValueError("invalid Crossref DOI")
            object.__setattr__(self, "doi", normalized)
        return self


_APPROVED_ID = Annotated[StrictStr, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]


class CsvDescribeGrantV1(Contract):
    recipe_id: Literal["csv.describe.v1"]
    recipe_version: Literal["1"]
    recipe_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_id: Literal["prof.csv-stdlib@py3.14.7"]
    profile_version: Literal["1"]
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    input_ref: ObjectRef
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    numeric_columns: list[Annotated[StrictStr, Field(min_length=1, max_length=128)]] = Field(min_length=1, max_length=8)
    max_input_bytes: Literal[1_048_576] = 1_048_576
    max_output_bytes: Literal[262_144] = 262_144
    timeout_ms: Literal[30_000] = 30_000
    memory_limit_bytes: Literal[1_073_741_824] = 1_073_741_824
    workspace_limit_bytes: Literal[67_108_864] = 67_108_864

    @model_validator(mode="after")
    def input_and_columns_are_bound(self):
        if self.input_sha256 != self.input_ref.sha256.lower() or self.input_ref.size > self.max_input_bytes:
            raise ValueError("CSV grant input identity or size is invalid")
        if (len(set(self.numeric_columns)) != len(self.numeric_columns)
                or any(not name.strip() or _has_control(name) for name in self.numeric_columns)):
            raise ValueError("CSV grant numeric columns must be unique bounded names")
        return self


class ComputeProfilePin(Contract):
    profile_id: Literal["prof.csv-stdlib@py3.14.7"]
    version: Literal["1"]
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class ScientificBindingV2(Contract):
    binding_version: Literal[2]
    catalog_commit: Literal["154988403bb5a18e9d3c0ce4e6d5e2e4b184a298"]
    registry_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    capability_ids: list[Annotated[StrictStr, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]] = Field(min_length=1, max_length=177)
    instruction_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    agent_runtime_pins: RuntimePins
    input_snapshot_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved_crossref_queries: dict[_APPROVED_ID, CrossrefQueryV1] = Field(default_factory=dict, max_length=20)
    required_compute_profiles: list[ComputeProfilePin] = Field(default_factory=list, max_length=8)
    csv_describe_grants: dict[_APPROVED_ID, CsvDescribeGrantV1] = Field(default_factory=dict, max_length=20)

    @model_validator(mode="after")
    def grants_match_selected_authority(self):
        if len(set(self.capability_ids)) != len(self.capability_ids):
            raise ValueError("scientific capabilities must be unique")
        if self.agent_runtime_pins.runtime_commit != RUNTIME_COMMIT:
            raise ValueError("unreviewed agent runtime commit")
        if self.approved_crossref_queries and "paper-lookup" not in self.capability_ids:
            raise ValueError("Crossref requests require the paper-lookup capability")
        if self.csv_describe_grants and "exploratory-data-analysis" not in self.capability_ids:
            raise ValueError("CSV grants require the exploratory-data-analysis capability")
        grants = {(grant.profile_id, grant.profile_version, grant.image_digest)
                  for grant in self.csv_describe_grants.values()}
        profiles = [(pin.profile_id, pin.version, pin.image_digest) for pin in self.required_compute_profiles]
        if len(profiles) != len(set(profiles)) or set(profiles) != grants:
            raise ValueError("compute profiles must exactly match approved CSV grants")
        return self


class PlanSpec(Contract):
    input_snapshot_digest: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    provider_id: UUID
    model: str = Field(min_length=1, max_length=200)
    stages: list[ShortText] = Field(max_length=50)
    allowed_ops: list[ShortText] = Field(max_length=100)
    data_recipients: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(max_length=100)
    packages: list[PackageSpec] = Field(max_length=200)
    peer_releases: list[PeerReleaseSpec] = Field(default_factory=list, max_length=20, exclude_if=lambda value: not value)
    scientific: ScientificBinding | ScientificBindingV2 | None = Field(default=None, exclude_if=lambda value: value is None)
    token_limit: int = Field(ge=0)
    elapsed_limit_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def bound_plan_size(self):
        if self.scientific is not None and self.scientific.input_snapshot_digest != self.input_snapshot_digest:
            raise ValueError("scientific input differs from the approved snapshot")
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
    kind: Literal["llm", "search", "package", "peer", "compute"]
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


class ResearchRequirementView(Contract):
    id: ShortText
    label: ShortText
    purpose: Annotated[str, Field(min_length=1, max_length=1000)]
    state: Literal["ready", "missing", "preparing", "blocked", "failed"]
    action: Literal["none", "configure_connection", "prepare_environment", "request_approval", "provide_hardware"]
    reason: ShortText | None = None


class ResearchProfileView(Contract):
    profile_id: ShortText
    version: ShortText
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    label: ShortText
    purpose: Annotated[str, Field(min_length=1, max_length=1000)]
    state: Literal["ready", "missing", "preparing", "blocked", "failed"]
    memory_limit_bytes: Annotated[StrictInt, Field(ge=1)]
    workspace_limit_bytes: Annotated[StrictInt, Field(ge=1)]
    reason: ShortText | None = None


class PreparationSubmit(Contract):
    profile_id: ShortText
    version: ShortText
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id: UUID


class PreparationJobView(Contract):
    id: UUID
    project_id: UUID
    profile_id: ShortText
    version: ShortText
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: Literal["queued", "building", "checking", "ready", "blocked", "failed", "unknown"]
    stage: Literal["queued", "context", "image", "compatibility", "security", "license", "isolation", "complete", "owner_decision"]
    error_code: ShortText | None = None
    evidence_verified: bool = False

    @model_validator(mode="after")
    def readiness_has_evidence(self):
        if self.state == "ready" and (not self.evidence_verified or self.stage != "complete"):
            raise ValueError("prepared environment lacks accepted evidence")
        return self


class ResearchSetupView(Contract):
    project_id: UUID
    requirements: list[ResearchRequirementView] = Field(max_length=200)
    profiles: list[ResearchProfileView] = Field(max_length=100)
    connections: list[ConnectionView] = Field(max_length=100)
    preparations: list[PreparationJobView] = Field(max_length=100)


class RunReadinessView(Contract):
    run_id: UUID
    revision: Annotated[StrictInt, Field(ge=1)]
    plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    binding_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    state: Literal["ready", "missing", "preparing", "blocked", "failed"]
    requirements: list[ResearchRequirementView] = Field(max_length=200)


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


class PendingDecisionView(Contract):
    """An owner-actionable decision reconstructed from current durable state."""
    decision_id: UUID
    reason: Literal["budget_exhausted", "unknown_outcome", "data_scope", "plan_change"]
    required_tokens: int | None = Field(default=None, ge=0)
    required_elapsed_ms: int | None = Field(default=None, ge=0)
    operation_reserved_tokens: int | None = Field(default=None, ge=0)


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
    choice: Literal["verified_result", "retry", "stop", "extend", "confirm_usage"]
    result: ObjectRef | None = None
    add_tokens: int | None = Field(default=None, ge=0)
    add_elapsed_ms: int | None = Field(default=None, ge=0)
    usage_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_choice_fields(self):
        extend = (self.add_tokens, self.add_elapsed_ms)
        if (self.choice == "confirm_usage") != (self.usage_tokens is not None):
            raise ValueError("usage_tokens is required for, and only valid with, confirm_usage")
        if (self.choice == "verified_result") != (self.result is not None):
            raise ValueError("result is required for, and only valid with, verified_result")
        if self.choice == "extend" and None in extend:
            raise ValueError("extend requires add_tokens and add_elapsed_ms")
        if self.choice != "extend" and extend != (None, None):
            raise ValueError("additions are only valid with extend")
        return self


MODELS = (RuntimePins, ScientificBinding, ScientificBindingV2, CrossrefQueryV1, CsvDescribeGrantV1, ComputeProfilePin, ResearchRequirementView, ResearchProfileView, PreparationSubmit, PreparationJobView, ResearchSetupView, RunReadinessView, Principal, APIError, ObjectRef, PackageSpec, PeerDataRef, PeerReleaseSpec, PlanSpec, ArtifactView, PlanView, RunView, PendingDecisionView, OperationRequest, OperationResult, CheckpointManifest, ProjectView, SessionView, FileView, FindingView, CitationView, ConnectionView, MessageView, DecisionSubmit, PlanReadyPayload, RunStatePayload, StageStartedPayload, StageCompletedPayload, ArtifactReadyPayload, DecisionRequiredPayload, UsageUpdatedPayload, RunEvent)


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
        additional = schema.get("additionalProperties")
        patterns = schema.get("patternProperties")
        if isinstance(patterns, dict) and patterns:
            value_types = sorted({_ts_type(value) for value in patterns.values()})
            value_type = " | ".join(value_types)
        else:
            value_type = _ts_type(additional) if isinstance(additional, dict) else "unknown"
        return f"Record<string, {value_type}>"
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
