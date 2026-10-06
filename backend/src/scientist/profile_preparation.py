"""Owner requests record preparation; only the trusted host can attest readiness."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
import re
from pathlib import Path, PurePosixPath
from typing import Annotated, Callable, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy import text
from sqlalchemy.orm import Session

from scientist import domain, secrets as secretstore
from scientist.auth import DomainError
from scientist.contracts import (PreparationJobView, PreparationSubmit, Principal,
                                ResearchProfileView, ResearchRequirementView,
                                ResearchSetupView, RunReadinessView, ScientificBinding, ScientificBindingV2)

ROOT = Path(__file__).resolve().parents[3]
PROFILE_ID = "prof.worker-base@py3.14.7"
_SHA = r"^[a-f0-9]{64}$"
_IMAGE_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
Digest = Annotated[str, Field(pattern=_SHA)]
Positive = Annotated[StrictInt, Field(ge=1)]
_builder: Callable[["Profile", UUID], dict] | None = None
_evidence_key: bytes | None = None
_expected_image_digest: str | None = None


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Profile(_Record):
    schema_version: Literal[1]
    profile_id: Literal["prof.worker-base@py3.14.7"]
    version: Literal["1"]
    label: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=1000)
    runtime_commit: Literal["bd0affe5e5f723579df8902852f5d0c47795f355"]
    catalog_commit: Literal["154988403bb5a18e9d3c0ce4e6d5e2e4b184a298"]
    python_version: Literal["3.14.7"]
    architecture: Literal["linux/arm64"]
    skills_manifest_sha256: Digest
    lock_sha256: dict[str, Digest]
    compatibility_checks: list[str] = Field(min_length=1, max_length=20)
    memory_limit_bytes: Annotated[StrictInt, Field(ge=1, le=1073741824)]
    workspace_limit_bytes: Annotated[StrictInt, Field(ge=1, le=67108864)]
    timeout_ms: Annotated[StrictInt, Field(ge=1, le=30000)]
    max_result_bytes: Annotated[StrictInt, Field(ge=1, le=1048576)]
    manifest_sha256: Digest


class ScanProof(_Record):
    report_sha256: Digest
    scanned_at: datetime
    database_updated_at: datetime
    database_next_update: datetime
    high: Annotated[StrictInt, Field(ge=0, le=0)]
    critical: Annotated[StrictInt, Field(ge=0, le=0)]
    os_packages: Positive
    python_packages: Positive


class ContainmentProof(_Record):
    uid: Annotated[StrictInt, Field(ge=65532, le=65532)]
    read_only: StrictBool
    network: Literal["none", "internal"]
    cap_drop_all: StrictBool
    memory_limit_bytes: Positive
    workspace_limit_bytes: Positive
    pids_limit: Annotated[StrictInt, Field(ge=1, le=128)]
    docker_socket_absent: StrictBool
    unrelated_host_mounts_absent: StrictBool
    direct_egress_denied: StrictBool
    unreviewed_code_disabled: StrictBool
    isolation_test_sha256: Digest


class BuildProof(_Record):
    """Trusted builder summaries must be backed by the named actual evidence hashes."""
    schema_version: Literal[1]
    job_id: UUID
    profile_id: str
    version: str
    manifest_sha256: Digest
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    runtime_commit: str
    catalog_commit: str
    python_version: str
    architecture: str
    skills_manifest_sha256: Digest
    lock_sha256: dict[str, Digest]
    built_at: datetime
    checked_at: datetime
    source_manifest_sha256: Digest
    build_network: Literal["none"]
    compatibility_checks: list[str] = Field(max_length=20)
    scan: ScanProof
    sbom_sha256: Digest
    sbom_packages: Positive
    license_approved: StrictBool
    containment: ContainmentProof


class BuildOutcomeUnknown(Exception):
    """The host cannot prove whether a launched preparation completed; never retry."""


class BuildFailure(Exception):
    """The trusted host has proved a terminal failed build, without exposing logs."""


def _bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def load_profiles(root: Path = ROOT) -> dict[str, Profile]:
    """Read the sole reviewed profile and locks; no installation or discovery."""
    try:
        path = root / "runtime/profiles/worker-base.json"
        if any(part.is_symlink() for part in (path, path.parent, root / "runtime")) or path.stat().st_size > 65536:
            raise ValueError("unsafe profile")
        raw = json.loads(path.read_bytes())
        if not isinstance(raw, dict) or "manifest_sha256" in raw:
            raise ValueError("invalid manifest")
        profile = Profile.model_validate({**raw, "manifest_sha256": sha256(_bytes(raw)).hexdigest()})
        skills = root / "runtime/skills-manifest.json"
        if skills.is_symlink() or not skills.is_file() or skills.stat().st_size > 16777216:
            raise ValueError("unsafe bundle manifest")
        if sha256(skills.read_bytes()).hexdigest() != profile.skills_manifest_sha256:
            raise ValueError("bundle manifest drift")
        if set(profile.lock_sha256) != {"runtime/requirements.lock"}:
            raise ValueError("unreviewed locks")
        for relative, expected in profile.lock_sha256.items():
            parts = PurePosixPath(relative).parts
            if not parts or any(part in {".", ".."} for part in parts):
                raise ValueError("invalid lock")
            target = root
            for part in parts:
                target /= part
                if target.is_symlink():
                    raise ValueError("unsafe lock")
            if not target.is_file() or target.stat().st_size > 1048576 or sha256(target.read_bytes()).hexdigest() != expected:
                raise ValueError("lock drift")
        return {profile.profile_id: profile}
    except (OSError, ValueError, TypeError) as exc:
        raise DomainError("profile_configuration_unavailable", 503) from exc


def configure_evidence_key(key: bytes | None) -> None:
    """Trusted host/controller composition; verification does not configure a builder."""
    global _evidence_key
    if key is not None and (not isinstance(key, bytes) or len(key) < 32):
        raise ValueError("preparation evidence key is invalid")
    _evidence_key = key


def configure_builder(
    callback: Callable[[Profile, UUID], dict] | None,
    *,
    expected_image_digest: str | None = None,
    evidence_key: bytes | None = None,
) -> None:
    """Trusted host only. Reuse a private host key; never accept it through owner APIs.

    The callback must enforce the profile's time/resource limits and verify actual
    build/scan/SBOM/license/isolation files before returning their proof summaries.
    No callback is configured by imports or by a setup/read/preparation request.
    """
    global _builder, _expected_image_digest
    if callback is not None and (not callable(callback) or evidence_key is None):
        raise ValueError("trusted builder requires evidence authentication")
    if expected_image_digest is not None and (
        not isinstance(expected_image_digest, str)
        or _IMAGE_DIGEST.fullmatch(expected_image_digest) is None
    ):
        raise ValueError("worker image digest invalid")
    if evidence_key is not None and (not isinstance(evidence_key, bytes) or len(evidence_key) < 32):
        raise ValueError("preparation evidence key is invalid")
    configure_evidence_key(evidence_key)
    _builder = callback
    _expected_image_digest = expected_image_digest


def _owner_project(db: Session, owner: Principal, project_id: UUID) -> None:
    if owner.kind != "owner":
        raise DomainError("forbidden", 403)
    domain.get_project(db, owner, project_id)


def _profile(profile_id: str, version: str, digest: str | None = None) -> Profile:
    profile = load_profiles().get(profile_id)
    if profile is None or profile.version != version or (digest is not None and profile.manifest_sha256 != digest):
        raise DomainError("profile_not_configured", 409)
    return profile


def _validate_proof(raw: dict, profile: Profile, job_id: UUID) -> BuildProof:
    proof = BuildProof.model_validate(raw)
    if len(_bytes(proof.model_dump(mode="json"))) > 65536:
        raise ValueError("oversized evidence")
    if proof.job_id != job_id or any(getattr(proof, name) != getattr(profile, name) for name in (
        "profile_id", "version", "manifest_sha256", "runtime_commit", "catalog_commit",
        "python_version", "architecture", "skills_manifest_sha256", "lock_sha256", "compatibility_checks")):
        raise ValueError("evidence identity differs")
    now = datetime.now(timezone.utc)
    times = [proof.built_at, proof.checked_at, proof.scan.scanned_at,
             proof.scan.database_updated_at, proof.scan.database_next_update]
    if any(value.utcoffset() != timedelta(0) for value in times):
        raise ValueError("evidence dates must be UTC")
    if not (proof.built_at <= proof.checked_at
            and now-timedelta(hours=24) <= proof.checked_at <= now+timedelta(minutes=5)
            and proof.built_at <= proof.scan.scanned_at <= proof.checked_at
            and now-timedelta(hours=24) <= proof.scan.scanned_at
            and proof.scan.database_updated_at <= proof.scan.scanned_at < proof.scan.database_next_update
            and proof.scan.database_next_update > now):
        raise ValueError("evidence is stale")
    containment = proof.containment
    if (not proof.license_approved or not all(getattr(containment, name) for name in (
            "read_only", "cap_drop_all", "docker_socket_absent", "unrelated_host_mounts_absent",
            "direct_egress_denied", "unreviewed_code_disabled"))
            or containment.memory_limit_bytes > profile.memory_limit_bytes
            or containment.workspace_limit_bytes > profile.workspace_limit_bytes):
        raise ValueError("unaccepted containment or license")
    return proof


def _signature(proof: dict) -> str:
    if _evidence_key is None:
        raise ValueError("evidence authentication unavailable")
    return hmac.new(_evidence_key, b"scientist.profile-preparation.v1\0" + _bytes(proof), "sha256").hexdigest()


def _verified(row, *, expected_image_digest: str | None = None) -> BuildProof | None:
    try:
        expected = _expected_image_digest if expected_image_digest is None else expected_image_digest
        if not isinstance(expected, str) or _IMAGE_DIGEST.fullmatch(expected) is None:
            return None
        envelope = row.evidence
        if not isinstance(envelope, dict) or set(envelope) != {"proof", "signature"}:
            return None
        if not hmac.compare_digest(_signature(envelope["proof"]), envelope["signature"]):
            return None
        proof = _validate_proof(
            envelope["proof"], _profile(row.profile_id, row.version, row.manifest_sha256.strip()), row.id
        )
        if proof.image_digest != expected:
            return None
        return proof
    except (ValueError, TypeError, DomainError):
        return None


def _job_view(row, project_id: UUID) -> PreparationJobView:
    valid = row.state == "ready" and _verified(row) is not None
    invalid = row.state == "ready" and not valid
    return PreparationJobView(id=row.id, project_id=project_id, profile_id=row.profile_id, version=row.version,
                              manifest_sha256=row.manifest_sha256.strip(), state="blocked" if invalid else row.state,
                              stage="owner_decision" if invalid else row.stage,
                              error_code="build_evidence_unavailable" if invalid else row.error_code,
                              evidence_verified=valid)


def get_job(db: Session, owner: Principal, project_id: UUID, job_id: UUID) -> PreparationJobView:
    _owner_project(db, owner, project_id)
    row = db.execute(text("""SELECT p.* FROM profile_preparations p WHERE p.id=:job AND p.owner_identity=:owner
        AND EXISTS (SELECT 1 FROM preparation_requests r WHERE r.job_id=p.id
                    AND r.owner_identity=:owner AND r.project_id=:project)"""),
        {"job": job_id, "owner": owner.identity, "project": project_id}).one_or_none()
    if row is None:
        raise DomainError("not_found", 404)
    return _job_view(row, project_id)


def request_preparation(db: Session, owner: Principal, project_id: UUID, body: PreparationSubmit) -> PreparationJobView:
    _owner_project(db, owner, project_id)
    body = PreparationSubmit.model_validate(body.model_dump(mode="json"))
    payload = sha256(_bytes({"project_id": str(project_id), **body.model_dump(mode="json")})).hexdigest()
    # DB lock/constraint, rather than a process lock, covers refreshes and multiple hosts.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
               {"key": f"profile-request:{owner.identity}:{body.request_id}"})
    prior = db.execute(text("SELECT job_id,payload_sha256 FROM preparation_requests WHERE owner_identity=:owner AND request_id=:request"),
                       {"owner": owner.identity, "request": body.request_id}).one_or_none()
    if prior:
        if prior.payload_sha256.strip() != payload:
            raise DomainError("idempotency_conflict", 409)
        return get_job(db, owner, project_id, prior.job_id)
    profile = _profile(body.profile_id, body.version, body.manifest_sha256)
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
               {"key": f"profile-job:{owner.identity}:{profile.profile_id}:{profile.version}"})
    conflicting = db.execute(text("""SELECT 1 FROM profile_preparations WHERE owner_identity=:owner
        AND profile_id=:profile AND version=:version AND manifest_sha256<>:digest
        AND state IN ('queued','building','checking','unknown')"""),
        {"owner": owner.identity, "profile": profile.profile_id, "version": profile.version,
         "digest": profile.manifest_sha256}).first()
    if conflicting:
        raise DomainError("profile_preparation_conflict", 409)
    candidates = db.execute(text("""SELECT * FROM profile_preparations WHERE owner_identity=:owner
        AND profile_id=:profile AND version=:version AND manifest_sha256=:digest
        AND state IN ('queued','building','checking','unknown','ready') ORDER BY created_at DESC,id DESC"""),
        {"owner": owner.identity, "profile": profile.profile_id, "version": profile.version,
         "digest": profile.manifest_sha256}).all()
    selected = next((row for row in candidates if row.state != "ready" or _verified(row) is not None), None)
    job_id = selected.id if selected else uuid4()
    if selected is None:
        db.execute(text("""INSERT INTO profile_preparations(id,owner_identity,profile_id,version,manifest_sha256,state,stage)
            VALUES(:id,:owner,:profile,:version,:digest,'queued','queued')"""),
            {"id": job_id, "owner": owner.identity, "profile": profile.profile_id,
             "version": profile.version, "digest": profile.manifest_sha256})
    db.execute(text("""INSERT INTO preparation_requests(owner_identity,request_id,project_id,job_id,payload_sha256)
        VALUES(:owner,:request,:project,:job,:payload)"""),
        {"owner": owner.identity, "request": body.request_id, "project": project_id, "job": job_id, "payload": payload})
    return get_job(db, owner, project_id, job_id)


def _latest(db: Session, owner: Principal, profile: Profile):
    return db.execute(text("""SELECT * FROM profile_preparations WHERE owner_identity=:owner
        AND profile_id=:profile AND version=:version AND manifest_sha256=:digest
        ORDER BY created_at DESC,id DESC LIMIT 1"""),
        {"owner": owner.identity, "profile": profile.profile_id, "version": profile.version,
         "digest": profile.manifest_sha256}).one_or_none()


def _state(row, *, expected_image_digest: str | None = None) -> tuple[str, str | None]:
    if row is None:
        return "missing", "environment_not_prepared"
    if row.state == "ready":
        return ("ready", None) if _verified(row, expected_image_digest=expected_image_digest) is not None else ("blocked", "build_evidence_unavailable")
    if row.state in {"queued", "building", "checking"}:
        return "preparing", None
    return ("blocked" if row.state == "unknown" else row.state), row.error_code


def get_setup(db: Session, owner: Principal, project_id: UUID) -> ResearchSetupView:
    _owner_project(db, owner, project_id)
    profiles, requirements = [], []
    for profile in load_profiles().values():
        state, reason = _state(_latest(db, owner, profile))
        profiles.append(ResearchProfileView(profile_id=profile.profile_id, version=profile.version,
            manifest_sha256=profile.manifest_sha256, label=profile.label, purpose=profile.purpose,
            state=state, memory_limit_bytes=profile.memory_limit_bytes,
            workspace_limit_bytes=profile.workspace_limit_bytes, reason=reason))
        requirements.append(ResearchRequirementView(id=profile.profile_id, label=profile.label,
            purpose=profile.purpose, state=state, reason=reason,
            action="none" if state in {"ready", "preparing"} else
                   "request_approval" if state == "blocked" else "prepare_environment"))
    rows = db.execute(text("""SELECT p.* FROM profile_preparations p WHERE p.owner_identity=:owner
        AND EXISTS (SELECT 1 FROM preparation_requests r WHERE r.job_id=p.id AND r.project_id=:project
                    AND r.owner_identity=:owner) ORDER BY p.created_at DESC,p.id DESC LIMIT 100"""),
        {"owner": owner.identity, "project": project_id}).all()
    return ResearchSetupView(project_id=project_id, profiles=profiles, requirements=requirements,
        connections=secretstore.list_connections(db, owner)[:100], preparations=[_job_view(row, project_id) for row in rows])


def validate_binding_for_run(db: Session, project_id: UUID, binding: ScientificBinding | ScientificBindingV2, *,
                             owner: Principal, require_ready: bool = True) -> None:
    """Profile proof complements R1's pinned instruction validation; it never grants tools."""
    _owner_project(db, owner, project_id)
    if isinstance(binding, ScientificBindingV2):
        binding = ScientificBindingV2.model_validate(binding.model_dump(mode="json"))
        if not require_ready:
            return
        identities = [(PROFILE_ID, "1", binding.agent_runtime_pins.image_digest)]
        identities.extend((pin.profile_id, pin.version, pin.image_digest)
                          for pin in binding.required_compute_profiles)
        for profile_id, version, image_digest in identities:
            try:
                profile = _profile(profile_id, version)
            except DomainError as exc:
                raise DomainError("scientific_environment_not_ready", 409) from exc
            row = _latest(db, owner, profile)
            proof = (_verified(row, expected_image_digest=image_digest)
                     if row is not None and row.state == "ready" else None)
            if proof is None:
                raise DomainError("scientific_environment_not_ready", 409)
        return
    binding = ScientificBinding.model_validate(binding.model_dump(mode="json"))
    profile = _profile(binding.profile_id, binding.profile_version)
    if (binding.capability_ids != ["get-available-resources"] or binding.parameters or binding.tool_version != "1"
            or any(getattr(binding, name) > getattr(profile, name) for name in (
                "timeout_ms", "max_result_bytes", "memory_limit_bytes", "workspace_limit_bytes"))):
        raise DomainError("scientific_binding_unavailable", 409)
    if not require_ready:
        return
    row = _latest(db, owner, profile)
    proof = (_verified(row, expected_image_digest=binding.image_digest)
             if row is not None and row.state == "ready" else None)
    if proof is None or proof.image_digest != binding.image_digest:
        raise DomainError("scientific_environment_not_ready", 409)


