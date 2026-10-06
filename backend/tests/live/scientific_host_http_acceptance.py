#!/usr/bin/env python3
"""W1 real-host acceptance: synthetic provider, sealed profile, receipt recovery and HTTP API."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID, uuid4

PROFILE = "prof.worker-base@py3.14.7"
MANIFEST = "06334236ca1ce7c4ae358f838339a7415e858f02538b9f6fd30dacc94164ac17"
NOT_RUN = 77
IMAGE_REF = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
EXTENSION_KEYS = {
    "b5_live_config", "evidence_dir", "postgres_container_id", "minio_container_id",
    "scientific_bucket", "scientific_fixture_image", "scientific_bundle_dir",
    "sealed_profile_evidence_dir", "web_dist_dir",
}


def self_check() -> None:
    from types import SimpleNamespace
    import types

    name = "scientific_dispatch_fixture"
    path = Path(__file__).with_name("fixtures") / "scientific" / "sitecustomize.py"
    spec = importlib.util.spec_from_file_location(name, path)
    fixture = importlib.util.module_from_spec(spec)
    original_uvicorn = sys.modules.get("uvicorn")
    sys.modules["uvicorn"] = types.SimpleNamespace(run=lambda *args, **kwargs: None)
    try:
        spec.loader.exec_module(fixture)
    finally:
        if original_uvicorn is None:
            sys.modules.pop("uvicorn", None)
        else:
            sys.modules["uvicorn"] = original_uvicorn
    base = SimpleNamespace(kind="llm", payload={"model": "fixture", "messages": []})
    target = SimpleNamespace(url="https://research.example")
    assert fixture._stage(base, target) == "batch"
    final = SimpleNamespace(**{**vars(base), "payload": {"messages": [
        {"role": "assistant", "tool_calls": [
            {"id": "w1-instruction", "function": {"name": "instruction_view",
                "arguments": '{"capability_id":"get-available-resources"}'}},
            {"id": "w1-resources", "function": {"name": "scientific_resources", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "w1-instruction"},
        {"role": "tool", "tool_call_id": "w1-resources"},
    ], "model": "fixture"}})
    assert fixture._stage(final, target) == "final"
    try:
        fixture._stage(SimpleNamespace(**{**vars(base), "kind": "peer"}), target)
    except RuntimeError:
        pass
    else:
        raise AssertionError("unexpected operation was accepted")
    body, _ = fixture._response("batch")
    batch = json.loads(body)["choices"][0]["message"]["tool_calls"]
    assert [(call["id"], call["function"]["name"]) for call in batch] == [
        ("w1-instruction", "instruction_view"), ("w1-resources", "scientific_resources")]
    assert json.loads(batch[0]["function"]["arguments"]) == {"capability_id": "get-available-resources"}
    assert json.loads(batch[1]["function"]["arguments"]) == {}
    assert fixture._hold_boundary(200, True, True, True)
    assert not fixture._hold_boundary(200, False, True, True)
    assert not fixture._hold_boundary(500, True, True, True)
    assert not fixture._hold_boundary(200, True, False, True)  # persisted run-level claim prevents restart reactivation
    assert not fixture._hold_boundary(200, True, True, False)  # unrelated scientific runs pass through
    absent = SimpleNamespace(q=lambda query: [{"relation": None}])
    present = SimpleNamespace(q=lambda query: [{"relation": "w1_boundary_barriers"}])
    assert not _boundary_barrier_table_ready(absent)
    assert _boundary_barrier_table_ready(present)
    _self_check_b5_prerequisites()
    print(json.dumps({"status": "PASS", "check": "scientific-driver-prerequisites"}))


def _self_check_b5_prerequisites() -> None:
    """Exercise strict B5 loading and the owned-VM disk guard with synthetic inputs only."""
    repo = _repo_root()
    local = repo / ".local"
    if not local.is_dir():
        raise AssertionError("local source-only test root is unavailable")
    expected_engine = "e3285329-4f64-4c8c-a566-40a098af03da"
    digest = "a" * 64
    with tempfile.TemporaryDirectory(prefix="scientific-preflight-", dir=local) as temp:
        root = Path(temp)
        private = root / "private"
        private.mkdir(mode=0o700)
        for name in ("database_url", "broker_capability_key", "master_key", "s3_access_key", "s3_secret_key"):
            (private / name).touch(mode=0o600)
        evidence = root / "evidence"
        evidence.mkdir(mode=0o700)
        bundle = root / "bundle"
        bundle.mkdir(mode=0o700)
        profiles = root / "profiles"
        profiles.mkdir(mode=0o700)
        dist = root / "web-dist"
        dist.mkdir(mode=0o700)
        (dist / "index.html").write_text("<!doctype html>\n")
        source_map = root / "source-hashes.json"
        source_map.write_text("{}\n")
        example = json.loads((Path(__file__).with_name("b5_live.example.json")).read_text())
        example.update({
            "expected_engine_id": expected_engine,
            "database_url": "postgresql+psycopg://postgres@127.0.0.1:54331/scientist_w1_20261006",
            "private_dir": str(private),
            "server_source_hashes": str(source_map),
            "worker_image": f"scientist-worker@sha256:{digest}",
            "server_image": f"scientist-server@sha256:{digest}",
            "postgres_image": f"scientist-postgres@sha256:{digest}",
            "minio_image": f"scientist-minio@sha256:{digest}",
            "fixture_images": {name: f"scientist-fixture-{name}@sha256:{digest}"
                               for name in ("happy", "counter", "barrier", "checkpoint_fault")},
        })
        config = root / "b5-live.json"
        config.write_text(json.dumps(example))
        extension = {
            "b5_live_config": str(config),
            "evidence_dir": str(evidence),
            "postgres_container_id": "1" * 64,
            "minio_container_id": "2" * 64,
            "scientific_bucket": "scientist-w1-20261006",
            "scientific_fixture_image": f"scientist-server-scientific@sha256:{digest}",
            "scientific_bundle_dir": str(bundle),
            "sealed_profile_evidence_dir": str(profiles),
            "web_dist_dir": str(dist),
        }
        extension_path = root / "scientific.json"
        extension_path.write_text(json.dumps(extension))
        old_config = os.environ.get("B5_LIVE_CONFIG")
        old_evidence = os.environ.get("B5_LIVE_EVIDENCE_DIR")
        old_extension = os.environ.get("SCIENTIFIC_W1_CONFIG")
        os.environ["SCIENTIFIC_W1_CONFIG"] = str(extension_path)

        original_read_text = Path.read_text
        original_read_bytes = Path.read_bytes
        original_open = Path.open
        def guarded_read_text(path: Path, *args, **kwargs):
            if path.resolve().is_relative_to(private):
                raise AssertionError("source-only preflight attempted to read a secret file")
            return original_read_text(path, *args, **kwargs)
        def guarded_read_bytes(path: Path, *args, **kwargs):
            if path.resolve().is_relative_to(private):
                raise AssertionError("source-only preflight attempted to read a secret file")
            return original_read_bytes(path, *args, **kwargs)
        def guarded_open(path: Path, *args, **kwargs):
            if path.resolve().is_relative_to(private):
                raise AssertionError("source-only preflight attempted to open a secret file")
            return original_open(path, *args, **kwargs)
        Path.read_text = guarded_read_text
        Path.read_bytes = guarded_read_bytes
        Path.open = guarded_open
        live_dir = str(Path(__file__).resolve().parent)
        sys.path.insert(0, live_dir)
        try:
            loaded_extension, _ = load_config()
            if loaded_extension["scientific_fixture_image"] != extension["scientific_fixture_image"]:
                raise AssertionError("scientific extension config did not validate its immutable fixture pin")
            import b5_matrix_common as common
            import b5_persistent_services as persistent
            if common.CFG.expected_engine_id != expected_engine:
                raise AssertionError("strict B5 loader rejected the authentic engine UUID")
            if common.CFG.worker_image != example["worker_image"] or common.CFG.server_image != example["server_image"]:
                raise AssertionError("strict B5 loader rejected immutable 64-hex image pins")
            if persistent.CTX != example["docker_context"]:
                raise AssertionError("owned VM guard did not use the configured Colima Docker context")
            calls = []
            def recorded_vm_docker(*args: str, **kwargs) -> str:
                calls.append(args)
                if "df" in args:
                    return "Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/vda 10000000 100 4194304 1% /"
                if "du" in args:
                    return "0 /v"
                raise AssertionError("unexpected source-only VM disk command")
            original_docker = persistent.docker
            persistent.docker = recorded_vm_docker
            try:
                common.guard_storage_headroom()
            finally:
                persistent.docker = original_docker
            if not any("df" in args for args in calls) or not any("du" in args for args in calls):
                raise AssertionError("owned VM free-space and volume probes were not exercised")
        finally:
            Path.read_text = original_read_text
            Path.read_bytes = original_read_bytes
            Path.open = original_open
            sys.path.remove(live_dir)
            sys.modules.pop("b5_matrix_common", None)
            sys.modules.pop("b5_persistent_services", None)
            if old_config is None:
                os.environ.pop("B5_LIVE_CONFIG", None)
            else:
                os.environ["B5_LIVE_CONFIG"] = old_config
            if old_evidence is None:
                os.environ.pop("B5_LIVE_EVIDENCE_DIR", None)
            else:
                os.environ["B5_LIVE_EVIDENCE_DIR"] = old_evidence
            if old_extension is None:
                os.environ.pop("SCIENTIFIC_W1_CONFIG", None)
            else:
                os.environ["SCIENTIFIC_W1_CONFIG"] = old_extension


def not_run(message: str) -> None:
    print(json.dumps({"status": "NOT RUN", "reason": message}))
    raise SystemExit(NOT_RUN)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _under_local(repo: Path, value: str) -> Path:
    path = Path(value)
    resolved = (repo / path).resolve() if not path.is_absolute() else path.resolve()
    if not resolved.is_relative_to(repo / ".local"):
        not_run("scientific config paths must stay under .local")
    return resolved


def _canonical_engine_id(value: object) -> str:
    if not isinstance(value, str):
        not_run("owned engine identity unavailable")
    try:
        parsed = UUID(value)
    except ValueError:
        not_run("owned engine identity is not a UUID")
    if str(parsed) != value.lower():
        not_run("owned engine identity is not canonical")
    return str(parsed)


def load_config() -> tuple[dict, Path]:
    repo = _repo_root()
    path = _under_local(repo, os.environ.get(
        "SCIENTIFIC_W1_CONFIG", ".local/scientific-w1-20261006/scientific.json"))
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        not_run("scientific extension config unavailable")
    if not isinstance(raw, dict) or set(raw) != EXTENSION_KEYS:
        not_run("scientific extension config keys differ from its example")

    core_path = _under_local(repo, raw["b5_live_config"])
    if not core_path.is_file():
        not_run("strict B5 core config unavailable")
    try:
        core = json.loads(core_path.read_text())
        core_keys = set(json.loads((Path(__file__).with_name("b5_live.example.json")).read_text()))
    except (OSError, ValueError):
        not_run("strict B5 core config is unreadable")
    if not isinstance(core, dict) or set(core) != core_keys:
        not_run("strict B5 core config keys differ from b5_live.example.json")
    for name in ("worker_image", "server_image", "postgres_image", "minio_image"):
        if not isinstance(core.get(name), str) or not IMAGE_REF.fullmatch(core[name]):
            not_run("B5 core image references must be immutable SHA-256 pins")
    fixtures = core.get("fixture_images")
    if not isinstance(fixtures, dict) or set(fixtures) != {"happy", "counter", "barrier", "checkpoint_fault"}:
        not_run("B5 core fixture schema differs from the strict loader")
    if any(not isinstance(ref, str) or not IMAGE_REF.fullmatch(ref) for ref in fixtures.values()):
        not_run("B5 core fixture references must be immutable SHA-256 pins")
    _canonical_engine_id(core.get("expected_engine_id"))
    for name in ("postgres_container_id", "minio_container_id"):
        identity = raw.get(name)
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            not_run("owned service container identity is invalid")

    fixture = raw.get("scientific_fixture_image")
    if not isinstance(fixture, str) or not IMAGE_REF.fullmatch(fixture):
        not_run("scientific fixture image must be a reviewed immutable SHA-256 pin")
    if raw.get("scientific_bucket") != "scientist-w1-20261006":
        not_run("scientific acceptance bucket is not isolated")
    if "scientist_w1_20261006" not in str(core.get("database_url", "")):
        not_run("strict B5 core config does not select the isolated W1 database")
    if not isinstance(core.get("database_url"), str) or urlsplit(core["database_url"]).password:
        not_run("strict B5 database URL must not contain a password")

    evidence = _under_local(repo, raw["evidence_dir"])
    bundle = _under_local(repo, raw["scientific_bundle_dir"])
    profiles = _under_local(repo, raw["sealed_profile_evidence_dir"])
    dist = (repo / raw["web_dist_dir"]).resolve()
    if not evidence.is_dir() or not bundle.is_dir() or not profiles.is_dir():
        not_run("scientific evidence, bundle, or sealed profile directory unavailable")
    if not dist.is_relative_to(repo) or not (dist / "index.html").is_file():
        not_run("built SPA directory unavailable")

    os.environ["B5_LIVE_CONFIG"] = str(core_path)
    os.environ["B5_LIVE_EVIDENCE_DIR"] = str(evidence)
    raw["scientific_fixture_image"] = fixture
    raw["scientific_bundle_dir"] = str(bundle)
    raw["sealed_profile_evidence_dir"] = str(profiles)
    raw["web_dist_dir"] = str(dist)
    raw["evidence_dir"] = str(evidence)
    raw["b5_live_config"] = str(core_path)
    return raw, evidence

def run() -> None:
    raw, evidence = load_config()
    # Must precede b5_matrix_common: it sets SCIENTIST_DATABASE_URL before importing scientist.db.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import b5_matrix_common as c
    import b5_host_http_acceptance as http
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    c.BUCKET = raw["scientific_bucket"]  # scientific extension namespace; CFG stays schema-exact
    from b5_persistent_services import SERVICES_NETWORK
    if os.environ.get("PGPASSFILE") is None:
        not_run("PGPASSFILE for the isolated database is not configured")
    host_db = make_url(c.CFG.database_url)
    if host_db.database != "scientist_w1_20261006":
        not_run("strict B5 core config does not select the isolated W1 database")
    engine_id = _canonical_engine_id(c.CFG.expected_engine_id)
    source_head = None

    h0 = c.H("scientific-w1")
    h0.eng = c.DockerWorkerEngine()
    h0.engine_id = h0.eng.engine_id()
    if h0.engine_id != engine_id:
        not_run("owned Docker engine identity differs")
    c.guard_storage_headroom()  # existing owned-Colima VM free-space and volume guard
    dirty_sources = subprocess.run(["git", "-C", str(c.ROOT), "status", "--porcelain", "--", "backend/src", "runtime"],
                                   capture_output=True, text=True, check=True).stdout.strip()
    if dirty_sources:
        not_run("backend/runtime source differs from the reviewed startup tree")
    source_map = json.loads(Path(c.CFG.server_source_hashes).read_text())
    for relative, expected in source_map.get("files", source_map).items():
        if relative.startswith(("backend/src/", "runtime/")):
            actual = hashlib.sha256((c.ROOT / relative).read_bytes()).hexdigest()
            if actual != expected:
                not_run("host source differs from the pinned server image source map")
    source_head = subprocess.run(["git", "-C", str(c.ROOT), "rev-parse", "HEAD"],
                                 capture_output=True, text=True, check=True).stdout.strip()
    service_ids = {}
    for name, expected in (("scientist-b5-postgres", raw["postgres_container_id"]),
                           ("scientist-b5-minio", raw["minio_container_id"])):
        actual = h0.docker("inspect", "--format", "{{.Id}}", name)
        if actual != expected or h0.docker("inspect", "--format", "{{.State.Running}}", name) != "true":
            not_run(f"expected existing {name} service is unavailable")
        service_ids[name] = actual
    service_network_id = h0.docker("network", "inspect", SERVICES_NETWORK, "--format", "{{.Id}}").strip()
    if not service_network_id:
        not_run("expected existing service network is unavailable")
    fixture_image = raw["scientific_fixture_image"]
    h0.setup(fixture_image)
    http.forbid_supervisor()
    provider_id, request_id = uuid4(), uuid4()
    root = evidence / "scientific-host"
    root.mkdir(mode=0o700, exist_ok=True)
    host = http.HostProc("scientific-host", root, provider_id, fixture_image, 0.5)
    host.cfg.update({"service_network": SERVICES_NETWORK, "scientific_bundle_dir": raw["scientific_bundle_dir"],
                     "web_dist_dir": raw["web_dist_dir"]})
    if os.environ.get("PGPASSFILE"):
        host.env["PGPASSFILE"] = os.environ["PGPASSFILE"]
    profiles = host.state / "profiles"
    shutil.copytree(raw["sealed_profile_evidence_dir"], profiles)
    api = None
    h = None
    host2 = None
    run_id = None
    phase = "host_start"
    try:
        api = host.start()
        project = http.call(api, "POST", "/api/v1/projects", json={"name": "Scientific W1 acceptance", "instructions": ""})
        project_id = UUID(project["id"])
        session = http.call(api, "POST", f"/api/v1/projects/{project_id}/sessions", json={"title": "Resource measurement"})
        session_id = UUID(session["id"])
        connection = http.call(api, "POST", "/api/v1/connections", json={"provider_id": str(provider_id),
            "label": "Synthetic W1 provider", "model": "fixture", "secret": "synthetic-only-no-provider-access"}, ok=(201,))
        if UUID(connection["id"]) != provider_id or connection["state"] != "ready":
            raise RuntimeError("synthetic provider connection was not recorded as configured")

        phase = "research_setup_and_preparation"
        setup = http.call(api, "GET", f"/api/v1/projects/{project_id}/research-setup")
        profile = next(item for item in setup["profiles"] if item["profile_id"] == PROFILE)
        if profile["version"] != "1" or profile["manifest_sha256"] != MANIFEST:
            raise RuntimeError("research setup returned an unexpected reviewed profile")
        body = {"profile_id": PROFILE, "version": "1", "manifest_sha256": MANIFEST, "request_id": str(request_id)}
        path = f"/api/v1/projects/{project_id}/preparations"
        first = http.call(api, "POST", path, json=body)
        def retry(_):
            with httpx_client(api) as client:
                return http.call(client, "POST", path, json=body)
        with ThreadPoolExecutor(max_workers=2) as pool:
            concurrent = list(pool.map(retry, range(2)))
        if any(item["id"] != first["id"] for item in concurrent) or http.call(api, "POST", path, json=body)["id"] != first["id"]:
            raise RuntimeError("preparation retry did not return the same job")
        changed = {**body, "version": "0"}
        if api.post(path, json=changed).status_code != 409:
            raise RuntimeError("changed preparation request did not conflict")
        other = http.call(api, "POST", "/api/v1/projects", json={"name": "Scientific W1 idempotency scope", "instructions": ""})
        if api.post(f"/api/v1/projects/{other['id']}/preparations", json=body).status_code != 409:
            raise RuntimeError("preparation key was not bound to its project")
        job_path = f"/api/v1/projects/{project_id}/preparations/{first['id']}"
        job = http.wait_for(lambda: (lambda value: value if value["state"] in {"ready", "failed", "blocked"} else None)
                            (http.call(api, "GET", job_path)), "profile preparation did not finish", 900, 1)
        if job["state"] != "ready" or not job["evidence_verified"]:
            raise RuntimeError(f"profile preparation ended {job['state']}")

        phase = "run_plan_approval"
        created = http.call(api, "POST", f"/api/v1/sessions/{session_id}/runs", json={
            "submission_key": uuid4().hex, "question": "Measure the approved synthetic worker resource limits.",
            "input_ids": [], "provider_id": str(provider_id), "model": "fixture"})
        run_id = UUID(created["run_id"])
        h = c.H("scientific-w1-run").attach(run_id)
        h.eng, h.engine_id = h0.eng, h0.engine_id
        plan0 = http.call(api, "GET", f"/api/v1/runs/{run_id}/plan")
        revised = http.call(api, "POST", f"/api/v1/runs/{run_id}/prepare-plan", json={
            "expected_revision": plan0["revision"], "workflow": "resources", "search_terms": []})
        plan = http.call(api, "GET", f"/api/v1/runs/{run_id}/plan")
        if (plan["revision"] != revised["revision"]
                or plan["plan"]["scientific"]["capability_ids"] != ["get-available-resources"]):
            raise RuntimeError("resource plan does not carry the expected scientific binding")
        budgeted = http.call(api, "PATCH", f"/api/v1/runs/{run_id}/plan", json={
            "expected_revision": plan["revision"],
            "plan": {**plan["plan"], "token_limit": 20_000, "elapsed_limit_ms": 600_000},
        })
        plan = http.call(api, "GET", f"/api/v1/runs/{run_id}/plan")
        readiness = http.call(api, "GET", f"/api/v1/runs/{run_id}/readiness")
        current = http.call(api, "GET", f"/api/v1/runs/{run_id}")
        if (current["revision"] != plan["revision"] or plan["revision"] != budgeted["revision"]
                or plan["revision"] <= revised["revision"]
                or plan["plan"]["token_limit"] != 20_000
                or plan["plan"]["elapsed_limit_ms"] != 600_000
                or plan["plan"]["scientific"]["capability_ids"] != ["get-available-resources"]):
            raise RuntimeError("budgeted resource plan is not the current scientific plan")
        if (readiness["state"] != "ready" or readiness["binding_sha256"] is None
                or readiness["revision"] != plan["revision"]
                or readiness["plan_digest"] != plan["plan_digest"]):
            raise RuntimeError("budgeted resource plan readiness is not current and profile-backed")
        with c.session() as db:
            db.execute(text("""CREATE TABLE IF NOT EXISTS w1_boundary_fault_targets (
                run_id uuid PRIMARY KEY, armed_at timestamptz NOT NULL DEFAULT now())"""))
            db.execute(text("INSERT INTO w1_boundary_fault_targets(run_id) VALUES (:run) ON CONFLICT DO NOTHING"),
                       {"run": run_id})
            db.commit()
        http.call(api, "POST", f"/api/v1/runs/{run_id}/approve", json={
            "expected_revision": plan["revision"], "plan_digest": plan["plan_digest"]})

        phase = "committed_receipt_lost_ack_restart"
        http.wait_for(lambda: "ready" if _boundary_barrier_table_ready(h) else None,
                      "scientific fixture boundary table did not initialize", 30, 0.2)
        barrier = http.wait_for(lambda: h.q("SELECT generation,released FROM w1_boundary_barriers WHERE run_id=:run"),
                                "committed scientific receipt did not reach the boundary barrier", 240, 0.2)
        if len(barrier) != 1 or barrier[0]["generation"] != 1 or barrier[0]["released"]:
            raise RuntimeError("boundary barrier evidence is not the expected unreleased generation")
        before = counts(h)
        if before["receipts"] != 1 or before["boundaries"].get("tool_committed") != 1:
            raise RuntimeError("barrier was reached without one durable scientific receipt")
        tool_checkpoint = before["tool_checkpoints"][0]
        ack_before = h.q("""SELECT boundary_id,generation,checkpoint_id,payload_hash,ack FROM checkpoint_boundaries
            WHERE run_id=:run AND checkpoint_id=:checkpoint""", checkpoint=UUID(tool_checkpoint))
        if len(ack_before) != 1:
            raise RuntimeError("committed tool boundary does not have one durable ACK")
        host.proc.kill()
        if host.proc.wait(10) != -9:
            raise RuntimeError("first host process was not terminated with the ACK held")
        host2 = http.HostProc("scientific-host-restart", root, provider_id, fixture_image, 0.5)
        host2.cfg.update({"service_network": SERVICES_NETWORK, "scientific_bundle_dir": raw["scientific_bundle_dir"],
                          "web_dist_dir": raw["web_dist_dir"], "state_dir": str(host.state)})
        host2.state = host.state
        if os.environ.get("PGPASSFILE"):
            host2.env["PGPASSFILE"] = os.environ["PGPASSFILE"]
        api.close()
        api = host2.start()
        fenced = http.wait_for(lambda: (lambda rows: rows if rows and
            all(row["state"] == "inactive" for row in rows if row["generation"] == 1) and
            any(row["generation"] >= 2 for row in rows) else None)(h.executors()),
            "restarted host did not fence the old executor before ACK release", 240, 0.2)
        if not any(row["generation"] == 1 and row["state"] == "inactive" for row in fenced):
            raise RuntimeError("old executor exact quiescence was not established")
        with c.session() as db:
            changed = db.execute(c.text("UPDATE w1_boundary_barriers SET released=true WHERE run_id=:run AND generation=1"),
                                 {"run": run_id}).rowcount
            db.commit()
        if changed != 1:
            raise RuntimeError("exact persisted boundary barrier release failed")
        result = http.wait_for(lambda: (lambda value: value if value["state"] in c.TERMINAL else None)
                               (http.call(api, "GET", f"/api/v1/runs/{run_id}")), "scientific run did not finish", 300, 0.5)
        if result["state"] != "completed":
            raise RuntimeError(f"recovered scientific run ended {result['state']}")
        after = counts(h)
        if after["receipts"] != 1 or after["artifacts"] != 1 or after["boundaries"].get("tool_committed") != 1:
            raise RuntimeError("recovery duplicated or lost the scientific receipt/artifact")
        if after["boundaries"].get("before_tool", 0) != before["boundaries"].get("before_tool", 0):
            raise RuntimeError("recovery recomputed the scientific tool or repeated a provider stage")
        if before["attempts"] != {"batch": 1} or after["attempts"] != {"batch": 1, "final": 1}:
            raise RuntimeError("synthetic provider transcript stages were repeated or skipped")
        ack_after = h.q("""SELECT boundary_id,generation,checkpoint_id,payload_hash,ack FROM checkpoint_boundaries
            WHERE run_id=:run AND checkpoint_id=:checkpoint""", checkpoint=UUID(tool_checkpoint))
        barrier_after = h.q("SELECT generation,released FROM w1_boundary_barriers WHERE run_id=:run")
        if (len(ack_after) != 1 or ack_after[0] != ack_before[0] or len(barrier_after) != 1
                or barrier_after[0]["generation"] != 1 or not barrier_after[0]["released"]):
            raise RuntimeError("lost ACK recovery changed its durable boundary or reactivated the barrier")
        ops = h.ops()
        if len(ops) != 2 or any(item["state"] != "committed" for item in ops):
            raise RuntimeError("recovery did not retain exactly the two committed model operations")
        if h.run_row()["generation"] != 2 or h.event_count("run.state", "completed") != 1:
            raise RuntimeError("recovery generation or terminal event is not exact")
        artifacts = http.call(api, "GET", f"/api/v1/runs/{run_id}")["artifacts"]
        artifact = next((item for item in artifacts if item["partial"] is False), None)
        if artifact is None:
            raise RuntimeError("completed run has no final scientific artifact")
        content = api.get(f"/api/v1/artifacts/{artifact['artifact_id']}/content")
        if content.status_code != 200 or hashlib.sha256(content.content).hexdigest() != artifact["sha256"]:
            raise RuntimeError("authenticated artifact download differs from the persisted artifact hash")
        object_row = h.q("SELECT key,sha256,size FROM stored_objects WHERE project_id=:p AND sha256=:sha",
                         p=project_id, sha=artifact["sha256"])
        if len(object_row) != 1 or object_row[0]["size"] != len(content.content):
            raise RuntimeError("scientific artifact object-store metadata differs from REST bytes")
        stored = h.s3.get_object(Bucket=c.BUCKET, Key=object_row[0]["key"])["Body"].read()
        if stored != content.content:
            raise RuntimeError("scientific artifact object-store bytes differ from REST download")
        receipt = h.q("SELECT artifact_id,receipt_sha256 FROM scientific_artifact_receipts WHERE run_id=:run")
        if (len(receipt) != 1 or str(receipt[0]["artifact_id"]) != artifact["artifact_id"]
                or len(receipt[0]["receipt_sha256"].strip()) != 64):
            raise RuntimeError("final artifact does not match the single committed scientific receipt")
        api.close()
        api = None
        host2_exit = host2.stop()
        if host2_exit not in (0, -15):
            raise RuntimeError("restarted host did not stop cleanly")
        host_exit = host.stop()
        host_logs = {"first": host.scan_logs(), "restarted": host2.scan_logs()}
        if any(item["known_secret_in_logs"] or item["tokenish_strings"] for item in host_logs.values()):
            raise RuntimeError("host logs contain secret-like material")
        h.exact_cleanup()

        proof = {
            "status": "PASS",
            "application_source": source_head,
            "current_source_head": source_head,
            "server_source_map_sha256": hashlib.sha256(Path(c.CFG.server_source_hashes).read_bytes()).hexdigest(),
            "worker_image": c.CFG.worker_image,
            "server_image": c.CFG.server_image,
            "fixture_image": fixture_image,
            "engine_id": h0.engine_id,
            "service_container_ids": service_ids,
            "service_network_id": service_network_id,
            "profile_id": PROFILE,
            "profile_version": "1",
            "profile_manifest_sha256": MANIFEST,
            "project_id": str(project_id),
            "session_id": str(session_id),
            "run_id": str(run_id),
            "preparation_id": first["id"],
            "artifact_id": artifact["artifact_id"],
            "artifact_sha256": artifact["sha256"],
            "artifact_size": len(content.content),
            "fixture_counts": after,
            "final_state": result["state"],
            "host_log_scan": {key: {"log_files": value["log_files"], "clean": True}
                               for key, value in host_logs.items()},
            "browser_host_config": str(host2.cfg_path),
            "browser_host_state": str(host2.state),
            "browser_bootstrap_url_file": str(host2.state / "owner-bootstrap.url"),
            "browser_origin": host2.base,
            "browser_web_dist_dir": raw["web_dist_dir"],
            "browser_host_launch": [sys.executable, str(Path(__file__).with_name("b5_host_http_acceptance.py")),
                                    "--launch", str(host2.cfg_path)],
        }
        proof_path = evidence / "scientific_host_http_proof.json"
        proof_path.write_text(json.dumps(proof, sort_keys=True) + "\n")
        proof_path.chmod(0o600)
        print(json.dumps({"status": "PASS", "phase": "scientific-host-http", "run_id": str(run_id),
                          "artifact_sha256": artifact["sha256"], "artifact_size": len(content.content),
                          "host_logs_clean": True}))
    except BaseException as exc:
        try:
            if api is not None and run_id is not None:
                api.post(f"/api/v1/runs/{run_id}/stop")
            if api is not None:
                api.close()
            (host2 or host).stop()
        except Exception:
            pass
        print(json.dumps({"status": "FAIL", "phase": phase, "error_type": type(exc).__name__}))
        raise SystemExit(1) from None


def verify_browser_run(run_id: str) -> None:
    raw, _ = load_config()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import b5_matrix_common as c
    from uuid import UUID

    c.BUCKET = raw["scientific_bucket"]
    evidence_path = Path(os.environ.get("SCIENTIFIC_W1_BROWSER_EVIDENCE", ""))
    try:
        browser = json.loads(evidence_path.read_text())
        run_uuid = UUID(run_id)
    except (OSError, ValueError):
        not_run("browser artifact evidence is unavailable")
    if browser.get("run_id") != str(run_uuid) or browser.get("status") != "PASS":
        raise SystemExit("FAIL: browser evidence identity mismatch")
    h = c.H("scientific-w1-browser-proof").attach(run_uuid)
    row = h.q("""SELECT r.state,a.id,a.project_id,a.object_key,a.sha256,a.size,a.partial
        FROM runs r JOIN artifacts a ON a.run_id=r.id WHERE r.id=:run AND a.id=:artifact""",
        artifact=UUID(browser["artifact_id"]))
    receipts = h.q("SELECT artifact_id FROM scientific_artifact_receipts WHERE run_id=:run")
    objects = h.q("SELECT sha256,size FROM stored_objects WHERE key=:key", key=row[0]["object_key"]) if row else []
    if (len(row) != 1 or row[0]["state"] != "completed" or row[0]["partial"] is not False or len(receipts) != 1
            or str(receipts[0]["artifact_id"]) != browser["artifact_id"] or len(objects) != 1
            or row[0]["sha256"].strip() != browser["artifact_sha256"] or row[0]["size"] != browser["artifact_size"]
            or objects[0]["sha256"].strip() != row[0]["sha256"].strip() or objects[0]["size"] != row[0]["size"]):
        raise SystemExit("FAIL: browser artifact differs from final DB receipt/object metadata")
    data = h.s3.get_object(Bucket=c.BUCKET, Key=row[0]["object_key"])["Body"].read(row[0]["size"] + 1)
    if len(data) != row[0]["size"] or hashlib.sha256(data).hexdigest() != browser["artifact_sha256"]:
        raise SystemExit("FAIL: browser download differs from stored object bytes")
    print(json.dumps({"status": "PASS", "phase": "browser-db-s3-readback", "run_id": str(run_uuid),
                      "artifact_id": browser["artifact_id"], "artifact_sha256": browser["artifact_sha256"],
                      "artifact_size": len(data)}))


def httpx_client(api):
    import httpx
    return httpx.Client(base_url=api.base_url, headers=dict(api.headers), cookies=api.cookies, timeout=30)


def _boundary_barrier_table_ready(h) -> bool:
    rows = h.q("SELECT to_regclass('w1_boundary_barriers') AS relation")
    return bool(rows and rows[0]["relation"])


def counts(h) -> dict:
    scalar = lambda query: h.q(query)[0]["n"]
    attempt_rows = h.q("SELECT stage,count(*) AS n FROM w1_fixture_attempts WHERE run_id=:run GROUP BY stage")
    boundaries = {}
    tool_checkpoints = []
    verified = 0
    from scientist.contracts import ObjectRef
    from scientist import objects
    for row in h.q("SELECT id,manifest FROM checkpoints WHERE run_id=:run"):
        if h.verify_checkpoint(row["manifest"]) != "verified":
            raise RuntimeError("checkpoint objects did not verify from the object store")
        verified += 1
        ref = ObjectRef.model_validate(row["manifest"]["context"])
        with objects.open_verified(ref) as source:
            context = json.loads(source.read(1_048_577))
        boundaries[context["boundary"]] = boundaries.get(context["boundary"], 0) + 1
        if context["boundary"] == "tool_committed":
            tool_checkpoints.append(str(row["id"]))
    return {"receipts": scalar("SELECT count(*) AS n FROM scientific_artifact_receipts WHERE run_id=:run"),
            "artifacts": scalar("SELECT count(*) AS n FROM artifacts WHERE run_id=:run"),
            "boundaries": boundaries,
            "tool_checkpoints": tool_checkpoints,
            "verified_checkpoints": verified,
            "attempts": {row["stage"]: row["n"] for row in attempt_rows},
            "attempt_total": sum(row["n"] for row in attempt_rows)}


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-check"]:
        self_check()
    elif len(sys.argv) == 3 and sys.argv[1] == "--verify-browser-run":
        verify_browser_run(sys.argv[2])
    else:
        run()
