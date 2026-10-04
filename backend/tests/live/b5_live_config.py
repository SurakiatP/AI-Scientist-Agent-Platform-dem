"""Config loader for the B5 live suite. Reads files only; never talks to Docker and never reads secrets.

Real config: $B5_LIVE_CONFIG, default <repo>/.local/b5-live.json. b5_live.example.json is the only key list.
Any problem is NOT RUN (exit 77), never a pass.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
NOT_RUN = 77
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
SECRET_NAMES = ("broker_capability_key", "database_url", "master_key", "s3_access_key", "s3_secret_key")
EXAMPLE = Path(__file__).with_name("b5_live.example.json")


def not_run(reason: str):
    print(json.dumps({"status": "NOT RUN", "reason": reason}))
    raise SystemExit(NOT_RUN)


def digest(ref: str) -> str:
    return ref.split("@", 1)[1]


def _path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _allowed(path: Path) -> bool:
    path = path.resolve()
    return not path.is_relative_to(ROOT) or path.is_relative_to(ROOT / ".local")


def _load() -> SimpleNamespace:
    cfg_path = _path(os.environ.get("B5_LIVE_CONFIG") or ".local/b5-live.json")
    if not cfg_path.is_file():
        not_run(f"config file missing: {cfg_path.name}")
    try:
        raw = json.loads(cfg_path.read_text())
        keys = json.loads(EXAMPLE.read_text()).keys()
    except (OSError, ValueError) as exc:
        return not_run(f"config unreadable: {type(exc).__name__}")
    if not isinstance(raw, dict) or set(raw) != set(keys):
        not_run("config keys differ from b5_live.example.json")
    images = [raw["worker_image"], raw["server_image"], raw["postgres_image"], raw["minio_image"]]
    fixtures = raw["fixture_images"]
    if not isinstance(fixtures, dict) or set(fixtures) != {"happy", "counter", "barrier", "checkpoint_fault"}:
        not_run("fixture_images must have happy, counter, barrier, checkpoint_fault")
    images += list(fixtures.values())
    if not all(isinstance(i, str) and IMAGE_RE.match(i) for i in images):
        not_run("every image must be repo@sha256:<64 hex>")
    if urlsplit(raw["database_url"]).password:
        not_run("database_url must not contain a password")
    hashes = _path(raw["server_source_hashes"])
    if not hashes.is_file():
        not_run("server_source_hashes file missing")
    private = _path(raw["private_dir"])
    if not private.is_dir() or any(not (private / n).is_file() for n in SECRET_NAMES):  # existence only
        not_run("private_dir missing or lacks a required file")
    evidence_env = os.environ.get("B5_LIVE_EVIDENCE_DIR")
    if not evidence_env:
        not_run("B5_LIVE_EVIDENCE_DIR not set")
    evidence = Path(evidence_env).resolve()
    if not evidence.is_dir():
        not_run("evidence dir does not exist")
    for label, path in (("private_dir", private), ("evidence dir", evidence)):
        if not _allowed(path):
            not_run(f"{label} must be outside the repo or under .local")
    for label, value in (("repo root", str(ROOT)), ("evidence dir", str(evidence)), ("python", sys.executable)):
        if re.search(r"\s", value):
            not_run(f"{label} path contains whitespace")
    cfg = SimpleNamespace(**raw)
    cfg.root, cfg.evidence, cfg.private_dir = ROOT, evidence, private.resolve()
    cfg.server_source_hashes = hashes
    cfg.worker_image_id, cfg.server_image_id = digest(raw["worker_image"]), digest(raw["server_image"])
    return cfg


CFG = _load()
