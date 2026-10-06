"""Compose immutable instruction authority with current host preparation evidence."""
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

from scientist import profile_preparation, supervisor
from scientist.auth import DomainError
from scientist.contracts import Principal, ScientificBinding

ROOT = Path(__file__).resolve().parents[3]
_bundle_root = ROOT


def configure_bundle(root: Path | None) -> None:
    """Trusted host-only source location; browser/agent requests cannot set paths."""
    global _bundle_root
    _bundle_root = Path(root) if root is not None else ROOT


def current_image_digest() -> str:
    if supervisor._config is None:
        raise DomainError('scientific_environment_unavailable', 409)
    return supervisor._config.image_digest


def validate_instruction(binding: ScientificBinding, expected_image_digest: str):
    from scientist.instruction_loader import load_instruction_pins, validate_scientific_binding
    registry = ROOT / 'docs/skills/capability-registry.json'
    if not registry.is_file():
        registry = ROOT / 'runtime/capability-registry.json'
    try:
        return validate_scientific_binding(binding, registry_path=registry,
            bundle_root=_bundle_root,
            pinned_hashes=load_instruction_pins(ROOT / 'runtime/skills-manifest.json'),
            expected_image_digest=expected_image_digest)
    except (OSError, ValueError, TypeError) as exc:
        raise DomainError('scientific_binding_unavailable', 409) from exc


def validate_plan_binding(db, owner: Principal, project_id: UUID, binding: ScientificBinding,
                          *, require_ready: bool = True, expected_image_digest: str | None = None) -> None:
    validate_instruction(binding, expected_image_digest or current_image_digest())
    profile_preparation.validate_binding_for_run(db, project_id, binding, owner=owner, require_ready=require_ready)


def validate_runtime_binding(db, run_id: UUID, binding: ScientificBinding, *, expected_image_digest: str) -> None:
    # Authority comes from the current approved database revision, never the worker-supplied owner.
    row = db.execute(text('''SELECT r.project_id,a.owner_identity FROM runs r JOIN approvals a
        ON a.run_id=r.id AND a.revision=r.revision AND a.plan_digest=r.plan_digest
        WHERE r.id=:run'''), {'run': run_id}).one_or_none()
    if row is None:
        raise DomainError('forbidden', 403)
    validate_plan_binding(db, Principal(identity=row.owner_identity, kind='owner'), row.project_id,
                          binding, expected_image_digest=expected_image_digest)


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
