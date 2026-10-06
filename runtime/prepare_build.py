"""Prepare a secret-free worker build context from pinned Git archives."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

RUNTIME_COMMIT = "bd0affe5e5f723579df8902852f5d0c47795f355"
RUNTIME_ARCHIVE_SHA256 = "e97403574699e253952c14dcfe03682f62985f59f529d624d92553694c7e8fea"
CATALOG_COMMIT = "154988403bb5a18e9d3c0ce4e6d5e2e4b184a298"
MODULES = ("__init__.py", "contracts.py", "model_payload.py", "runtime_adapter.py", "runtime_contracts.py",
           "capability_registry.py", "instruction_loader.py", "resource_recipe.py")


def archive(source: Path, commit: str, *paths: str) -> bytes:
    return subprocess.check_output(
        ["rtk", "proxy", "git", "-C", str(source), "archive", "--format=tar", commit, *paths]
    )


def extract(data: bytes, destination: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data)) as bundle:
        # Source inputs may never create special files or links in the context.
        if any(not (item.isfile() or item.isdir()) for item in bundle.getmembers()):
            raise ValueError("source archive contains a link or special file")
        bundle.extractall(destination, filter="data")


def _catalog(root: Path, catalog_source: Path, output: Path) -> None:
    manifest_path = root / "runtime/skills-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    payload = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if manifest["catalog_commit"] != CATALOG_COMMIT or digest != manifest["manifest_sha256"]:
        raise ValueError("skill manifest pin or digest mismatch")
    selected = ["skills/" + skill["name"] for skill in manifest["skills"]]
    catalog = archive(catalog_source, CATALOG_COMMIT, "LICENSE.md", *selected)
    extract(catalog, output)
    expected = set()
    for skill in manifest["skills"]:
        for item in skill["files"]:
            relative = Path("skills") / skill["name"] / item["path"]
            data = (output / relative).read_bytes()
            if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError("skill resource integrity mismatch")
            expected.add(relative.as_posix())
    actual = {p.relative_to(output).as_posix() for p in (output / "skills").rglob("*") if p.is_file()}
    if actual != expected:
        raise ValueError("skill archive does not exactly match the manifest")


def _hashes(output: Path) -> None:
    hashes = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(output.rglob("*")) if p.is_file()}
    (output / "source-hashes.json").write_text(json.dumps(hashes, sort_keys=True, indent=2) + "\n")


def prepare(hermes_source: Path, catalog_source: Path, output: Path) -> None:
    if sys.version_info < (3, 13):
        raise RuntimeError("build preparation requires Python 3.13 or newer")
    root = Path(__file__).resolve().parents[1]
    native = archive(hermes_source, RUNTIME_COMMIT)
    if hashlib.sha256(native).hexdigest() != RUNTIME_ARCHIVE_SHA256:
        raise ValueError("native source archive digest mismatch")
    # Never overwrite an existing context or copy a working checkout's private files.
    output.mkdir(parents=True, exist_ok=False)
    extract(native, output / "hermes")
    _catalog(root, catalog_source, output)
    (output / "scientist").mkdir()
    (output / "runtime").mkdir()
    for name in MODULES:
        shutil.copyfile(root / "backend/src/scientist" / name, output / "scientist" / name)
    for name in ("__init__.py", "entrypoint.py", "skills-manifest.json"):
        shutil.copyfile(root / "runtime" / name, output / "runtime" / name)
    shutil.copyfile(root / "docs/skills/capability-registry.json", output / "runtime/capability-registry.json")
    shutil.copyfile(root / "runtime/Dockerfile", output / "Dockerfile")
    _hashes(output)


def prepare_server(catalog_source: Path, output: Path) -> None:
    """Same immutable catalog as the worker; whitelist application sources only."""
    if sys.version_info < (3, 13):
        raise RuntimeError("build preparation requires Python 3.13 or newer")
    root = Path(__file__).resolve().parents[1]
    output.mkdir(parents=True, exist_ok=False)
    _catalog(root, catalog_source, output)
    selected = [*sorted((root / "backend/src/scientist").glob("*.py")),
                *sorted((root / "backend/migrations").glob("*.sql")),
                *sorted((root / "runtime/profiles").glob("*.json")),
                *(root / f"runtime/{name}" for name in
                  ("__init__.py", "entrypoint.py", "prepare.py", "skills-manifest.json", "requirements.lock")),
                root / "docs/skills/capability-registry.json",
                root / "backend/Dockerfile", root / "backend/Dockerfile.dockerignore"]
    for source in selected:
        if source.is_symlink() or not source.is_file():
            raise ValueError("unsafe server source")
        target = output / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    _hashes(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path)
    parser.add_argument("--catalog-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--server", action="store_true")
    args = parser.parse_args()
    if args.server:
        prepare_server(args.catalog_source, args.output)
    else:
        if args.hermes_source is None:
            parser.error("--hermes-source is required for a worker context")
        prepare(args.hermes_source, args.catalog_source, args.output)
