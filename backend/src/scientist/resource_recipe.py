"""Fixed, read-only CPU resource recipe for the X0 worker pilot."""

from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path


RECIPE_ID = "get-available-resources"
PROFILE_ID = "prof.cpu-sci@py3.13"
MAX_ARTIFACT_BYTES = 1_048_576
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class WorkerResources:
    cpu_count: int
    cpu_quota_cores: float | None
    memory_limit_bytes: int | None
    memory_current_bytes: int | None


@dataclass(frozen=True)
class ArtifactManifestDescriptor:
    recipe_id: str
    profile_id: str
    instruction_fingerprint: str
    result_sha256: str
    size_bytes: int


def collect_worker_resources() -> WorkerResources:
    """Return bounded cgroup CPU/memory limits; never enumerate environment or host data."""
    cpu_count = max(1, os.cpu_count() or 1)
    affinity = getattr(os, "sched_getaffinity", None)
    if affinity is not None:
        try:
            cpu_count = max(1, min(cpu_count, len(affinity(0))))
        except OSError:
            pass
    cpu_quota = _read_cpu_quota()
    if cpu_quota is not None:
        cpu_count = max(1, min(cpu_count, math.ceil(cpu_quota)))
    return WorkerResources(
        cpu_count=cpu_count,
        cpu_quota_cores=cpu_quota,
        memory_limit_bytes=_read_memory_value("/sys/fs/cgroup/memory.max"),
        memory_current_bytes=_read_memory_value("/sys/fs/cgroup/memory.current"),
    )


def get_available_resources_recipe(resources: WorkerResources) -> dict[str, int | float | bool | None]:
    """Render the pilot recipe's small, fixed result schema."""
    if resources.cpu_count < 1 or (resources.memory_limit_bytes is not None and resources.memory_limit_bytes < 1):
        raise ValueError("worker resource limits are invalid")
    return {
        "cpu_count": resources.cpu_count,
        "cpu_quota_cores": resources.cpu_quota_cores,
        "memory_limit_bytes": resources.memory_limit_bytes,
        "memory_current_bytes": resources.memory_current_bytes,
        "gpu_validation": False,
    }


def build_artifact_descriptor(
    result: bytes,
    *,
    profile_id: str,
    instruction_fingerprint: str,
    max_bytes: int = MAX_ARTIFACT_BYTES,
) -> ArtifactManifestDescriptor:
    if profile_id != PROFILE_ID:
        raise ValueError("recipe profile is not reviewed")
    if not isinstance(instruction_fingerprint, str) or not _SHA256.fullmatch(instruction_fingerprint):
        raise ValueError("instruction fingerprint is invalid")
    if (
        not isinstance(result, bytes)
        or isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes < 0
        or max_bytes > MAX_ARTIFACT_BYTES
    ):
        raise ValueError("artifact size limit is invalid")
    if len(result) > max_bytes:
        raise ValueError("artifact exceeds size limit")
    return ArtifactManifestDescriptor(
        recipe_id=RECIPE_ID,
        profile_id=profile_id,
        instruction_fingerprint=instruction_fingerprint,
        result_sha256=hashlib.sha256(result).hexdigest(),
        size_bytes=len(result),
    )


def _read_cpu_quota() -> float | None:
    try:
        quota_text, period_text = Path("/sys/fs/cgroup/cpu.max").read_text(encoding="ascii").split()
        if quota_text == "max":
            return None
        quota, period = int(quota_text), int(period_text)
        if quota <= 0 or period <= 0:
            return None
        return round(quota / period, 3)
    except (OSError, ValueError):
        return None


def _read_memory_value(path: str) -> int | None:
    try:
        value = Path(path).read_text(encoding="ascii").strip()
        if value == "max":
            return None
        number = int(value)
        return number if number > 0 else None
    except (OSError, ValueError):
        return None
