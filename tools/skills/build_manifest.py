"""Hash the pinned catalog as inert files; bundling never enables execution."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

CATALOG_COMMIT = "154988403bb5a18e9d3c0ce4e6d5e2e4b184a298"
OUTPUT = Path(__file__).resolve().parents[2] / "runtime/skills-manifest.json"


def build(source: Path) -> dict:
    archive = subprocess.check_output([
        "rtk", "proxy", "git", "-C", str(source), "archive", "--format=tar", CATALOG_COMMIT, "skills",
    ])
    skills: dict[str, list[dict]] = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for member in bundle:
            if member.isdir():
                continue
            path = PurePosixPath(member.name)
            if not member.isfile() or path.parts[0] != "skills" or len(path.parts) < 3 or ".." in path.parts:
                raise ValueError("catalog contains an unsafe entry")
            data = bundle.extractfile(member).read()
            skills.setdefault(path.parts[1], []).append({
                "path": PurePosixPath(*path.parts[2:]).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            })
    if len(skills) != 177 or any(not any(f["path"] == "SKILL.md" for f in fs) for fs in skills.values()):
        raise ValueError("catalog does not match the pinned 177-skill set")
    payload = {"schema_version": 1, "catalog_commit": CATALOG_COMMIT, "skills": [
        {"name": name, "files": sorted(files, key=lambda f: f["path"])} for name, files in sorted(skills.items())
    ]}
    payload["manifest_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-source", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    encoded = json.dumps(build(args.catalog_source), sort_keys=True, indent=2) + "\n"
    if args.check:
        if OUTPUT.read_text() != encoded:
            raise SystemExit("catalog manifest differs from the pinned archive")
    else:
        OUTPUT.write_text(encoded)