def get_readiness(db: Session, owner: Principal, run_id: UUID) -> RunReadinessView:
    if owner.kind != "owner":
        raise DomainError("forbidden", 403)
    run, plan = domain.get_run(db, owner, run_id), domain.get_plan(db, owner, run_id)
    configured = any(connection.id == plan.plan.provider_id and connection.model == plan.plan.model
                     and connection.state == "ready" for connection in secretstore.list_connections(db, owner))
    requirements = [ResearchRequirementView(id="model_connection", label="Model connection configuration",
        purpose="Configure an approved model connection; saving a key does not verify a provider round-trip.",
        state="ready" if configured else "missing", action="none" if configured else "configure_connection",
        reason=None if configured else "connection_not_configured")]
    binding = plan.plan.scientific
    if binding is not None:
        if isinstance(binding, ScientificBindingV2):
            identities = [(PROFILE_ID, "1", binding.agent_runtime_pins.image_digest)]
            identities.extend((pin.profile_id, pin.version, pin.image_digest)
                              for pin in binding.required_compute_profiles)
        else:
            identities = [(binding.profile_id, binding.profile_version, binding.image_digest)]
        for profile_id, version, image_digest in identities:
            try:
                profile = _profile(profile_id, version)
            except DomainError:
                requirements.append(ResearchRequirementView(id=profile_id,
                    label="CSV analysis environment", purpose="Prepare the approved CSV analysis environment.",
                    state="blocked", reason="profile_not_configured", action="request_approval"))
                continue
            state, reason = _state(_latest(db, owner, profile), expected_image_digest=image_digest)
            if state == "ready" and not isinstance(binding, ScientificBindingV2):
                try:
                    validate_binding_for_run(db, run.project_id, binding, owner=owner)
                except DomainError:
                    state, reason = "blocked", "scientific_binding_unavailable"
            requirements.append(ResearchRequirementView(id=profile.profile_id, label=profile.label,
                purpose=profile.purpose, state=state, reason=reason,
                action="none" if state in {"ready", "preparing"} else
                       "request_approval" if state == "blocked" else "prepare_environment"))
    states = {requirement.state for requirement in requirements}
    state = next((status for status in ("blocked", "failed", "missing", "preparing") if status in states), "ready")
    return RunReadinessView(run_id=run_id, revision=plan.revision, plan_digest=plan.plan_digest,
        binding_sha256=sha256(_bytes(binding.model_dump(mode="json"))).hexdigest() if binding is not None else None,
        state=state, requirements=requirements)


