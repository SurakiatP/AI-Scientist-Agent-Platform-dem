#!/usr/bin/env python3
"""W2 live actors for committed-result recovery and stop-unknown preservation.

Run only with the separately prepared W2 configuration. The default actor
exercises Crossref + CSV recovery; ``stop`` starts a no-Crossref resource run
and launches the real Playwright stop test against that host.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import types
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
W2_ROOT = ROOT / ".local" / "w2-scientific-20261007"
W2_BUCKET = "scientist-w2-20261007"
W2_DB = "scientist_w2_20261007"
FIXTURE = Path(__file__).with_name("fixtures") / "scientific" / "w2_partial.csv"
OUTPUTS = ("summary.json", "summary.csv", "chart.svg", "report.md")
STOP_PROFILES = ("prof.worker-base@py3.14.7",)
RUN_PROFILES = ("prof.worker-base@py3.14.7", "prof.csv-stdlib@py3.14.7")


class AcceptanceError(RuntimeError):
    pass


def need(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def wait_for(fn, message: str, timeout: float, step: float = 0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(step)
    raise TimeoutError(message)


def _load_runtime():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import w2_scientific_acceptance as w2

    try:
        raw, _core, config_path = w2.load_config()
    except w2.NotRunError as exc:
        w2._not_run(str(exc))
    evidence_dir = w2._repo_path(raw["evidence_dir"], under=W2_ROOT)
    w2._set_up_process_environment(config_path, evidence_dir)
    import b5_matrix_common as common
    import b5_host_http_acceptance as host_http
    import httpx
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    common.BUCKET = W2_BUCKET
    need(make_url(common.DB_URL).database == W2_DB, "W2 database authority mismatch")
    need(common.PRIVATE.resolve() == W2_ROOT / "private", "W2 private namespace mismatch")
    need(os.environ.get("PGPASSFILE"), "W2 PostgreSQL passfile unavailable")
    need(not subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain", "--", "backend/src", "runtime"],
        capture_output=True, text=True, check=True,
    ).stdout.strip(), "backend/runtime differs from immutable image sources")
    engine = common.H("w2-recovery-preflight")
    need(engine.engine_id() == common.EXPECTED_ENGINE_ID, "owned Docker engine changed")
    common.guard_storage_headroom()
    for name, expected in {
        "scientist-b5-postgres": raw["postgres_container_id"],
        "scientist-b5-minio": raw["minio_container_id"],
    }.items():
        actual = engine.docker("inspect", "--format", "{{.Id}}", name).strip()
        running = engine.docker("inspect", "--format", "{{.State.Running}}", name).strip()
        need(actual == expected and running == "true", f"owned W2 service unavailable: {name}")
    network = engine.docker(
        "network", "inspect", "--format",
        '{{.Id}}|{{index .Labels "scientist.platform/egress"}}|{{.Internal}}',
        "scientist-b5-egress-test",
    ).strip()
    need(network == f"{raw['egress_network_id']}|b5|false",
         "owned W2 Crossref egress network identity or policy differs")
    for image in (raw["scientific_fixture_image"], raw["compute_image"], common.CFG.worker_image,
                  common.CFG.server_image, common.CFG.postgres_image, common.CFG.minio_image):
        engine.docker("image", "--format", "{{.Id}}", image.split("@", 1)[1])
    source_map = w2._read_json(common.CFG.server_source_hashes, "W2 server source hash map")
    for relative, expected in source_map.get("files", source_map).items():
        if relative.startswith(("backend/src/", "runtime/")):
            actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
            need(actual == expected, "server source differs immutable-image source hash map")
    host_http.forbid_supervisor()
    return w2, common, host_http, httpx, text, raw, evidence_dir


def _host(common, host_http, raw, evidence_dir: Path, label: str, provider_id: uuid.UUID):
    from w2_scientific_acceptance import configure_host_database
    root = evidence_dir / label
    root.mkdir(mode=0o700, exist_ok=False)
    host = host_http.HostProc(label, root, provider_id, raw["scientific_fixture_image"], 0.5)
    host.cfg.update({
        "service_network": "scientist-b5-services-test",
        "bucket": W2_BUCKET,
        "compute_image": raw["compute_image"],
        "egress_network": "scientist-b5-egress-test",
        "scientific_bundle_dir": str(Path(raw["scientific_bundle_dir"]).resolve()),
        "state_dir": str(host.state),
        "web_dist_dir": str(Path(raw["web_dist_dir"]).resolve()),
    })
    host.env["SCIENTIST_SCHOLARLY_ENDPOINTS"] = "https://api.crossref.org"
    configure_host_database(host)
    host.cfg_path.write_text(json.dumps(host.cfg, sort_keys=True))
    host.cfg_path.chmod(0o600)
    import shutil
    shutil.copytree(raw["sealed_profile_evidence_dir"], host.state / "profiles")
    shutil.copytree(raw["sealed_compute_profile_evidence_dir"], host.state / "compute-profiles")
    return host


def _create_project_run(api, common, host_http, *, workflow: str, provider_id: uuid.UUID):
    project = host_http.call(api, "POST", "/api/v1/projects", json={
        "name": "W2 recovery acceptance", "instructions": "",
    })
    session = host_http.call(api, "POST", f"/api/v1/projects/{project['id']}/sessions", json={
        "title": "Scientific recovery acceptance",
    })
    connection = host_http.call(api, "POST", "/api/v1/connections", json={
        "provider_id": str(provider_id), "label": "Synthetic W2 provider",
        "model": "fixture", "secret": "synthetic-only-no-provider-access",
    }, ok=(201,))
    need(uuid.UUID(connection["id"]) == provider_id and connection["state"] == "ready",
         "synthetic provider connection was not configured")

    input_ids: list[str] = []
    file_id = None
    if workflow == "crossref_csv":
        uploaded = api.post(
            f"/api/v1/projects/{project['id']}/files?filename=w2_partial.csv",
            content=FIXTURE.read_bytes(), headers={"content-type": "text/csv"},
        )
        need(uploaded.status_code == 201, "W2 CSV upload failed")
        file_id = uploaded.json()["id"]
        input_ids = [file_id]
        wait_for(
            lambda: next((item for item in host_http.call(
                api, "GET", f"/api/v1/projects/{project['id']}/files")
                if item["id"] == file_id and item["state"] == "ready"), None),
            "W2 CSV input did not become ready", 60,
        )

    created = host_http.call(api, "POST", f"/api/v1/sessions/{session['id']}/runs", json={
        "submission_key": uuid.uuid4().hex,
        "question": "Describe workspace resources for stop acceptance." if workflow == "resources"
                    else "Compare Crossref metadata with approved CSV measurements.",
        "input_ids": input_ids,
        "provider_id": str(provider_id),
        "model": "fixture",
    }, ok=(201,))
    run_id = uuid.UUID(created["run_id"])
    plan0 = host_http.call(api, "GET", f"/api/v1/runs/{run_id}/plan")
    if workflow == "resources":
        selection = None
        plan_payload = {"expected_revision": plan0["revision"], "workflow": "resources", "search_terms": []}
    else:
        need(file_id is not None, "W2 CSV file identity missing")
        selection = {
            "crossref": {
                "source_id": "crossref", "version": 1, "access_mode": "public_read",
                "query": "coastal nitrate monitoring", "doi": None, "limit": 5,
            },
            "csv_file_id": file_id,
            "numeric_columns": ["temperature_c", "nitrate_mg_l"],
        }
        plan_payload = {
            "expected_revision": plan0["revision"], "workflow": "crossref_csv",
            "search_terms": [], "csv_selection": selection,
        }
    host_http.call(api, "POST", f"/api/v1/runs/{run_id}/prepare-plan", json=plan_payload)
    _prepare_profiles(api, host_http, project["id"], STOP_PROFILES if workflow == "resources" else RUN_PROFILES)
    plan = host_http.call(api, "GET", f"/api/v1/runs/{run_id}/plan")
    updated = host_http.call(api, "PATCH", f"/api/v1/runs/{run_id}/plan", json={
        "expected_revision": plan["revision"],
        "plan": {**plan["plan"], "token_limit": 20_000, "elapsed_limit_ms": 600_000},
    })
    plan = host_http.call(api, "GET", f"/api/v1/runs/{run_id}/plan")
    readiness = host_http.call(api, "GET", f"/api/v1/runs/{run_id}/readiness")
    need(updated["revision"] == plan["revision"] and plan["plan"]["token_limit"] == 20_000
         and plan["plan"]["elapsed_limit_ms"] == 600_000, "approved budget was not persisted")
    need(readiness["state"] == "ready" and readiness["revision"] == plan["revision"]
         and readiness["plan_digest"] == plan["plan_digest"], "scientific plan readiness is stale")
    return project, session, run_id, plan


def _prepare_profiles(api, host_http, project_id: str, required: tuple[str, ...]) -> None:
    setup = host_http.call(api, "GET", f"/api/v1/projects/{project_id}/research-setup")
    selected = [profile for profile in setup["profiles"] if profile["profile_id"] in required]
    need({profile["profile_id"] for profile in selected} == set(required), "required setup profile missing")
    for profile in selected:
        existing = next((job for job in setup["preparations"]
                         if job["profile_id"] == profile["profile_id"]), None)
        if existing and existing["state"] == "ready" and existing["stage"] == "complete" \
                and existing["evidence_verified"]:
            continue
        if existing and existing["state"] in {"queued", "building", "checking"}:
            job = existing
        elif existing:
            raise AcceptanceError(f"existing W2 profile preparation is {existing['state']}")
        else:
            job = host_http.call(api, "POST", f"/api/v1/projects/{project_id}/preparations", json={
                "profile_id": profile["profile_id"], "version": profile["version"],
                "manifest_sha256": profile["manifest_sha256"], "request_id": str(uuid.uuid4()),
            }, ok=(200, 201))
        job_id = job["id"]
        wait_for(lambda: _ready_job(host_http, api, project_id, job_id),
                 "W2 reviewed profile preparation did not finish", 300, 1.0)


def _ready_job(host_http, api, project_id: str, job_id: str):
    job = host_http.call(api, "GET", f"/api/v1/projects/{project_id}/preparations/{job_id}")
    if job["state"] in {"failed", "blocked", "unknown"}:
        raise AcceptanceError(f"profile preparation ended {job['state']}")
    return job if job["state"] == "ready" and job["stage"] == "complete" and job["evidence_verified"] else None


def _arm(common, text, run_id: uuid.UUID, table: str) -> None:
    if table == "w1_boundary_fault_targets":
        ddl = "CREATE TABLE IF NOT EXISTS w1_boundary_fault_targets (run_id uuid PRIMARY KEY, armed_at timestamptz NOT NULL DEFAULT now())"
    elif table == "w2_fixture_stall_targets":
        ddl = "CREATE TABLE IF NOT EXISTS w2_fixture_stall_targets (run_id uuid PRIMARY KEY, armed_at timestamptz NOT NULL DEFAULT now())"
    else:
        raise ValueError("unsupported W2 fixture target")
    with common.session() as db:
        db.execute(text(ddl))
        if table == "w1_boundary_fault_targets":
            db.execute(text("""CREATE TABLE IF NOT EXISTS w1_boundary_barriers (
                run_id uuid NOT NULL, generation bigint NOT NULL,
                released boolean NOT NULL DEFAULT false, PRIMARY KEY (run_id))"""))
        else:
            db.execute(text("""CREATE TABLE IF NOT EXISTS w2_fixture_stalls (
                run_id uuid PRIMARY KEY, operation_id text NOT NULL,
                started_at timestamptz NOT NULL DEFAULT clock_timestamp())"""))
        db.execute(text(f"INSERT INTO {table}(run_id) VALUES (:run)"), {"run": run_id})
        db.commit()


def _receipts_at_tool_checkpoint(h, text):
    from scientist.contracts import ObjectRef

    rows = h.q("""
        SELECT r.output_index, r.tool_call_id, r.artifact_id, r.receipt_sha256,
               c.manifest->'context' AS context
        FROM scientific_artifact_receipts r
        JOIN checkpoints c ON c.id=r.checkpoint_id
        WHERE r.run_id=:run
        ORDER BY r.output_index
    """)
    committed = []
    for row in rows:
        ref = ObjectRef.model_validate(row["context"])
        need(ref.size <= 1_048_576, "W2 checkpoint context exceeds byte limit")
        response = h.s3.get_object(Bucket=W2_BUCKET, Key=ref.key)
        with response["Body"] as body:
            data = body.read(ref.size + 1)
        need(len(data) == ref.size and hashlib.sha256(data).hexdigest() == ref.sha256,
             "W2 checkpoint context bytes differ from verified reference")
        context = json.loads(data)
        if context.get("boundary") == "tool_committed":
            committed.append(dict(row, context=context))
    return committed


def _check_v2_receipts(rows) -> None:
    need(len(rows) == 4 and {row["output_index"] for row in rows} == {0, 1, 2, 3},
         "W2 V2 receipt set is not exactly four indexed outputs")
    need(len({row["artifact_id"] for row in rows}) == 4
         and len({row["receipt_sha256"].strip() for row in rows}) == 4,
         "W2 V2 receipts do not bind four distinct outputs and receipt digests")
    need(all((row["context"] or {}).get("boundary") == "tool_committed" for row in rows),
         "W2 V2 receipt is not bound to the committed tool checkpoint")


def _output_snapshot(h, expected_titles: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    rows = h.q("""
        SELECT id, title, object_key, sha256, size, partial
        FROM artifacts WHERE run_id=:run ORDER BY title
    """)
    need({row["title"] for row in rows} == set(expected_titles) and len(rows) == len(expected_titles),
         "run artifact set differs from acceptance contract")
    need(all(not row["partial"] and len(row["sha256"].strip()) == 64 and row["size"] > 0 for row in rows),
         "run contains partial or unhashed scientific output")
    return {row["title"]: {**row, "sha256": row["sha256"].strip()} for row in rows}


def _assert_physical_executors_inactive(h, common, *, generations: set[int] | None = None) -> list[dict[str, Any]]:
    rows = h.executors()
    selected = [row for row in rows if generations is None or row["generation"] in generations]
    need(bool(selected) and all(row["state"] == "inactive" for row in selected),
         "one or more selected W2 executors are not durably inactive")
    for row in selected:
        need(row["engine_id"] == common.EXPECTED_ENGINE_ID and row["container_id"]
             and (row["proof"] or {}).get("engine_id") == row["engine_id"]
             and (row["proof"] or {}).get("container_id") == row["container_id"],
             "W2 executor fencing proof is not bound to exact engine/container identity")
        live = h.docker("ps", "-aq", "--no-trunc", "--filter", f"id={row['container_id']}").strip()
        need(not live, "inactive W2 executor container still exists")
    return selected


def _assert_four_effects(h, expected_state: str) -> None:
    attempts = h.q("SELECT stage,operation_id FROM w2_scientific_fixture_attempts WHERE run_id=:run ORDER BY stage")
    ops = h.ops()
    _check_four_effect_lineage(attempts, ops, expected_state)


def _check_four_effect_lineage(attempts, ops, expected_state: str) -> None:
    need(len(attempts) == 4 and {row["stage"] for row in attempts} == {"instruction", "search", "compute", "final"}
         and len({row["operation_id"] for row in attempts}) == 4,
         "W2 fixture stages repeated, skipped, or changed operation identity")
    need(len(ops) == 6 and sum(row["kind"] == "llm" for row in ops) == 4
         and sum(row["kind"] == "search" for row in ops) == 1
         and sum(row["kind"] == "compute" for row in ops) == 1
         and all(row["state"] == expected_state for row in ops),
         "W2 Crossref/compute effects or ledger states differ from one invocation")


def _read_outputs(api, h, artifacts: dict[str, dict[str, Any]]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for title, row in artifacts.items():
        rest = api.get(f"/api/v1/artifacts/{row['id']}/content")
        need(rest.status_code == 200, f"authenticated REST output unavailable: {title}")
        rest_bytes = rest.content
        rest_hash = hashlib.sha256(rest_bytes).hexdigest()
        need(rest_hash == row["sha256"] and len(rest_bytes) == row["size"],
             f"REST output bytes differ immutable metadata: {title}")
        stored = h.s3.get_object(Bucket=W2_BUCKET, Key=row["object_key"])["Body"].read(row["size"] + 1)
        need(stored == rest_bytes and hashlib.sha256(stored).hexdigest() == row["sha256"],
             f"MinIO output bytes differ authenticated REST bytes: {title}")
        hashes[title] = rest_hash
    return hashes


def _run_recovery() -> None:
    w2, c, http, httpx, text, raw, evidence = _load_runtime()
    w2.self_check()
    engine = c.H("w2-recovery-run")
    provider = uuid.uuid4()
    host = _host(c, http, raw, evidence, "w2-scientific-recovery", provider)
    api = None
    host2 = None
    phase = "host_start"
    try:
        api = host.start()
        project, session, run_id, plan = _create_project_run(api, c, http, workflow="crossref_csv", provider_id=provider)
        _arm(c, text, run_id, "w1_boundary_fault_targets")
        http.call(api, "POST", f"/api/v1/runs/{run_id}/approve", json={
            "expected_revision": plan["revision"], "plan_digest": plan["plan_digest"],
        })
        h = c.H("w2-recovery-run").attach(run_id)
        h.eng, h.engine_id = engine.eng, engine.engine_id
        barrier = wait_for(
            lambda: h.q("SELECT generation,released FROM w1_boundary_barriers WHERE run_id=:run"),
            "W2 committed-result boundary did not reach the fault barrier", 300,
        )
        need(len(barrier) == 1 and barrier[0]["generation"] == 1 and not barrier[0]["released"],
             "W2 fault barrier is not held by generation one")
        receipts = wait_for(lambda: rows if len(rows := _receipts_at_tool_checkpoint(h, text)) == 4 else None,
                            "four V2 output receipts were not committed at tool_committed", 30)
        _check_v2_receipts(receipts)
        before = _output_snapshot(h, OUTPUTS)
        before_hashes = {title: row["sha256"] for title, row in before.items()}
        phase = "kill_and_restart"
        host.proc.kill()
        host.proc.wait(timeout=10)
        need(host.proc.returncode == -signal.SIGKILL, "first W2 host was not killed exactly")
        restart_root = evidence / "w2-scientific-recovery-restart"
        restart_root.mkdir(mode=0o700, exist_ok=False)
        host2 = http.HostProc("w2-scientific-recovery-restart", restart_root,
                              provider, raw["scientific_fixture_image"], 0.5)
        host2.state = host.state
        host2.cfg = {**host.cfg, "listen_port": host2.port, "state_dir": str(host.state)}
        host2.cfg_path.write_text(json.dumps(host2.cfg, sort_keys=True))
        host2.cfg_path.chmod(0o600)
        host2.env["SCIENTIST_SCHOLARLY_ENDPOINTS"] = "https://api.crossref.org"
        w2.configure_host_database(host2)
        api.close()
        api = host2.start()
        fenced = wait_for(
            lambda: rows if (rows := h.executors()) and all(row["state"] == "inactive" for row in rows if row["generation"] == 1)
            and any(row["generation"] >= 2 for row in rows) else None,
            "restarted W2 host did not fence generation one", 240,
        )
        need(any(row["generation"] == 1 and row["state"] == "inactive"
                 and row["engine_id"] == c.EXPECTED_ENGINE_ID and row["container_id"]
                 and (row["proof"] or {}).get("engine_id") == c.EXPECTED_ENGINE_ID
                 and (row["proof"] or {}).get("container_id") == row["container_id"] for row in fenced),
             "generation-one executor fencing proof is not bound to exact physical identity")
        _assert_physical_executors_inactive(h, c, generations={1})
        with c.session() as db:
            changed = db.execute(text("UPDATE w1_boundary_barriers SET released=true WHERE run_id=:run AND generation=1"),
                                 {"run": run_id}).rowcount
            db.commit()
        need(changed == 1, "exact W2 boundary barrier release failed")
        phase = "completion_readback"
        result = wait_for(lambda: run if (run := http.call(api, "GET", f"/api/v1/runs/{run_id}")).get("state")
                          in c.TERMINAL else None, "recovered W2 run did not terminate", 360, 0.5)
        need(result["state"] == "completed", f"recovered W2 run ended {result['state']}")
        need(h.run_row()["generation"] == 2, "recovered W2 run generation is not exactly two")
        after = _output_snapshot(h, OUTPUTS)
        after_hashes = {title: row["sha256"] for title, row in after.items()}
        need(before_hashes == after_hashes, "W2 recovery changed one or more committed output hashes")
        _assert_four_effects(h, "committed")
        need(h.run_row()["usage_tokens"] > 0 and h.run_row()["reserved_tokens"] == 0,
             "completed W2 run usage reservation did not settle")
        final_executors = _assert_physical_executors_inactive(h, c)
        need(not h.docker("network", "ls", "-q", "--filter", f"label=scientist.platform/run={run_id}").strip(),
             "completed W2 run left a run-labelled network")
        after_receipts = h.q("""
            SELECT output_index,artifact_id,receipt_sha256 FROM scientific_artifact_receipts
            WHERE run_id=:run ORDER BY output_index
        """)
        need(len(after_receipts) == 4 and {row["artifact_id"] for row in after_receipts} ==
             {row["artifact_id"] for row in receipts}, "W2 recovery duplicated or lost V2 output receipts")
        read_hashes = _read_outputs(api, h, after)
        need(read_hashes == after_hashes, "authenticated REST/MinIO output readback changed hashes")
        summary = {"status": "PASS", "phase": "w2-scientific-recovery", "run_id": str(run_id),
                   "project_id": project["id"], "session_id": session["id"], "generation": 2,
                   "receipts": 4, "executors_inactive": len(final_executors),
                   "effects": {"crossref": 1, "compute": 1}, "output_sha256": read_hashes}
        _write_evidence(evidence / "w2-scientific-recovery-proof.json", summary)
        api.close()
        api = None
        need(host2.stop() in (0, -signal.SIGTERM), "restarted W2 host did not stop cleanly")
        need(host.scan_logs()["known_secret_in_logs"] == 0 and host2.scan_logs()["known_secret_in_logs"] == 0,
             "W2 host logs contain a known secret")
        print(json.dumps(summary, sort_keys=True))
    except BaseException as exc:
        print(json.dumps({"status": "FAIL", "phase": phase, "error_type": type(exc).__name__}))
        raise
    finally:
        if api is not None:
            api.close()
        for process in (host2, host):
            if process is not None and process.proc is not None and process.proc.poll() is None:
                process.stop()


def _run_stop() -> None:
    """Prepare a no-Crossref run, signal Playwright only after the fixture stalls."""
    w2, c, http, _httpx, text, raw, evidence = _load_runtime()
    w2.self_check()
    engine = c.H("w2-stop-run")
    provider = uuid.uuid4()
    host = _host(c, http, raw, evidence, "w2-stop-unknown", provider)
    api = None
    browser = None
    phase = "host_start"
    ready_file = evidence / "w2-stop-stall-ready"
    try:
        api = host.start()
        project, session, run_id, plan = _create_project_run(api, c, http, workflow="resources", provider_id=provider)
        _arm(c, text, run_id, "w2_fixture_stall_targets")
        proof = {"project_id": project["id"], "session_id": session["id"], "run_id": str(run_id),
                 "workflow": "resources", "crossref_requests_expected": 0}
        proof_path = evidence / "w2-stop-browser-proof.json"
        _write_evidence(proof_path, proof)
        if ready_file.exists():
            ready_file.unlink()
        browser_env = os.environ.copy()
        browser_session = w2.write_browser_session(api, host, host.state / "browser-owner-session.json")
        browser_env.update({
            "SCIENTIFIC_W2_STOP_OWNER_SESSION_FILE": str(browser_session),
            "SCIENTIFIC_W2_STOP_PROOF": str(proof_path),
            "SCIENTIFIC_W2_STOP_BROWSER_EVIDENCE": str(evidence / "w2-stop-browser-evidence.json"),
            "SCIENTIFIC_W2_STOP_API_ORIGIN": host.base,
            "SCIENTIFIC_W2_STOP_STALL_READY": str(ready_file),
            "CI": "1",
        })
        phase = "real_browser_stop"
        browser = subprocess.Popen(["npm", "run", "test:e2e", "--", "--grep", "W2 stop preserves unknown compute outcome"],
                                   cwd=ROOT / "apps/web", env=browser_env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        h = c.H("w2-stop-run").attach(run_id)
        h.eng, h.engine_id = engine.eng, engine.engine_id
        wait_for(lambda: h.q("SELECT operation_id FROM w2_fixture_stalls WHERE run_id=:run"),
                 "W2 fixture did not durably record the stalled operation", 180)
        ready_file.write_text("stalled\n")
        ready_file.chmod(0o600)
        stdout, _ = browser.communicate(timeout=660)
        need(browser.returncode == 0, "real W2 stop browser acceptance failed")
        need("W2 stop preserves unknown compute outcome" in stdout, "Playwright did not run the stop-only case")
        phase = "unknown_outcome_readback"
        run = h.run_row()
        ops = h.ops()
        need(run["state"] == "canceled" or
             (run["state"] == "waiting_input" and run["waiting_reason"] == "unknown_outcome"),
             "stopped no-Crossref run has an unexpected domain state")
        need(len(ops) == 1 and ops[0]["kind"] == "llm" and ops[0]["state"] == "unknown",
             "stopped fixture operation did not remain unknown")
        need(run["usage_tokens"] == 0 and run["reserved_tokens"] > 0
             and ops[0]["usage_tokens"] in (None, 0), "unknown operation reservation was settled")
        pending = http.call(api, "GET", f"/api/v1/runs/{run_id}/pending-decisions")
        need(len(pending) == 1 and pending[0]["reason"] == "unknown_outcome"
             and pending[0]["operation_reserved_tokens"] == ops[0]["reserve_tokens"] > 0,
             "unknown operation no longer has its exact pending owner decision and reservation")
        executors = wait_for(lambda: rows if (rows := h.executors()) and all(row["state"] == "inactive" for row in rows)
                             else None, "stop did not prove all physical executors inactive", 90)
        need(all(row["engine_id"] == c.EXPECTED_ENGINE_ID and row["container_id"]
                 and (row["proof"] or {}).get("engine_id") == row["engine_id"]
                 and (row["proof"] or {}).get("container_id") == row["container_id"] for row in executors),
             "stop executor cleanup is not bound to exact physical identities")
        _assert_physical_executors_inactive(h, c)
        need(not h.docker("network", "ls", "-q", "--filter", f"label=scientist.platform/run={run_id}").strip(),
             "stopped W2 run left a run-labelled network")
        need(not h.q("""
            SELECT 1 FROM owner_decisions
            WHERE run_id=:run AND state='resolved'
              AND resolution->>'choice' IN ('retry','confirm_result','confirm_usage')
        """),
             "stop actor confirmed or retried the unknown operation")
        evidence_doc = {"status": "PASS", "phase": "w2-stop-unknown", "run_id": str(run_id),
                        "run_state": run["state"], "operation_state": ops[0]["state"],
                        "usage_tokens": run["usage_tokens"], "reserved_tokens": run["reserved_tokens"],
                        "pending_decisions": len(pending), "executors": len(executors), "crossref_requests": 0}
        _write_evidence(evidence / "w2-stop-acceptance-proof.json", evidence_doc)
        api.close()
        api = None
        need(host.stop() in (0, -signal.SIGTERM), "W2 stop host did not stop cleanly")
        log_scan = host.scan_logs()
        need(log_scan["known_secret_in_logs"] == 0 and log_scan["tokenish_strings"] == 0,
             "W2 stop host logs contain secret-like material")
        print(json.dumps(evidence_doc, sort_keys=True))
    except BaseException as exc:
        if browser is not None and browser.poll() is None:
            browser.terminate()
            try:
                browser.wait(timeout=10)
            except subprocess.TimeoutExpired:
                browser.kill()
                browser.wait(timeout=10)
        print(json.dumps({"status": "FAIL", "phase": phase, "error_type": type(exc).__name__}))
        raise
    finally:
        if api is not None:
            api.close()
        if host.proc is not None and host.proc.poll() is None:
            host.stop()


def _write_evidence(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n")
    path.chmod(0o600)


def self_check() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import w2_scientific_acceptance as w2
    w2.self_check()
    fixture = (Path(__file__).with_name("fixtures") / "scientific" / "sitecustomize.py").read_text()
    need("context.get(\"boundary\") == \"tool_committed\"" in fixture,
         "W2 fixture no longer gates ACK loss on committed checkpoint context")
    need("scientific_artifact_receipts r JOIN checkpoints c" in fixture,
         "W2 fixture no longer checks durable scientific receipt/checkpoint binding")
    need("INSERT INTO w2_fixture_stalls(run_id, operation_id)" in fixture and "time.sleep(120)" in fixture,
         "W2 stop fixture no longer persists its stall marker before blocking")
    fixture_module = w2._load_fixture_module()
    need(fixture_module._hold_boundary(200, True, True, True)
         and not fixture_module._hold_boundary(200, False, True, True)
         and not fixture_module._hold_boundary(503, True, True, True),
         "W2 boundary fixture no longer gates on successful committed receipt")
    events = []

    class _Result:
        def __init__(self, value=None, rowcount=0):
            self.value, self.rowcount = value, rowcount

        def scalar_one(self):
            return self.value

        def scalar_one_or_none(self):
            return self.value

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, parameters=None):
            query = str(statement)
            events.append(("execute", query, parameters))
            if "to_regclass" in query:
                return _Result("w2_fixture_stall_targets")
            if query.startswith("SELECT 1 FROM w2_fixture_stall_targets"):
                return _Result(1)
            if query.startswith("INSERT INTO w2_fixture_stalls"):
                return _Result(rowcount=1)
            return _Result()

        def commit(self):
            events.append(("commit",))

    previous_scientist = sys.modules.get("scientist")
    previous_time = fixture_module.time
    sys.modules["scientist"] = types.SimpleNamespace(db=types.SimpleNamespace(session=lambda: _Session()))
    fixture_module.time = types.SimpleNamespace(sleep=lambda seconds: events.append(("sleep", seconds)))
    try:
        try:
            fixture_module._stall_targeted_operation(types.SimpleNamespace(
                run_id=uuid.uuid4(), operation_id="w2-stop-self-check",
            ))
        except TimeoutError:
            pass
        else:
            raise AssertionError("W2 stop self-check did not hold the provider response")
    finally:
        fixture_module.time = previous_time
        if previous_scientist is None:
            sys.modules.pop("scientist", None)
        else:
            sys.modules["scientist"] = previous_scientist
    marker_index = next((index for index, event in enumerate(events)
                         if event[0] == "execute" and event[1].startswith("INSERT INTO w2_fixture_stalls")), -1)
    need(marker_index >= 0 and events[marker_index + 1:] == [("commit",), ("sleep", 120)],
         "W2 fixture stall marker was not committed before its blocking wait")
    receipt_rows = [{"output_index": index, "artifact_id": f"artifact-{index}",
                     "receipt_sha256": str(index) * 64, "context": {"boundary": "tool_committed"}}
                    for index in range(4)]
    _check_v2_receipts(receipt_rows)
    operations = ([{"kind": "llm", "state": "committed"} for _ in range(4)]
                  + [{"kind": "search", "state": "committed"}, {"kind": "compute", "state": "committed"}])
    attempts = [{"stage": stage, "operation_id": f"op-{index}"}
                for index, stage in enumerate(("instruction", "search", "compute", "final"))]
    _check_four_effect_lineage(attempts, operations, "committed")
    try:
        _check_four_effect_lineage(attempts, [*operations, operations[-1]], "committed")
    except AcceptanceError:
        pass
    else:
        raise AssertionError("W2 offline lineage check accepted a repeated compute operation")
    need(OUTPUTS == ("summary.json", "summary.csv", "chart.svg", "report.md"),
         "W2 output lineage set changed")
    print(json.dumps({"status": "PASS", "check": "w2-recovery-and-stop-source"}, sort_keys=True))


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--self-check"]:
        self_check()
    elif args in ([], ["recovery"]):
        _run_recovery()
    elif args == ["stop"]:
        _run_stop()
    else:
        raise SystemExit("usage: w2_scientific_recovery_acceptance.py [--self-check|recovery|stop]")
