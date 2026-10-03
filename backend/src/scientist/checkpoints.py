"""Durable, immutable worker checkpoints and fail-closed workspace restoration."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from scientist import objects
from scientist.contracts import CheckpointManifest, ObjectRef
from scientist.runtime_contracts import (
    MAX_CONTEXT_BYTES,
    MAX_FILE_BYTES,
    MAX_WORKSPACE_BYTES,
    MAX_WORKSPACE_FILES,
    RUNTIME_COMMIT,
    RuntimeContextV1,
    validate_workspace_path,
)


class CheckpointIntegrityError(ValueError):
    """A checkpoint's durable bytes or database identity failed validation."""


_trusted_pins: tuple[str, str, str, str] | None = None


def configure_trusted_pins(*, image_digest: str, skills_digest: str,
                           environment_digest: str, runtime_commit: str = RUNTIME_COMMIT) -> None:
    """Install immutable image/runtime pins from trusted application config."""
    global _trusted_pins
    _trusted_pins = (image_digest, skills_digest, environment_digest, runtime_commit)


def capture(
    db: Session,
    run_id: UUID,
    generation: int,
    context: bytes,
    workspace_dir: Path,
) -> CheckpointManifest:
    """Store a quiescent context/workspace and append its manifest.

    The caller owns the transaction. Object writes, registry rows and the
    checkpoint row therefore become usable together when its transaction is
    committed; this function deliberately never commits.
    """
    if not isinstance(context, bytes) or len(context) > MAX_CONTEXT_BYTES:
        raise CheckpointIntegrityError("context exceeds 1 MiB")
    row = db.execute(
        text("SELECT * FROM runs WHERE id = :run FOR UPDATE"), {"run": run_id}
    ).mappings().one_or_none()
    if row is None or row["generation"] != generation or row["state"] != "running":
        raise CheckpointIntegrityError("run generation is not active")
    if row["lease_expires_at"] is None or row["lease_expires_at"] <= db.execute(
        text("SELECT now()")
    ).scalar_one():
        raise CheckpointIntegrityError("run lease has expired")
    if row["plan_digest"] is None:
        raise CheckpointIntegrityError("run has no approved plan")
    if _trusted_pins is None:
        raise CheckpointIntegrityError("trusted runtime pins are not configured")

    try:
        supplied = json.loads(context, object_pairs_hook=_unique_object)
        if not isinstance(supplied, dict):
            raise ValueError("context must be a JSON object")
        file_entries, _ = _read_workspace(workspace_dir)
        actual_workspace = [
            {"path": path, "sha256": digest, "size": len(data)}
            for path, digest, data in file_entries
        ]
        if supplied.get("workspace_manifest") != actual_workspace:
            raise ValueError("context workspace manifest differs from staged bytes")
        parsed = RuntimeContextV1.model_validate(supplied)
    except (ValueError, TypeError, OSError, json.JSONDecodeError) as exc:
        raise CheckpointIntegrityError("invalid checkpoint context or workspace") from exc

    if (
        parsed.run_id != run_id
        or parsed.generation != generation
        or parsed.revision != row["revision"]
        or parsed.plan_digest != row["plan_digest"].strip()
        or parsed.runtime_commit != RUNTIME_COMMIT
    ):
        raise CheckpointIntegrityError("checkpoint context differs from authorized run")
    if (parsed.image_digest, parsed.skills_digest, parsed.environment_digest,
            parsed.runtime_commit) != _trusted_pins:
        raise CheckpointIntegrityError("context differs from trusted runtime pins")
    input_snapshot = db.execute(text("""
        SELECT s.digest FROM input_snapshots s WHERE s.run_id=:run AND s.project_id=:project
    """), {"run": run_id, "project": row["project_id"]}).scalar_one_or_none()
    plan_row = db.execute(text("""
        SELECT digest, plan FROM plan_revisions WHERE run_id=:run AND revision=:revision
            AND project_id=:project
    """), {"run": run_id, "revision": row["revision"], "project": row["project_id"]}).mappings().one_or_none()
    if (input_snapshot is None or input_snapshot.strip() != parsed.input_snapshot_digest
            or plan_row is None or plan_row["digest"].strip() != parsed.plan_digest
            or plan_row["plan"] != parsed.plan.model_dump(mode="json")):
        raise CheckpointIntegrityError("context snapshot or approved plan differs from database")
    approved = db.execute(
        text("""SELECT 1 FROM approvals WHERE run_id = :run AND revision = :revision
                AND plan_digest = :digest"""),
        {"run": run_id, "revision": row["revision"], "digest": row["plan_digest"].strip()},
    ).scalar_one_or_none()
    if approved is None:
        raise CheckpointIntegrityError("run approval is no longer valid")

    # The boundary acknowledgement binds the exact validated request bytes. The
    # controller has already derived the workspace manifest before serializing.
    context_bytes = context
    context_ref = objects.put(db, row["project_id"], _stream(context_bytes), "application/json")
    workspace_refs = [
        objects.put(db, row["project_id"], _stream(data), "application/octet-stream")
        for _, _, data in file_entries
    ]
    sequence = db.execute(
        text("SELECT COALESCE(MAX(revision), 0) + 1 FROM checkpoints WHERE run_id = :run"),
        {"run": run_id},
    ).scalar_one()
    manifest = CheckpointManifest(
        schema_version=1,
        run_id=run_id,
        revision=sequence,
        plan_digest=parsed.plan_digest,
        runtime_commit=parsed.runtime_commit,
        image_digest=parsed.image_digest,
        skills_digest=parsed.skills_digest,
        context=context_ref,
        workspace=workspace_refs,
        environment_digest=parsed.environment_digest,
        operation_ids=[mapping.operation_id for mapping in parsed.operation_mappings],
    )
    db.execute(
        text("""INSERT INTO checkpoints (id, run_id, revision, manifest)
                VALUES (:id, :run, :revision, CAST(:manifest AS jsonb))"""),
        {
            "id": uuid4(),
            "run": run_id,
            "revision": sequence,
            "manifest": json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
        },
    )
    # A durable boundary renews only this still-current fenced generation. The
    # caller commits it atomically with the boundary acknowledgement.
    renewed = db.execute(text("""
        UPDATE runs SET lease_expires_at=now() + interval '300 seconds'
        WHERE id=:run AND generation=:generation AND state='running'
            AND lease_expires_at > now()
    """), {"run": run_id, "generation": generation})
    if renewed.rowcount != 1:
        raise CheckpointIntegrityError("run lease changed before checkpoint commit")
    return manifest


