#!/usr/bin/env python3
"""Run one owner-approved W2 Crossref + CSV workflow through the real local host."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

NOT_RUN = 77
ROOT = Path(__file__).resolve().parents[3]
W2_ROOT = ROOT / ".local" / "w2-scientific-20261007"
W2_DB = "scientist_w2_20261007"
W2_BUCKET = "scientist-w2-20261007"
W2_EGRESS_NETWORK = "scientist-b5-egress-test"
SCHOLARLY_ENDPOINTS = "https://api.crossref.org"
IMAGE_REF = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_OUTPUTS = ("summary.json", "summary.csv", "chart.svg", "report.md")
CONFIG_KEYS = {
    "b5_live_config", "evidence_dir", "postgres_container_id", "minio_container_id",
    "egress_network_id", "scientific_bucket", "scientific_fixture_image", "scientific_bundle_dir",
    "sealed_profile_evidence_dir", "sealed_compute_profile_evidence_dir", "compute_evidence",
    "web_dist_dir", "compute_image", "compute_recipe_manifest_sha256",
}


class NotRunError(RuntimeError):
    pass


def _not_run(reason: str) -> None:
    print(json.dumps({"status": "NOT RUN", "reason": reason}))
    raise SystemExit(NOT_RUN)


def _repo_path(value: object, *, under: Path | None = None) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise NotRunError("W2 paths must be nonempty repository-relative paths")
    path = (ROOT / value).resolve()
    if not path.is_relative_to(ROOT) or (under is not None and not path.is_relative_to(under.resolve())):
        raise NotRunError("W2 path escapes its approved local namespace")
    return path


def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NotRunError(f"{label} is unavailable or malformed") from exc
    if not isinstance(value, dict):
        raise NotRunError(f"{label} must be a JSON object")
    return value


def load_config(*, require_ready: bool = True) -> tuple[dict, dict, Path]:
    example = _read_json(Path(__file__).with_name("w2_scientific.example.json"), "W2 example config")
    raw_path = os.environ.get("SCIENTIFIC_W2_CONFIG", ".local/w2-scientific-20261007/w2-scientific.json")
    config_path = _repo_path(raw_path, under=W2_ROOT)
    raw = _read_json(config_path, "W2 config")
    if set(raw) != CONFIG_KEYS:
        raise NotRunError("W2 config keys differ from the closed example schema")
    if set(example) != CONFIG_KEYS:
        raise NotRunError("W2 example keys differ from the closed runner schema")

    for key in ("postgres_container_id", "minio_container_id", "egress_network_id"):
        if not isinstance(raw[key], str) or not HEX64.fullmatch(raw[key]):
            raise NotRunError(f"{key} must be an owned 64-hex identity")
    if raw["scientific_bucket"] != W2_BUCKET:
        raise NotRunError("scientific bucket is outside the W2 namespace")
    for key in ("scientific_fixture_image", "compute_image"):
        if not isinstance(raw[key], str) or not IMAGE_REF.fullmatch(raw[key]):
            raise NotRunError(f"{key} must be an immutable image reference")
    if not isinstance(raw["compute_recipe_manifest_sha256"], str) or not HEX64.fullmatch(raw["compute_recipe_manifest_sha256"]):
        raise NotRunError("compute recipe manifest must be a SHA-256 digest")

    paths = {
        key: _repo_path(raw[key], under=W2_ROOT)
        for key in (
            "b5_live_config", "evidence_dir", "scientific_bundle_dir",
            "sealed_profile_evidence_dir", "sealed_compute_profile_evidence_dir",
        )
    }
    compute_evidence = _repo_path(raw["compute_evidence"], under=ROOT / ".local" / "w2-compute-profile-20261007")
    if compute_evidence != ROOT / ".local" / "w2-compute-profile-20261007" / "accepted":
        raise NotRunError("compute evidence must be the accepted W2 receipt directory")
    paths["compute_evidence"] = compute_evidence
    web_dist = _repo_path(raw["web_dist_dir"])
    if web_dist != ROOT / "apps" / "web" / "dist":
        raise NotRunError("W2 web distribution must be the reviewed apps/web/dist tree")
    paths["web_dist_dir"] = web_dist

    core = _read_json(paths["b5_live_config"], "W2 B5 host config")
    core_example = _read_json(Path(__file__).with_name("b5_live.example.json"), "B5 example config")
    if set(core) != set(core_example):
        raise NotRunError("B5 core config differs from its strict schema")
    try:
        database_url = urlsplit(core["database_url"])
    except (TypeError, ValueError) as exc:
        raise NotRunError("W2 database URL is invalid") from exc
    if database_url.password is not None or database_url.path.lstrip("/") != W2_DB:
        raise NotRunError("B5 core must bind the passwordless W2 database")
    private_dir = _repo_path(core["private_dir"], under=W2_ROOT)
    if private_dir != W2_ROOT / "private":
        raise NotRunError("B5 private path must use the dedicated W2 namespace")

    if require_ready:
        for key, path in paths.items():
            if key == "b5_live_config":
                if not path.is_file():
                    raise NotRunError("W2 B5 configuration file is not prepared")
            elif key == "evidence_dir":
                if not path.is_dir():
                    raise NotRunError("W2 evidence directory is not prepared")
            elif key == "web_dist_dir":
                if not (path / "index.html").is_file():
                    raise NotRunError("reviewed W2 web distribution is unavailable")
            elif not path.is_dir():
                raise NotRunError(f"W2 {key} directory is not prepared")
    return raw, core, paths["b5_live_config"]


def _load_fixture_module():
    import importlib.util
    import types

    path = Path(__file__).with_name("fixtures") / "scientific" / "sitecustomize.py"
    spec = importlib.util.spec_from_file_location("w2_scientific_acceptance_fixture", path)
    if spec is None or spec.loader is None:
        raise AssertionError("scientific fixture source is unavailable")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get("uvicorn")
    sys.modules["uvicorn"] = types.SimpleNamespace(run=lambda *args, **kwargs: None)
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop("uvicorn", None)
        else:
            sys.modules["uvicorn"] = previous
    return module


def self_check() -> None:
    import csv
    from types import SimpleNamespace

    fixture_path = Path(__file__).with_name("fixtures") / "scientific" / "w2_partial.csv"
    with fixture_path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream, strict=True))
    if len(rows) != 4 or list(rows[0]) != ["site", "temperature_c", "nitrate_mg_l"]:
        raise AssertionError("W2 CSV fixture header or row count changed")
    if rows != [
        {"site": "A", "temperature_c": "18", "nitrate_mg_l": "2"},
        {"site": "B", "temperature_c": "20", "nitrate_mg_l": ""},
        {"site": "C", "temperature_c": "22", "nitrate_mg_l": "4"},
        {"site": "D", "temperature_c": "", "nitrate_mg_l": "6"},
    ]:
        raise AssertionError("W2 CSV fixture values changed")
    if EXPECTED_OUTPUTS != ("summary.json", "summary.csv", "chart.svg", "report.md"):
        raise AssertionError("W2 output order changed")
    example = _read_json(Path(__file__).with_name("w2_scientific.example.json"), "W2 example config")
    if set(example) != CONFIG_KEYS:
        raise AssertionError("W2 example config is not a closed schema")

    fixture = _load_fixture_module()
    from uuid import UUID

    run_id = UUID("c5f6b6a7-7f53-4a40-9c38-1151bc6eb7d8")
    request = lambda messages: SimpleNamespace(
        kind="llm", run_id=run_id, operation_id="self-check",
        payload={"model": "fixture", "messages": messages, "tools": _self_check_tools()},
    )
    target = SimpleNamespace(url="https://research.example")
    stage, instruction, finish = fixture._w2_stage(request([]), target)
    if stage != "instruction" or finish != "tool_calls":
        raise AssertionError("W2 fixture did not request the selected EDA instruction first")
    instruction_call = instruction["tool_calls"][0]
    if instruction_call["function"]["arguments"] != '{"capability_id":"exploratory-data-analysis"}':
        raise AssertionError("W2 fixture did not select the approved EDA instruction")
    history = [
        {"role": "assistant", "tool_calls": [instruction_call]},
        {"role": "tool", "tool_call_id": instruction_call["id"], "content": "EDA instruction " * 24},
    ]
    stages = [stage]
    for expected in ("search", "compute", "final"):
        stage, message, finish = fixture._w2_stage(request(history), target)
        if stage != expected:
            raise AssertionError("W2 fixture native bridge order changed")
        stages.append(stage)
        calls = message.get("tool_calls", [])
        if calls:
            call = calls[0]
            history.extend([
                {"role": "assistant", "tool_calls": [call]},
                {"role": "tool", "tool_call_id": call["id"], "content": "synthetic accepted result"},
            ])
        elif finish != "stop":
            raise AssertionError("W2 final fixture response did not stop")
    if stages != ["instruction", "search", "compute", "final"]:
        raise AssertionError("W2 fixture transcript is not complete")
    print(json.dumps({"status": "PASS", "check": "w2-scientific-source", "stages": stages,
                      "csv_rows": len(rows), "compute_outputs": list(EXPECTED_OUTPUTS)}))


def _self_check_tools() -> list[dict]:
    schemas = {
        "instruction_view": ("capability_id", ["paper-lookup", "exploratory-data-analysis"]),
        "scientific_search": ("request_id", ["crossref"]),
        "scientific_csv_describe": ("grant_id", ["csv_describe"]),
    }
    return [{"type": "function", "function": {
        "name": name,
        "parameters": {
            "type": "object", "additionalProperties": False, "required": [arg],
            "properties": {arg: {"type": "string", "enum": identifier}},
        },
    }} for name, (arg, identifier) in schemas.items()]


def _set_up_process_environment(config_path: Path, evidence_dir: Path) -> None:
    os.environ["B5_LIVE_CONFIG"] = str(config_path)
    os.environ["B5_LIVE_EVIDENCE_DIR"] = str(evidence_dir)
    os.environ["SCIENTIST_SCHOLARLY_ENDPOINTS"] = SCHOLARLY_ENDPOINTS


def write_browser_session(api, host, destination: Path) -> Path:
    """Transfer the already bootstrapped test owner session without reusing its token."""
    cookie = api.cookies.get("owner_session")
    if not cookie or not host.base.startswith("http://127.0.0.1:"):
        raise RuntimeError("W2 authenticated local owner session unavailable")
    destination.write_text(json.dumps([{
        "name": "owner_session", "value": cookie, "url": host.base,
        "httpOnly": True, "secure": False, "sameSite": "Strict",
    }]) + "\n")
    destination.chmod(0o600)
    return destination


def configure_host_database(host) -> None:
    passfile = os.environ.get("PGPASSFILE")
    if not passfile:
        raise RuntimeError("W2 PostgreSQL passfile not configured")
    host.env["PGPASSFILE"] = passfile


def engine_probe(common):
    probe = common.H("w2-engine-probe")
    probe.eng = common.DockerWorkerEngine()
    probe.engine_id = probe.eng.engine_id()
    return probe


def run() -> None:
    try:
        raw, _core, config_path = load_config()
    except NotRunError as exc:
        _not_run(str(exc))
    evidence_dir = _repo_path(raw["evidence_dir"], under=W2_ROOT)
    _set_up_process_environment(config_path, evidence_dir)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import b5_matrix_common as c
        import b5_host_http_acceptance as http
        from sqlalchemy.engine import make_url
    except SystemExit:
        raise
    except Exception as exc:
        _not_run(f"W2 B5 host dependencies unavailable: {type(exc).__name__}")

    c.BUCKET = W2_BUCKET
    if c.PRIVATE.resolve() != W2_ROOT / "private" or make_url(c.DB_URL).database != W2_DB:
        _not_run("loaded B5 runtime authority is not bound to the W2 namespace")
    if os.environ.get("PGPASSFILE") is None:
        _not_run("W2 PostgreSQL passfile is not configured")
    engine = engine_probe(c)
    try:
        if engine.engine_id != c.EXPECTED_ENGINE_ID:
            _not_run("owned Docker engine identity differs")
        c.guard_storage_headroom()
        dirty_sources = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--", "backend/src", "runtime"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if dirty_sources:
            _not_run("backend/runtime source differs from the reviewed immutable-image source tree")
        source_map = _read_json(c.CFG.server_source_hashes, "server source hash map")
        for relative, expected in source_map.get("files", source_map).items():
            if relative.startswith(("backend/src/", "runtime/")):
                actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
                if actual != expected:
                    _not_run("server source differs from its immutable-image source hash map")
        expected_services = {
            "scientist-b5-postgres": raw["postgres_container_id"],
            "scientist-b5-minio": raw["minio_container_id"],
        }
        for name, expected in expected_services.items():
            actual = engine.docker("inspect", "--format", "{{.Id}}", name).strip()
            running = engine.docker("inspect", "--format", "{{.State.Running}}", name).strip()
            if actual != expected or running != "true":
                _not_run(f"owned W2 service unavailable: {name}")
        network = engine.docker("network", "inspect", "--format",
                                '{{.Id}}|{{index .Labels "scientist.platform/egress"}}|{{.Internal}}',
                                W2_EGRESS_NETWORK).strip()
        if network != f"{raw['egress_network_id']}|b5|false":
            _not_run("owned W2 Crossref egress network identity or policy differs")
        for image in (raw["scientific_fixture_image"], raw["compute_image"],
                      c.CFG.worker_image, c.CFG.server_image, c.CFG.postgres_image, c.CFG.minio_image):
            image_id = engine.docker("image", "inspect", "--format", "{{.Id}}", image).strip()
            if image_id != image.split("@", 1)[1]:
                _not_run("required immutable image is not present locally")
    except SystemExit:
        raise
    except Exception as exc:
        _not_run(f"W2 owned engine preflight unavailable: {type(exc).__name__}")

    # B5's matrix helper defaults to W1 namespaces. Keep its transport and DB
    # helpers, but bind every W2-specific authority explicitly here.
    http.forbid_supervisor()
    provider_id = uuid4()
    root = evidence_dir / "w2-scientific-host"
    root.mkdir(mode=0o700, exist_ok=False)
    host = http.HostProc("w2-scientific", root, provider_id, raw["scientific_fixture_image"], 0.5)
    host.cfg.update({
        "service_network": "scientist-b5-services-test",
        "bucket": W2_BUCKET,
        "compute_image": raw["compute_image"],
        "egress_network": W2_EGRESS_NETWORK,
        "scientific_bundle_dir": str(_repo_path(raw["scientific_bundle_dir"], under=W2_ROOT)),
        "state_dir": str(host.state),
        "web_dist_dir": str(_repo_path(raw["web_dist_dir"])),
    })
    host.env["SCIENTIST_SCHOLARLY_ENDPOINTS"] = SCHOLARLY_ENDPOINTS
    configure_host_database(host)
    host.cfg_path.write_text(json.dumps(host.cfg, sort_keys=True))
    host.cfg_path.chmod(0o600)
    shutil.copytree(raw["sealed_profile_evidence_dir"], host.state / "profiles")
    shutil.copytree(raw["sealed_compute_profile_evidence_dir"], host.state / "compute-profiles")
    api = None
    browser_status = None
    phase = "host_start"
    try:
        api = host.start()
        project = http.call(api, "POST", "/api/v1/projects", json={
            "name": "W2 scientific acceptance", "instructions": "",
        })
        session = http.call(api, "POST", f"/api/v1/projects/{project['id']}/sessions", json={
            "title": "Crossref and CSV analysis",
        })
        connection = http.call(api, "POST", "/api/v1/connections", json={
            "provider_id": str(provider_id), "label": "Synthetic W2 provider",
            "model": "fixture", "secret": "synthetic-only-no-provider-access",
        }, ok=(201,))
        if UUID(connection["id"]) != provider_id or connection["state"] != "ready":
            raise RuntimeError("synthetic provider connection was not configured")
        proof_file = root / "w2-browser-proof.json"
        proof_file.write_text(json.dumps({
            "project_id": project["id"], "session_id": session["id"],
            "compute_image_digest": raw["compute_image"].split("@", 1)[1],
            "compute_recipe_manifest_sha256": raw["compute_recipe_manifest_sha256"],
            "source_hashes": {"csv_fixture_sha256": hashlib.sha256(
                (Path(__file__).with_name("fixtures") / "scientific" / "w2_partial.csv").read_bytes()
            ).hexdigest()},
        }, sort_keys=True) + "\n")
        proof_file.chmod(0o600)
        phase = "browser_acceptance"
        browser_session = write_browser_session(api, host, host.state / "browser-owner-session.json")
        browser_env = os.environ.copy()
        browser_env.update({
            "SCIENTIFIC_W2_OWNER_SESSION_FILE": str(browser_session),
            "SCIENTIFIC_W2_PROOF": str(proof_file),
            "SCIENTIFIC_W2_BROWSER_EVIDENCE": str(evidence_dir / "w2-browser-evidence.json"),
            "SCIENTIFIC_W2_API_ORIGIN": host.base,
            "SCIENTIFIC_W2_PYTHON": sys.executable,
        "SCIENTIFIC_W2_CSV_FIXTURE": str(Path(__file__).with_name("fixtures") / "scientific" / "w2_partial.csv"),
            "CI": "1",
        })
        web = ROOT / "apps" / "web"
        browser = subprocess.run(
            ["npm", "run", "test:e2e", "--", "--grep", "W2 real scientific workflow"],
            cwd=web, env=browser_env, capture_output=True, text=True, timeout=660, check=False,
        )
        browser_status = browser.returncode
        if browser_status != 0:
            raise RuntimeError("W2 Playwright acceptance did not pass")
        phase = "native_readback"
        run_id = verify_browser_run()
        phase = "host_stop"
        api.close()
        api = None
        host_exit = host.stop()
        log_scan = host.scan_logs()
        if host_exit not in (0, -15) or log_scan["known_secret_in_logs"] or log_scan["tokenish_strings"]:
            raise RuntimeError("W2 host stop or private log checks failed")
        _write_summary(evidence_dir, {
            "status": "PASS", "run_id": str(run_id), "project_id": project["id"],
            "session_id": session["id"], "browser_exit": browser_status,
            "host_exit": host_exit, "host_logs_clean": True,
            "egress_network": W2_EGRESS_NETWORK, "scientific_endpoint": SCHOLARLY_ENDPOINTS,
        })
        print(json.dumps({"status": "PASS", "phase": "w2-scientific", "run_id": str(run_id)}))
    except BaseException as exc:
        if api is not None:
            try:
                api.close()
            except Exception:
                pass
        try:
            host.stop()
        except Exception:
            pass
        print(json.dumps({"status": "FAIL", "phase": phase, "error_type": type(exc).__name__}))
        raise SystemExit(1)


def _write_summary(evidence_dir: Path, summary: dict) -> None:
    destination = evidence_dir / "w2-scientific-proof.json"
    if destination.exists():
        raise RuntimeError("W2 evidence already exists; preserve prior outcome")
    destination.write_text(json.dumps(summary, sort_keys=True) + "\n")
    destination.chmod(0o600)


def verify_browser_run() -> UUID:
    raw, _core, _config = load_config()
    browser_path = _repo_path(raw["evidence_dir"], under=W2_ROOT) / "w2-browser-evidence.json"
    browser = _read_json(browser_path, "W2 browser evidence")
    if browser.get("status") != "PASS":
        raise RuntimeError("W2 browser evidence status mismatch")
    try:
        run_id = UUID(browser["run_id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeError("W2 browser evidence run identity is invalid") from exc

    # This verifier is intentionally read-only; H.q uses a short-lived
    # PostgreSQL session and never changes native application state.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import b5_matrix_common as c

    c.BUCKET = W2_BUCKET
    h = c.H("w2-scientific-readback").attach(run_id)
    run = h.q("SELECT state, project_id, session_id FROM runs WHERE id=:run")
    attempts = h.q(
        "SELECT stage, operation_id, native_tools_sha256 FROM w2_scientific_fixture_attempts "
        "WHERE run_id=:run ORDER BY attempted_at, stage"
    )
    operations = h.ops()
    if (len(run) != 1 or run[0]["state"] != "completed"
            or str(run[0]["project_id"]) != browser.get("project_id")
            or str(run[0]["session_id"]) != browser.get("session_id")):
        raise RuntimeError("W2 run is not completed")
    if len(attempts) != 4 or {row["stage"] for row in attempts} != {"instruction", "search", "compute", "final"}:
        raise RuntimeError("W2 fixture stages were repeated, missing, or out of order")
    if len({row["operation_id"] for row in attempts}) != 4 or len({row["native_tools_sha256"].strip() for row in attempts}) != 1:
        raise RuntimeError("W2 provider stage identities or serialized tool payload changed")
    if (sum(row["kind"] == "search" for row in operations) != 1
            or sum(row["kind"] == "compute" for row in operations) != 1
            or sum(row["kind"] == "llm" for row in operations) != 4
            or len(operations) != 6
            or any(row["kind"] not in {"llm", "search", "compute"} for row in operations)):
        raise RuntimeError("W2 actual Crossref GET or compute effect was repeated or missing")
    if any(row["state"] != "committed" for row in operations):
        raise RuntimeError("W2 operation has an unresolved outcome")
    artifact_rows = h.q(
        "SELECT id, project_id, title, object_key, sha256, size, partial "
        "FROM artifacts WHERE run_id=:run ORDER BY title"
    )
    expected = set(EXPECTED_OUTPUTS)
    if {row["title"] for row in artifact_rows} != expected or len(artifact_rows) != len(expected):
        raise RuntimeError("W2 output artifact set differs from the four compute outputs")
    if any(row["partial"] or len(row["sha256"].strip()) != 64 or row["size"] < 1 for row in artifact_rows):
        raise RuntimeError("W2 output artifacts are partial or lack durable hashes")
    browser_outputs = browser.get("outputs")
    if not isinstance(browser_outputs, dict) or set(browser_outputs) != expected:
        raise RuntimeError("W2 browser did not hash all four expected outputs")
    for row in artifact_rows:
        evidence = browser_outputs[row["title"]]
        if (not isinstance(evidence, dict)
                or evidence.get("artifact_id") != str(row["id"])
                or evidence.get("sha256") != row["sha256"].strip()
                or evidence.get("size") != row["size"]):
            raise RuntimeError("W2 browser bytes differ from persisted output metadata")
        stored = h.s3.get_object(Bucket=W2_BUCKET, Key=row["object_key"])["Body"].read(row["size"] + 1)
        if len(stored) != row["size"] or hashlib.sha256(stored).hexdigest() != row["sha256"].strip():
            raise RuntimeError("W2 output bytes differ from native MinIO readback")
    return run_id


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-check"]:
        self_check()
    elif sys.argv[1:] == ["--verify-browser-run"]:
        verify_browser_run()
    elif not sys.argv[1:]:
        run()
    else:
        raise SystemExit("usage: w2_scientific_acceptance.py [--self-check|--verify-browser-run]")
