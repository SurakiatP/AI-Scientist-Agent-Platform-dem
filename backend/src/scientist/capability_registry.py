"""Read-only loader for the committed, audited scientific capability catalog."""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


CATALOG_COMMIT = "154988403bb5a18e9d3c0ce4e6d5e2e4b184a298"
EXPECTED_SKILL_COUNT = 177
REVIEWED_CAPABILITY_ALLOWLIST = frozenset({
    "get-available-resources", "paper-lookup", "exploratory-data-analysis",
    "scientific-visualization",
})
_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RegistryError(ValueError):
    """The checked-in registry failed its pinned schema or policy checks."""


@dataclass(frozen=True)
class CapabilityStatuses:
    analyzed: bool
    implemented: bool = False
    validated: bool = False
    enabled: bool = False
    blocked: bool = False


@dataclass(frozen=True)
class Capability:
    id: str
    wave: str
    profile_id: str | None
    instruction_path: str
    instruction_sha256: str
    statuses: CapabilityStatuses


@dataclass(frozen=True)
class CapabilitySelection:
    catalog_commit: str
    registry_sha256: str
    capabilities: tuple[Capability, ...]

    @property
    def capability_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.capabilities)

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.profile_id for item in self.capabilities if item.profile_id is not None))


@dataclass(frozen=True)
class CapabilityRegistry:
    catalog_commit: str
    registry_sha256: str
    capabilities: Mapping[str, Capability]

    def select(self, capability_ids: list[str] | tuple[str, ...]) -> CapabilitySelection:
        if not capability_ids or len(capability_ids) != len(set(capability_ids)):
            raise RegistryError("selection must contain unique capability ids")
        selected = []
        for capability_id in capability_ids:
            if capability_id not in REVIEWED_CAPABILITY_ALLOWLIST:
                raise RegistryError(f"capability {capability_id!r} is outside the reviewed allowlist")
            capability = self.capabilities.get(capability_id)
            if capability is None:
                raise RegistryError(f"unknown capability {capability_id!r}")
            selected.append(capability)
        return CapabilitySelection(self.catalog_commit, self.registry_sha256, tuple(selected))


def load_registry(path: str | Path) -> CapabilityRegistry:
    source = Path(path)
    raw_bytes = source.read_bytes()
    try:
        document = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryError("registry is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict) or document.get("schema_version") != "2.0":
        raise RegistryError("unsupported registry schema")
    catalog = document.get("catalog")
    raw_skills = document.get("skills")
    if not isinstance(catalog, dict) or catalog.get("commit") != CATALOG_COMMIT:
        raise RegistryError("registry catalog provenance does not match the reviewed pin")
    if not isinstance(raw_skills, dict) or len(raw_skills) != EXPECTED_SKILL_COUNT:
        raise RegistryError("registry must contain exactly 177 skill records")

    capabilities: dict[str, Capability] = {}
    for skill_id, record in raw_skills.items():
        if not isinstance(skill_id, str) or not _ID.fullmatch(skill_id) or not isinstance(record, dict):
            raise RegistryError("registry contains a malformed skill record")
        if record.get("catalog_commit") != CATALOG_COMMIT:
            raise RegistryError(f"skill {skill_id!r} has mismatched catalog provenance")
        audit = record.get("audit_source")
        if not isinstance(audit, dict):
            raise RegistryError(f"skill {skill_id!r} is missing its audit provenance")
        skill_path = audit.get("skill_path")
        digest = audit.get("skill_sha256")
        runtime = record.get("runtime")
        profile_id = runtime.get("image") if isinstance(runtime, dict) else None
        if (
            not isinstance(skill_path, str)
            or skill_path != f"skills/{skill_id}/SKILL.md"
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
            or (profile_id is not None and (not isinstance(profile_id, str) or not profile_id.startswith("prof.")))
        ):
            raise RegistryError(f"skill {skill_id!r} has invalid instruction provenance")
        capabilities[skill_id] = Capability(
            id=skill_id,
            wave=record.get("wave", ""),
            profile_id=profile_id,
            instruction_path=skill_path,
            instruction_sha256=digest,
            statuses=CapabilityStatuses(
                analyzed=audit.get("semantic_reviewed") is True,
                blocked=bool(record.get("prerequisites")),
            ),
        )
    return CapabilityRegistry(
        catalog_commit=CATALOG_COMMIT,
        registry_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        capabilities=MappingProxyType(capabilities),
    )
