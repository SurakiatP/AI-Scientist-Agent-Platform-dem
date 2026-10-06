"""Load only selected, hash-pinned instruction files as untrusted text."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Callable, Mapping

from scientist.capability_registry import (
    CATALOG_COMMIT,
    CapabilitySelection,
    RegistryError,
    REVIEWED_CAPABILITY_ALLOWLIST,
    load_registry,
)
from scientist.contracts import RuntimePins, ScientificBinding, ScientificBindingV2


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_INSTRUCTION_FILE_BYTES = 1_000_000
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_FILES = 20_000
MAX_CATALOG_FILE_BYTES = 64 * 1024 * 1024
MAX_CATALOG_TOTAL_BYTES = 256 * 1024 * 1024
_SKILL_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class InstructionLoadError(ValueError):
    """An instruction resource is untrusted, unpinned, or exceeds its budget."""


@dataclass(frozen=True)
class InstructionBundle:
    text: str
    files: tuple[str, ...]
    token_count: int
    capability_ids: tuple[str, ...]
    profile_ids: tuple[str, ...]
    registry_sha256: str
    instruction_fingerprint: str


def load_instruction_pins(manifest_path: str | Path) -> Mapping[str, str]:
    """Read a bounded, digest-verified manifest and return canonical file pins."""
    source = Path(manifest_path)
    try:
        info = source.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
            raise InstructionLoadError("instruction manifest is unsafe or oversized")
        fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            chunks = bytearray()
            while len(chunks) <= MAX_MANIFEST_BYTES:
                block = os.read(fd, min(65_536, MAX_MANIFEST_BYTES + 1 - len(chunks)))
                if not block:
                    break
                chunks.extend(block)
        finally:
            os.close(fd)
        if len(chunks) > MAX_MANIFEST_BYTES:
            raise InstructionLoadError("instruction manifest is oversized")
        document = json.loads(chunks, object_pairs_hook=_unique_object)
    except InstructionLoadError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise InstructionLoadError("instruction manifest is invalid") from exc

    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or document.get("catalog_commit") != CATALOG_COMMIT
        or not isinstance(document.get("manifest_sha256"), str)
        or not _SHA256.fullmatch(document["manifest_sha256"])
    ):
        raise InstructionLoadError("instruction manifest provenance is invalid")
    payload = {key: value for key, value in document.items() if key != "manifest_sha256"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != document["manifest_sha256"]:
        raise InstructionLoadError("instruction manifest digest mismatch")

    skills = document.get("skills")
    if not isinstance(skills, list) or not skills or len(skills) > 177:
        raise InstructionLoadError("instruction manifest skill list is invalid")
    pins: dict[str, str] = {}
    seen_skills: set[str] = set()
    file_count = 0
    total_bytes = 0
    for skill in skills:
        if not isinstance(skill, dict):
            raise InstructionLoadError("instruction manifest skill record is invalid")
        skill_id, files = skill.get("name"), skill.get("files")
        if not isinstance(skill_id, str) or not _SKILL_ID.fullmatch(skill_id) or skill_id in seen_skills:
            raise InstructionLoadError("instruction manifest has a duplicate or invalid skill")
        if not isinstance(files, list) or not files:
            raise InstructionLoadError("instruction manifest file list is invalid")
        seen_skills.add(skill_id)
        has_skill_text = False
        for entry in files:
            file_count += 1
            if file_count > MAX_MANIFEST_FILES or not isinstance(entry, dict):
                raise InstructionLoadError("instruction manifest file count is invalid")
            relative, digest, size = entry.get("path"), entry.get("sha256"), entry.get("size")
            if (
                not isinstance(relative, str)
                or not relative
                or "\\" in relative
                or ":" in relative
                or PurePosixPath(relative).is_absolute()
                or any(part in {"", ".", ".."} for part in relative.split("/"))
                or any(ord(char) < 32 or ord(char) == 127 for char in relative)
                or not isinstance(digest, str)
                or not _SHA256.fullmatch(digest)
                or type(size) is not int
                or size < 0
                or size > MAX_CATALOG_FILE_BYTES
            ):
                raise InstructionLoadError("instruction manifest contains an invalid file pin")
            total_bytes += size
            if total_bytes > MAX_CATALOG_TOTAL_BYTES:
                raise InstructionLoadError("instruction manifest total file size exceeds limit")
            path = f"skills/{skill_id}/{PurePosixPath(relative).as_posix()}"
            if path in pins:
                raise InstructionLoadError("instruction manifest contains duplicate paths")
            pins[path] = digest
            if path == f"skills/{skill_id}/SKILL.md":
                if size == 0:
                    raise InstructionLoadError("instruction manifest contains an empty skill instruction")
                has_skill_text = True
        if not has_skill_text:
            raise InstructionLoadError("instruction manifest skill is missing its SKILL.md pin")
    return MappingProxyType(pins)


def validate_scientific_binding(
    binding: ScientificBinding | ScientificBindingV2,
    *,
    registry_path: str | Path,
    bundle_root: str | Path,
    pinned_hashes: Mapping[str, str],
    expected_image_digest: str,
    expected_runtime_pins: RuntimePins | None = None,
) -> InstructionBundle:
    """Re-resolve scientific authority using only supervisor-pinned inputs."""
    if not isinstance(binding, (ScientificBinding, ScientificBindingV2)):
        raise InstructionLoadError("scientific binding is invalid")
    if not isinstance(expected_image_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_image_digest):
        raise InstructionLoadError("trusted image digest is invalid")
    if isinstance(binding, ScientificBindingV2):
        if (
            not isinstance(expected_runtime_pins, RuntimePins)
            or binding.agent_runtime_pins != expected_runtime_pins
            or binding.agent_runtime_pins.image_digest != expected_image_digest
        ):
            raise InstructionLoadError("scientific agent runtime pins differ from trusted runtime pins")
    elif binding.image_digest != expected_image_digest:
        raise InstructionLoadError("scientific binding image differs from trusted image")
    registry_file = Path(registry_path)
    try:
        info = registry_file.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
            raise InstructionLoadError("capability registry is unsafe or oversized")
        registry = load_registry(registry_file)
    except InstructionLoadError:
        raise
    except (OSError, RegistryError) as exc:
        raise InstructionLoadError("capability registry is invalid") from exc
    if binding.catalog_commit != registry.catalog_commit or binding.registry_sha256 != registry.registry_sha256:
        raise InstructionLoadError("scientific binding registry provenance differs")
    try:
        selection = registry.select(binding.capability_ids)
    except (RegistryError, TypeError) as exc:
        raise InstructionLoadError("scientific capability selection is invalid") from exc
    if list(selection.capability_ids) != binding.capability_ids:
        raise InstructionLoadError("scientific binding capability selection differs")
    if isinstance(binding, ScientificBinding) and selection.profile_ids != (binding.profile_id,):
        raise InstructionLoadError("scientific binding capability profile differs")
    bundle = load_instruction_bundle(
        selection,
        bundle_root,
        pinned_hashes,
        token_counter=lambda text: len(text.encode("utf-8")),
        token_budget=MAX_INSTRUCTION_FILE_BYTES,
    )
    if bundle.instruction_fingerprint != binding.instruction_fingerprint:
        raise InstructionLoadError("scientific instruction fingerprint differs")
    return bundle


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise InstructionLoadError("instruction manifest has duplicate JSON keys")
        value[key] = item
    return value


def load_instruction_bundle(
    selection: CapabilitySelection,
    bundle_root: str | Path,
    pinned_hashes: Mapping[str, str],
    *,
    token_counter: Callable[[str], int],
    token_budget: int,
    resource_paths: tuple[str, ...] = (),
) -> InstructionBundle:
    """Read selected SKILL.md files plus explicitly selected pinned resources.

    The counter must be supplied by the trusted host. This function returns plain
    text; no content is interpreted as tools, package names, or destinations.
    """
    if isinstance(token_budget, bool) or not isinstance(token_budget, int) or token_budget < 1:
        raise InstructionLoadError("token budget must be positive")
    if (
        not selection.capabilities
        or len(selection.capability_ids) != len(set(selection.capability_ids))
        or any(
            item.id not in REVIEWED_CAPABILITY_ALLOWLIST
            or item.instruction_path != f"skills/{item.id}/SKILL.md"
            or not _SHA256.fullmatch(item.instruction_sha256)
            for item in selection.capabilities
        )
    ):
        raise InstructionLoadError("selection is outside the reviewed instruction allowlist")
    root = Path(bundle_root)
    selected_ids = set(selection.capability_ids)
    selected_paths = {item.instruction_path for item in selection.capabilities}
    requested: set[str] = set(selected_paths)
    for raw_path in resource_paths:
        normalized = _normalize_relative_path(raw_path)
        if not any(normalized.startswith(f"skills/{skill_id}/") for skill_id in selected_ids):
            raise InstructionLoadError("resource is outside the selected instruction set")
        requested.add(normalized)

    chunks: list[str] = []
    used_files: list[str] = []
    identity: list[tuple[str, str, str]] = []
    caps_by_path = {item.instruction_path: item for item in selection.capabilities}
    for relative_path in sorted(requested):
        normalized = _normalize_relative_path(relative_path)
        expected = pinned_hashes.get(normalized)
        capability = caps_by_path.get(normalized)
        if capability is not None:
            if expected != capability.instruction_sha256:
                raise InstructionLoadError("selected instruction hash does not match registry pin")
            expected = capability.instruction_sha256
        if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
            raise InstructionLoadError("instruction resource has no valid pinned hash")
        target = _safe_file(root, normalized)
        if target.stat().st_size > MAX_INSTRUCTION_FILE_BYTES:
            raise InstructionLoadError("instruction resource exceeds file size limit")
        with target.open("rb") as source:
            content = source.read(MAX_INSTRUCTION_FILE_BYTES + 1)
        if len(content) > MAX_INSTRUCTION_FILE_BYTES:
            raise InstructionLoadError("instruction resource exceeds file size limit")
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected:
            raise InstructionLoadError("instruction resource hash mismatch")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InstructionLoadError("instruction resource is not UTF-8") from exc
        chunks.append(text)
        used_files.append(normalized)
        owner = capability.id if capability is not None else _skill_id_for_path(normalized)
        identity.append((owner, normalized, expected))

    bundled_text = "\n\n".join(chunks)
    count = token_counter(bundled_text)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise InstructionLoadError("trusted token counter returned an invalid count")
    if count > token_budget:
        raise InstructionLoadError("instruction token budget exceeded")
    fingerprint_input = json.dumps(
        [selection.catalog_commit, selection.registry_sha256, selection.profile_ids, identity],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    fingerprint = hashlib.sha256(fingerprint_input.encode("ascii")).hexdigest()
    return InstructionBundle(
        text=bundled_text,
        files=tuple(used_files),
        token_count=count,
        capability_ids=selection.capability_ids,
        profile_ids=selection.profile_ids,
        registry_sha256=selection.registry_sha256,
        instruction_fingerprint=fingerprint,
    )


def _normalize_relative_path(raw_path: str) -> str:
    if not isinstance(raw_path, str) or "\\" in raw_path:
        raise InstructionLoadError("instruction path is invalid")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise InstructionLoadError("instruction path traversal is forbidden")
    return path.as_posix()


def _safe_file(root: Path, relative_path: str) -> Path:
    current = root
    try:
        for part in PurePosixPath(relative_path).parts:
            current = current / part
            if stat.S_ISLNK(current.lstat().st_mode):
                raise InstructionLoadError("instruction path contains a symlink")
        if not stat.S_ISREG(current.stat().st_mode):
            raise InstructionLoadError("instruction resource is not a regular file")
        current.resolve(strict=True).relative_to(root.resolve(strict=True))
        return current
    except FileNotFoundError as exc:
        raise InstructionLoadError("instruction resource is missing") from exc
    except (OSError, ValueError) as exc:
        if isinstance(exc, InstructionLoadError):
            raise
        raise InstructionLoadError("instruction path is not safely contained") from exc


def _skill_id_for_path(path: str) -> str:
    parts = PurePosixPath(path).parts
    if len(parts) < 3 or parts[0] != "skills":
        raise InstructionLoadError("instruction path is outside the pinned skill bundle")
    return parts[1]
