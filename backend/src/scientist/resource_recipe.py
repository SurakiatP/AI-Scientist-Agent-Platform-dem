"""Fixed, read-only CPU resource recipe for the X0 worker pilot."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path


RECIPE_ID = "get-available-resources"
PROFILE_ID = "prof.worker-base@py3.14.7"
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


def canonical_resource_result(
    measurement: dict[str, int | float | bool | None],
    *,
    profile_id: str,
    instruction_fingerprint: str,
) -> bytes:
    """Serialize the fixed X0 measurement with its reviewed computation provenance."""
    if profile_id != PROFILE_ID or not _SHA256.fullmatch(instruction_fingerprint):
        raise ValueError("resource result provenance is invalid")
    envelope = {
        "schema_version": 1,
        "recipe_id": RECIPE_ID,
        "profile_id": profile_id,
        "instruction_fingerprint": instruction_fingerprint,
        "measurement": measurement,
    }
    result = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    validate_resource_result(
        result,
        profile_id=profile_id,
        instruction_fingerprint=instruction_fingerprint,
        max_bytes=MAX_ARTIFACT_BYTES,
    )
    return result


def validate_resource_result(
    result: bytes,
    *,
    profile_id: str,
    instruction_fingerprint: str,
    max_bytes: int = MAX_ARTIFACT_BYTES,
) -> dict[str, int | float | bool | None]:
    """Reject noncanonical, oversized, or semantically fabricated X0 results."""
    if (
        not isinstance(result, bytes)
        or type(max_bytes) is not int
        or not 1 <= max_bytes <= MAX_ARTIFACT_BYTES
        or len(result) > max_bytes
        or profile_id != PROFILE_ID
        or not isinstance(instruction_fingerprint, str)
        or not _SHA256.fullmatch(instruction_fingerprint)
    ):
        raise ValueError("resource result is outside its approved bounds")
    try:
        value = json.loads(result)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("resource result is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "recipe_id", "profile_id", "instruction_fingerprint", "measurement"
    }:
        raise ValueError("resource result envelope is invalid")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["recipe_id"] != RECIPE_ID
        or value["profile_id"] != profile_id
        or value["instruction_fingerprint"] != instruction_fingerprint
        or json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8") != result
    ):
        raise ValueError("resource result provenance or canonical encoding is invalid")
    measurement = value["measurement"]
    if not isinstance(measurement, dict) or set(measurement) != {
        "cpu_count", "cpu_quota_cores", "memory_limit_bytes", "memory_current_bytes", "gpu_validation"
    }:
        raise ValueError("resource measurement schema is invalid")
    cpu_count = measurement["cpu_count"]
    quota = measurement["cpu_quota_cores"]
    memory_limit = measurement["memory_limit_bytes"]
    memory_current = measurement["memory_current_bytes"]
    if (
        type(cpu_count) is not int
        or not 1 <= cpu_count <= 4096
        or (quota is not None and (
            isinstance(quota, bool) or not isinstance(quota, (int, float))
            or not math.isfinite(quota) or not 0 < quota <= 4096
        ))
        or (memory_limit is not None and (type(memory_limit) is not int or memory_limit < 1))
        or (memory_current is not None and (type(memory_current) is not int or memory_current < 0))
        or (memory_limit is not None and memory_current is not None and memory_current > memory_limit)
        or measurement["gpu_validation"] is not False
    ):
        raise ValueError("resource measurement values are invalid")
    return measurement


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