def restore(db: Session, manifest: CheckpointManifest, workspace_dir: Path) -> bytes:
    """Verify all durable references before atomically publishing the workspace."""
    try:
        manifest = CheckpointManifest.model_validate(manifest.model_dump(mode="json"))
        if manifest.runtime_commit != RUNTIME_COMMIT:
            raise CheckpointIntegrityError("runtime pin differs from platform pin")
        if _trusted_pins is None:
            raise CheckpointIntegrityError("trusted runtime pins are not configured")
        row = db.execute(
            text("""SELECT id, manifest FROM checkpoints
                    WHERE run_id = :run AND revision = :revision"""),
            {"run": manifest.run_id, "revision": manifest.revision},
        ).mappings().one_or_none()
        if row is None:
            raise CheckpointIntegrityError("checkpoint is not durably registered")
        persisted = CheckpointManifest.model_validate(row["manifest"])
        if persisted != manifest:
            raise CheckpointIntegrityError("checkpoint manifest differs from durable record")
        run = db.execute(text("SELECT project_id FROM runs WHERE id=:run"),
                         {"run": manifest.run_id}).one_or_none()
        if run is None:
            raise CheckpointIntegrityError("checkpoint run does not exist")

        _verify_registered_ref(db, manifest.context)
        context_bytes = _read_verified(manifest.context)
        if len(context_bytes) > MAX_CONTEXT_BYTES:
            raise CheckpointIntegrityError("stored context exceeds 1 MiB")
        context_data = json.loads(context_bytes, object_pairs_hook=_unique_object)
        context = RuntimeContextV1.model_validate(context_data)
        if (
            context.run_id != manifest.run_id
            or context.plan_digest != manifest.plan_digest
            or context.runtime_commit != manifest.runtime_commit
            or context.image_digest != manifest.image_digest
            or context.skills_digest != manifest.skills_digest
            or context.environment_digest != manifest.environment_digest
            or [item.operation_id for item in context.operation_mappings] != manifest.operation_ids
        ):
            raise CheckpointIntegrityError("context identity differs from manifest")
        if (context.image_digest, context.skills_digest, context.environment_digest,
                context.runtime_commit) != _trusted_pins:
            raise CheckpointIntegrityError("context differs from trusted runtime pins")
        plan_row = db.execute(text("""
            SELECT digest, plan FROM plan_revisions WHERE run_id=:run AND revision=:revision
                AND project_id=:project
        """), {"run": manifest.run_id, "revision": context.revision,
                "project": run.project_id}).mappings().one_or_none()
        input_digest = db.execute(text("""
            SELECT digest FROM input_snapshots WHERE run_id=:run AND project_id=:project
        """), {"run": manifest.run_id, "project": run.project_id}).scalar_one_or_none()
        if (context.project_id != run.project_id or plan_row is None
                or plan_row["digest"].strip() != context.plan_digest
                or plan_row["plan"] != context.plan.model_dump(mode="json")
                or input_digest is None or input_digest.strip() != context.input_snapshot_digest):
            raise CheckpointIntegrityError("context approval snapshot differs from database")
        approval = db.execute(text("""
            SELECT 1 FROM approvals WHERE run_id=:run AND revision=:revision
                AND plan_digest=:digest
        """), {"run": manifest.run_id, "revision": context.revision,
                "digest": context.plan_digest}).scalar_one_or_none()
        if approval is None:
            raise CheckpointIntegrityError("checkpoint approval is not durable")

        if len(context.workspace_manifest) != len(manifest.workspace):
            raise CheckpointIntegrityError("workspace manifest length mismatch")
        workspace_data: list[tuple[str, bytes]] = []
        total = 0
        for entry, ref in zip(context.workspace_manifest, manifest.workspace, strict=True):
            _verify_registered_ref(db, ref)
            data = _read_verified(ref)
            if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256 or ref.sha256 != entry.sha256 or ref.size != entry.size:
                raise CheckpointIntegrityError("workspace entry differs from stored object")
            total += len(data)
            if total > MAX_WORKSPACE_BYTES:
                raise CheckpointIntegrityError("workspace exceeds 64 MiB")
            workspace_data.append((entry.path, data))
        if len(workspace_data) > MAX_WORKSPACE_FILES:
            raise CheckpointIntegrityError("workspace exceeds file count limit")

        _publish_workspace(Path(workspace_dir), workspace_data)
        return context_bytes
    except CheckpointIntegrityError:
        raise
    except Exception as exc:
        # Validation, storage and filesystem errors fail closed before any runtime
        # is allowed to import or execute restored state.
        raise CheckpointIntegrityError("checkpoint restore validation failed") from exc