def record_stage(db: Session, job_id: UUID, stage: str) -> None:
    """Trusted builder progress only; this cannot set ready or attach evidence."""
    stages = ("context", "image", "compatibility", "security", "license", "isolation")
    if stage not in stages:
        raise ValueError("unreviewed preparation stage")
    row = db.execute(text("SELECT state,stage FROM profile_preparations WHERE id=:id FOR UPDATE"),
                     {"id": job_id}).one_or_none()
    if row is None or row.state not in {"building", "checking"} or row.stage not in stages or stages.index(stage) < stages.index(row.stage):
        raise DomainError("preparation_stage_conflict", 409)
    db.execute(text("UPDATE profile_preparations SET state=:state,stage=:stage,updated_at=now() WHERE id=:id"),
               {"id": job_id, "stage": stage, "state": "building" if stage in {"context", "image"} else "checking"})
    db.commit()


def process_next_job(db: Session, *, owner_identity: UUID | None = None) -> UUID | None:
    """Trusted host work loop only. Commit a launch claim before calling its bounded builder.

    Started/unknown jobs are never reclaimed automatically. Host loss requires
    exact physical reconciliation and an owner decision, not a second launch.
    """
    # ponytail: one build at a time; use reviewed per-engine admission if throughput requires it.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('scientist.profile-builder'))"))
    row = db.execute(text("""SELECT * FROM profile_preparations WHERE state='queued'
        AND (CAST(:owner AS uuid) IS NULL OR owner_identity=:owner)
        AND NOT EXISTS (SELECT 1 FROM profile_preparations active
            WHERE active.state IN ('building','checking','unknown'))
        ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1"""), {"owner": owner_identity}).one_or_none()
    if row is None:
        return None
    db.execute(text("UPDATE profile_preparations SET state='building',stage='context',updated_at=now() WHERE id=:id"), {"id": row.id})
    db.commit()
    evidence, state, stage, error = None, "blocked", "owner_decision", "preparation_unavailable"
    if _builder is not None and _evidence_key is not None:
        try:
            profile = _profile(row.profile_id, row.version, row.manifest_sha256.strip())
            raw = _builder(profile, row.id)
        except BuildFailure:
            state, stage, error = "failed", "image", "preparation_failed"
        except Exception:
            state, stage, error = "unknown", "owner_decision", "preparation_outcome_unknown"
        else:
            try:
                proof = _validate_proof(raw, profile, row.id).model_dump(mode="json")
                evidence = {"proof": proof, "signature": _signature(proof)}
                state, stage, error = "ready", "complete", None
            except (ValueError, TypeError):
                state, stage, error = "failed", "security", "build_evidence_invalid"
    db.execute(text("""UPDATE profile_preparations SET state=:state,stage=:stage,error_code=:error,
        evidence=CAST(:evidence AS jsonb),updated_at=now() WHERE id=:id AND state IN ('building','checking')"""),
        {"id": row.id, "state": state, "stage": stage, "error": error,
         "evidence": json.dumps(evidence) if evidence is not None else None})
    db.commit()
    return row.id
