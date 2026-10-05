"""Load only selected, hash-pinned instruction files as untrusted text."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

from scientist.capability_registry import CapabilitySelection, REVIEWED_CAPABILITY_ALLOWLIST


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_INSTRUCTION_FILE_BYTES = 1_000_000


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