def _read_workspace(root: Path) -> tuple[list[tuple[str, str, bytes]], int]:
    root = Path(root)
    root_stat = root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise ValueError("workspace root must be a real directory")
    entries: list[tuple[str, str, bytes]] = []
    total = 0
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        for name in list(dirs):
            candidate = base / name
            mode = candidate.lstat().st_mode
            if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                raise ValueError("workspace contains a non-directory entry")
        for name in files:
            candidate = base / name
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
                raise ValueError("workspace contains a non-regular or linked file")
            relative = candidate.relative_to(root).as_posix()
            validate_workspace_path(relative)
            if info.st_size > MAX_FILE_BYTES:
                raise ValueError("workspace file exceeds 20 MiB")
            descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
            try:
                opened = os.fstat(descriptor)
                if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                        or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
                    raise ValueError("workspace file changed while opening")
                with os.fdopen(descriptor, "rb", closefd=False) as source:
                    data = source.read(MAX_FILE_BYTES + 1)
                after = os.fstat(descriptor)
                if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
                    raise ValueError("workspace file changed while reading")
            finally:
                os.close(descriptor)
            if len(data) != info.st_size or len(data) > MAX_FILE_BYTES:
                raise ValueError("workspace changed while reading")
            total += len(data)
            if total > MAX_WORKSPACE_BYTES:
                raise ValueError("workspace exceeds 64 MiB")
            entries.append((relative, hashlib.sha256(data).hexdigest(), data))
            if len(entries) > MAX_WORKSPACE_FILES:
                raise ValueError("workspace exceeds 1024 files")
    entries.sort(key=lambda item: item[0])
    return entries, total


def _verify_registered_ref(db: Session, ref: ObjectRef) -> None:
    if ref.key != f"{ref.project_id}/{ref.sha256}" or ref.content_type != "application/octet-stream":
        # Objects are physically stored with the canonical octet-stream type.
        raise CheckpointIntegrityError("invalid object reference")
    registered = db.execute(
        text("SELECT project_id, sha256, size, content_type FROM stored_objects WHERE key = :key"),
        {"key": ref.key},
    ).one_or_none()
    if registered is None or (
        registered.project_id != ref.project_id
        or registered.sha256.strip() != ref.sha256
        or registered.size != ref.size
        or registered.content_type != ref.content_type
    ):
        raise CheckpointIntegrityError("object registry metadata differs")


def _read_verified(ref: ObjectRef) -> bytes:
    if ref.size > MAX_FILE_BYTES and ref.size > MAX_CONTEXT_BYTES:
        raise CheckpointIntegrityError("object exceeds checkpoint limits")
    with objects.open_verified(ref) as source:
        data = source.read(MAX_WORKSPACE_BYTES + 1)
    if len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.sha256:
        raise CheckpointIntegrityError("object bytes differ from reference")
    return data


def _publish_workspace(target: Path, files: list[tuple[str, bytes]]) -> None:
    target = target.absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and (target.is_symlink() or not target.is_dir()):
        raise CheckpointIntegrityError("workspace target must be a real directory")
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent))
    backup = target.with_name(f".{target.name}.previous-{uuid4().hex}")
    moved_old = False
    try:
        for relative, data in files:
            validate_workspace_path(relative)
            destination = staging.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(destination, 0o600)
        if target.exists():
            os.replace(target, backup)
            moved_old = True
        try:
            os.replace(staging, target)
        except OSError:
            if moved_old:
                os.replace(backup, target)
                moved_old = False
            raise
        if moved_old:
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if moved_old and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)


def _stream(data: bytes) -> BinaryIO:
    from io import BytesIO

    return BytesIO(data)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value
