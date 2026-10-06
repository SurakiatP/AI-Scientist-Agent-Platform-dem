"""Compose immutable instruction authority with current host preparation evidence."""
import json
import re
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

from scientist import profile_preparation, supervisor
from scientist.auth import DomainError
from scientist.contracts import ComputeProfilePin, Principal, RuntimePins, ScientificBinding, ScientificBindingV2, RUNTIME_COMMIT

ROOT = Path(__file__).resolve().parents[3]
_bundle_root = ROOT
_compute_profile_pin: ComputeProfilePin | None = None
_compute_recipe_manifest: str | None = None


def configure_bundle(root: Path | None) -> None:
    """Trusted host-only source location; browser/agent requests cannot set paths."""
    global _bundle_root
    _bundle_root = Path(root) if root is not None else ROOT


def configure_compute_profile(image_digest: str | None, recipe_manifest_sha256: str | None) -> None:
    """Install host-configured compute pins; caller values never come from a plan."""
    global _compute_profile_pin, _compute_recipe_manifest
    if image_digest is None and recipe_manifest_sha256 is None:
        _compute_profile_pin = None
        _compute_recipe_manifest = None
        return
    try:
        _compute_profile_pin = ComputeProfilePin(
            profile_id="prof.csv-stdlib@py3.14.7",
            version="1",
            image_digest=image_digest,
        )
    except (TypeError, ValueError) as exc:
        raise DomainError("scientific_environment_unavailable", 409) from exc
    if not isinstance(recipe_manifest_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", recipe_manifest_sha256):
        _compute_profile_pin = None
        _compute_recipe_manifest = None
        raise DomainError("scientific_environment_unavailable", 409)
    _compute_recipe_manifest = recipe_manifest_sha256


def trusted_compute_profile() -> tuple[ComputeProfilePin, str]:
    if _compute_profile_pin is None or _compute_recipe_manifest is None:
        raise DomainError("scientific_environment_unavailable", 409)
    return _compute_profile_pin, _compute_recipe_manifest


def current_image_digest() -> str:
    if supervisor._config is None:
        raise DomainError('scientific_environment_unavailable', 409)
    return supervisor._config.image_digest


def _current_runtime_pins(trusted_runtime_pins: RuntimePins | None = None) -> RuntimePins:
    if trusted_runtime_pins is not None:
        if not isinstance(trusted_runtime_pins, RuntimePins):
            raise DomainError("scientific_environment_unavailable", 409)
        return trusted_runtime_pins
    if supervisor._config is None:
        raise DomainError("scientific_environment_unavailable", 409)
    config = supervisor._config
    try:
        return RuntimePins(
            runtime_commit=RUNTIME_COMMIT,
            image_digest=config.image_digest,
            skills_digest=config.skills_digest,
            environment_digest=config.environment_digest,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise DomainError("scientific_environment_unavailable", 409) from exc


def validate_instruction(
    binding: ScientificBinding | ScientificBindingV2,
    expected_image_digest: str,
    *,
    trusted_runtime_pins: RuntimePins | None = None,
):
    from scientist.instruction_loader import load_instruction_pins, validate_scientific_binding
    registry = ROOT / 'docs/skills/capability-registry.json'
    if not registry.is_file():
        registry = ROOT / 'runtime/capability-registry.json'
    try:
        return validate_scientific_binding(
            binding,
            registry_path=registry,
            bundle_root=_bundle_root,
            pinned_hashes=load_instruction_pins(ROOT / "runtime/skills-manifest.json"),
            expected_image_digest=expected_image_digest,
            expected_runtime_pins=(
                _current_runtime_pins(trusted_runtime_pins)
                if isinstance(binding, ScientificBindingV2)
                else None
            ),
        )
    except (OSError, ValueError, TypeError) as exc:
        raise DomainError('scientific_binding_unavailable', 409) from exc


def _validate_csv_snapshot_membership(
    db,
    project_id: UUID,
    binding: ScientificBinding | ScientificBindingV2,
    *,
    run_id: UUID | None = None,
) -> None:
    if not isinstance(binding, ScientificBindingV2) or not binding.csv_describe_grants:
        return
    query = """
        SELECT manifest
        FROM input_snapshots
        WHERE project_id = :project AND digest = :digest
    """
    params: dict[str, object] = {
        "project": project_id,
        "digest": binding.input_snapshot_digest.lower(),
    }
    if run_id is not None:
        query += " AND run_id = :run"
        params["run"] = run_id
    rows = db.execute(text(query), params).mappings().all()
    if not rows:
        raise DomainError("scientific_input_unavailable", 409)

    grants = tuple(binding.csv_describe_grants.values())
    for row in rows:
        manifest = row.get("manifest")
        if isinstance(manifest, str):
            try:
                manifest = json.loads(manifest)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
            continue
        files = manifest["files"]
        if all(_grant_matches_snapshot_file(db, project_id, grant, files) for grant in grants):
            return
    raise DomainError("scientific_input_unavailable", 409)


def _grant_matches_snapshot_file(db, project_id: UUID, grant, files: list[object]) -> bool:
    ref = grant.input_ref
    if (ref.project_id != project_id or ref.content_type != "application/octet-stream"
            or ref.key != f"{project_id}/{ref.sha256.lower()}"):
        return False
    captured = next((
        item for item in files
        if isinstance(item, dict)
        and item.get("object_key") == ref.key
        and isinstance(item.get("sha256"), str)
        and item["sha256"].strip().lower() == ref.sha256.lower()
        and item.get("size") == ref.size
        and item.get("content_type") == "text/csv"
        and ("state" not in item or item.get("state") == "ready")
    ), None)
    if captured is None:
        return False
    stored = db.execute(text("""
        SELECT project_id, sha256, size, content_type
        FROM stored_objects WHERE key = :key
    """), {"key": ref.key}).one_or_none()
    return bool(
        stored is not None
        and stored.project_id == project_id
        and stored.sha256.strip().lower() == ref.sha256.lower()
        and stored.size == ref.size
        and stored.content_type == "application/octet-stream"
    )


def validate_plan_binding(
    db,
    owner: Principal,
    project_id: UUID,
    binding: ScientificBinding | ScientificBindingV2,
    *,
    require_ready: bool = True,
    expected_image_digest: str | None = None,
    trusted_runtime_pins: RuntimePins | None = None,
    run_id: UUID | None = None,
) -> None:
    validate_instruction(
        binding,
        expected_image_digest or current_image_digest(),
        trusted_runtime_pins=trusted_runtime_pins,
    )
    _validate_csv_snapshot_membership(db, project_id, binding, run_id=run_id)
    profile_preparation.validate_binding_for_run(db, project_id, binding, owner=owner, require_ready=require_ready)


def validate_runtime_binding(
    db,
    run_id: UUID,
    binding: ScientificBinding | ScientificBindingV2,
    *,
    expected_image_digest: str,
    trusted_runtime_pins: RuntimePins | None = None,
) -> None:
    # Authority comes from the current approved database revision, never the worker-supplied owner.
    row = db.execute(text('''SELECT r.project_id,a.owner_identity FROM runs r JOIN approvals a
        ON a.run_id=r.id AND a.revision=r.revision AND a.plan_digest=r.plan_digest
        WHERE r.id=:run'''), {'run': run_id}).one_or_none()
    if row is None:
        raise DomainError('forbidden', 403)
    validate_plan_binding(
        db,
        Principal(identity=row.owner_identity, kind="owner"),
        row.project_id,
        binding,
        expected_image_digest=expected_image_digest,
        trusted_runtime_pins=trusted_runtime_pins,
        run_id=run_id,
    )


def resource_binding(input_snapshot_digest: str) -> ScientificBinding:
    """Draft identities from trusted immutable inputs; preparation grants no run approval."""
    from scientist.capability_registry import load_registry
    from scientist.instruction_loader import load_instruction_bundle, load_instruction_pins
    registry_path = ROOT / 'docs/skills/capability-registry.json'
    if not registry_path.is_file():
        registry_path = ROOT / 'runtime/capability-registry.json'
    try:
        registry = load_registry(registry_path)
        selection = registry.select(['get-available-resources'])
        instructions = load_instruction_bundle(selection, _bundle_root,
            load_instruction_pins(ROOT / 'runtime/skills-manifest.json'),
            token_counter=lambda text: len(text.encode('utf-8')), token_budget=65536)
        profile = profile_preparation.load_profiles()['prof.worker-base@py3.14.7']
        binding = ScientificBinding(catalog_commit=selection.catalog_commit,
            registry_sha256=selection.registry_sha256, capability_ids=list(selection.capability_ids),
            instruction_fingerprint=instructions.instruction_fingerprint, profile_id=profile.profile_id,
            profile_version=profile.version, image_digest=current_image_digest(),
            input_snapshot_digest=input_snapshot_digest, parameters={},
            timeout_ms=profile.timeout_ms, memory_limit_bytes=profile.memory_limit_bytes,
            workspace_limit_bytes=profile.workspace_limit_bytes, max_result_bytes=profile.max_result_bytes)
        validate_instruction(binding, current_image_digest())
        return binding
    except (OSError, ValueError, TypeError) as exc:
        raise DomainError('scientific_binding_unavailable', 409) from exc
