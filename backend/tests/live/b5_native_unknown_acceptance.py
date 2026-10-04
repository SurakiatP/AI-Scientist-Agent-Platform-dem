"""Unknown-outcome native B5 fault acceptance on the owned Colima VM.

This harness consumes only the private synthetic service secrets already prepared
under the configured private dir. It does not call or require a paid provider. Evidence is
written to the run evidence dir and contains no secret values.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
import time
from uuid import uuid4

from b5_live_config import CFG

ROOT = CFG.root
PRIVATE = CFG.private_dir
EVIDENCE = CFG.evidence / "b5-native-unknown-acceptance.json"
DOCKER_CONTEXT = CFG.docker_context
WORKER_IMAGE = CFG.worker_image_id
SERVICE_IMAGE = CFG.fixture_images["counter"]
MODEL = "fixture-unknown"
RUNTIME_COMMIT = CFG.runtime_commit
USER_QUESTION = "Synthesize the approved synthetic fixture evidence."

os.environ["SCIENTIST_DATABASE_URL"] = CFG.database_url
os.environ["SCIENTIST_MASTER_KEY_FILE"] = str(PRIVATE / "master_key")
os.environ["SCIENTIST_PROVIDER_ENDPOINT"] = "https://research.example"  # D4: the synthetic plan recipient must be a configured destination

import boto3
from sqlalchemy import text

from scientist import broker, checkpoints, objects, secrets, supervisor
from scientist.contracts import CheckpointManifest, PlanSpec, Principal, ObjectRef
from scientist.db import create_project, create_session, migrate, session
from scientist.dispatch_runtime import DispatchServiceConfig, DockerDispatchRuntime
from scientist.domain import approve_run, revise_plan, submit_run
from scientist.runtime_contracts import (
    BootstrapMetadata,
    RUNTIME_COMMIT as CONTRACT_RUNTIME_COMMIT,
    RuntimeContextV1,
)
from scientist.supervisor import DockerWorkerEngine, WorkerBootstrap
from b5_native_fault_checks import assert_no_resend, assert_unknown_waiting


def docker(*args: str) -> str:
    return supervisor_engine._docker(*args)


def secret_path(name: str) -> str:
    # Return only a path for template configuration; never load a value here.
    return name


def image_id(reference: str) -> str:
    return docker("image", "inspect", reference, "--format", "{{.Id}}")


def build_context(db, run_id, generation, pins: dict[str, str]) -> WorkerBootstrap:
    row = db.execute(text("""
        SELECT r.project_id, r.revision, r.plan_digest, s.digest AS snapshot,
               p.plan
        FROM runs r
        JOIN input_snapshots s ON s.run_id=r.id AND s.project_id=r.project_id
        JOIN plan_revisions p ON p.run_id=r.id AND p.revision=r.revision
        WHERE r.id=:run AND r.generation=:generation
    """), {"run": run_id, "generation": generation}).mappings().one()
    plan = PlanSpec.model_validate(row["plan"])
    timestamp = time.time()
    context = RuntimeContextV1.model_validate({
        "schema_version": 1,
        "run_id": str(run_id),
        "project_id": str(row["project_id"]),
        "generation": generation,
        "revision": row["revision"],
        "input_snapshot_digest": row["snapshot"].strip(),
        "plan_digest": row["plan_digest"].strip(),
        "runtime_commit": RUNTIME_COMMIT,
        "image_digest": pins["image_digest"],
        "skills_digest": pins["skills_digest"],
        "environment_digest": pins["environment_digest"],
        "provider_id": str(plan.provider_id),
        "provider_endpoint": "https://research.example",
        "model": plan.model,
        "plan": plan.model_dump(mode="json"),
        "turn_id": str(uuid4()),
        "system_prompt": "Return one concise evidence synthesis based only on the approved synthetic fixture.",
        "messages": [{"role": "user", "content": USER_QUESTION}],
        "native_message_metadata": [{"message_index": 0, "timestamp": timestamp}],
        "current_turn_user_index": 0,
        "native_turn_timestamp": timestamp,
        "todo": {"todos": [], "revision": 0},
        "compacted_context": None,
        "boundary": "before_model",
        "pending_assistant": None,
        "operation_mappings": [],
        "operation_sequence": 0,
        "workspace_manifest": [],
    })
    return WorkerBootstrap(
        context=context.model_dump_json().encode("utf-8"),
        workspace=[],
        metadata=BootstrapMetadata(schema_version=1, checkpoint_revision=0),
    )


def inspect_identity(container_id: str) -> dict:
    raw = docker(
        "inspect", "--format",
        '{{.Id}}|{{.Image}}|{{.State.Status}}|{{.State.ExitCode}}|'
        '{{index .Config.Labels "scientist.platform/run"}}|'
        '{{index .Config.Labels "scientist.platform/generation"}}|'
        '{{index .Config.Labels "scientist.platform/executor"}}|'
        '{{index .Config.Labels "scientist.platform/kind"}}',
        container_id,
    )
    values = raw.split("|")
    if len(values) != 8:
        raise RuntimeError("container inspection did not return the expected identity fields")
    return dict(zip(("container_id", "image_id", "status", "exit_code", "run_id",
                     "generation", "executor_id", "kind"), values, strict=True))


def worker_diagnostics(container_id: str) -> list[dict[str, Any]]:
    """Keep only fixed-schema diagnostic events; never retain raw container logs."""
    logs = docker("logs", container_id)
    prefix = "SCIENTIST_B5_NATIVE_DIAGNOSTIC="
    allowed = {
        "native_result": {
            "event", "result_is_dict", "result_keys", "completed_true", "failed",
            "interrupted", "partial", "error_present", "error_type", "message_count",
            "pending_assistant", "failure_reason", "failure_retryable", "billing_unverified",
            "billing_block", "api_call_count", "error_shape", "error_identifier",
            "traceback_frames",
        },
        "worker_exception": {"event", "exception_types", "http_statuses"},
        "boundary_attempt": {
            "event", "attempt", "elapsed_ms", "outcome", "exception_type", "http_status",
            "http_error_code",
        },
    }
    records = []
    for line in logs.splitlines():
        if not line.startswith(prefix):
            continue
        try:
            item = json.loads(line[len(prefix):])
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict) or item.get("event") not in allowed:
            continue
        event = item["event"]
        if set(item) != allowed[event]:
            continue
        if event == "native_result":
            keys = item.get("result_keys")
            if not isinstance(keys, list) or any(not isinstance(key, str) or len(key) > 64 for key in keys):
                continue
            reason = item.get("failure_reason")
            if not isinstance(reason, str) or len(reason) > 64 or not reason.replace("_", "").isalnum():
                continue
            call_count = item.get("api_call_count")
            if call_count is not None and (type(call_count) is not int or call_count < 0):
                continue
            if item.get("error_shape") not in {
                "non_string", "other_string", "unexpected_keyword", "missing_attribute", "missing_module"
            }:
                continue
            identifier = item.get("error_identifier")
            if identifier is not None and (
                not isinstance(identifier, str)
                or len(identifier) > 128
                or any(not (char.isascii() and (char.isalnum() or char in "_.")) for char in identifier)
            ):
                continue
            frames = item.get("traceback_frames")
            if (
                not isinstance(frames, list)
                or len(frames) > 16
                or any(
                    not isinstance(frame, dict)
                    or set(frame) != {"line", "function"}
                    or type(frame["line"]) is not int
                    or not isinstance(frame["function"], str)
                    or len(frame["function"]) > 128
                    or any(not (char.isascii() and (char.isalnum() or char in "_.")) for char in frame["function"])
                    for frame in frames
                )
            ):
                continue
            item["result_keys"] = keys[:32]
        elif event == "worker_exception":
            types = item.get("exception_types")
            statuses = item.get("http_statuses")
            if (
                not isinstance(types, list)
                or any(not isinstance(value, str) or len(value) > 64 for value in types)
                or not isinstance(statuses, list)
                or any(type(value) is not int or not 100 <= value <= 599 for value in statuses)
            ):
                continue
            item["exception_types"] = types[:8]
            item["http_statuses"] = statuses[:8]
        else:
            exception_type = item.get("exception_type")
            status = item.get("http_status")
            error_code = item.get("http_error_code")
            if (
                type(item.get("attempt")) is not int
                or not 1 <= item["attempt"] <= 8
                or type(item.get("elapsed_ms")) is not int
                or not 0 <= item["elapsed_ms"] <= 60_000
                or item.get("outcome") not in {"exception", "response"}
                or (exception_type is not None and (
                    not isinstance(exception_type, str)
                    or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", exception_type)
                ))
                or (status is not None and (type(status) is not int or not 100 <= status <= 599))
                or (error_code is not None and (
                    not isinstance(error_code, str)
                    or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code)
                ))
                or (item["outcome"] == "exception") != (exception_type is not None)
                or (item["outcome"] == "response") != (status is not None)
            ):
                continue
        records.append(item)
    return records


def probe_worker_broker(container_id: str) -> dict[str, Any]:
    """Probe the actual broker endpoint from the launched worker namespace."""
    probe = r'''import ipaddress,json,os,socket,time
from urllib.parse import urlsplit
ip=None; port=None; connected=False; latency=None; status=None; ready=False
try:
    parsed=urlsplit(os.environ["SCIENTIST_BROKER_URL"])
    address=ipaddress.ip_address(parsed.hostname or "")
    if parsed.scheme != "http" or address.version != 4 or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError
    ip=address.compressed; port=parsed.port
    if port is None or not 1 <= port <= 65535:
        raise ValueError
    started=time.monotonic()
    with socket.create_connection((ip,port),timeout=2.0) as sock:
        latency=max(0,round((time.monotonic()-started)*1000)); connected=True
        sock.settimeout(2.0)
        sock.sendall(b"GET /ready HTTP/1.1\r\nHost: dispatch\r\nConnection: close\r\n\r\n")
        data=bytearray()
        while len(data) < 8192:
            part=sock.recv(min(2048,8192-len(data)))
            if not part: break
            data.extend(part)
        marker=b"\r\n\r\n"
        if marker in data:
            head,body=bytes(data).split(marker,1)
            first=head.split(b"\r\n",1)[0].split()
            if len(first) >= 2 and first[1].isdigit(): status=int(first[1])
            ready=(status == 200 and body == b'{"ready":true}')
except Exception:
    pass
print(json.dumps({"ip":ip,"port":port,"connect_success":connected,"connect_latency_ms":latency,"http_status":status,"ready_literal_matched":ready},separators=(",",":")))
'''
    raw = docker(
        "exec", "--user", "65532:65532", container_id,
        "/opt/python/bin/python3.14", "-c", probe,
    )
    try:
        result = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("worker broker probe returned invalid diagnostic data") from exc
    expected = {
        "ip", "port", "connect_success", "connect_latency_ms", "http_status",
        "ready_literal_matched",
    }
    if not isinstance(result, dict) or set(result) != expected:
        raise RuntimeError("worker broker probe returned an unexpected diagnostic shape")
    ip, port = result["ip"], result["port"]
    if ip is not None:
        import ipaddress

        parsed_ip = ipaddress.ip_address(ip)
        if parsed_ip.version != 4 or parsed_ip.compressed != ip:
            raise RuntimeError("worker broker probe returned an invalid endpoint address")
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise RuntimeError("worker broker probe returned an invalid endpoint port")
    if type(result["connect_success"]) is not bool or type(result["ready_literal_matched"]) is not bool:
        raise RuntimeError("worker broker probe returned invalid status fields")
    if result["connect_latency_ms"] is not None and (
        type(result["connect_latency_ms"]) is not int or result["connect_latency_ms"] < 0
    ):
        raise RuntimeError("worker broker probe returned invalid latency")
    status = result["http_status"]
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise RuntimeError("worker broker probe returned invalid HTTP status")
    return result


def main() -> dict:
    global supervisor_engine
    if CONTRACT_RUNTIME_COMMIT != RUNTIME_COMMIT:
        raise RuntimeError("runtime source pin differs from the accepted contract")
    if image_id(WORKER_IMAGE) != WORKER_IMAGE:
        raise RuntimeError("worker image identity differs from the immutable pin")
    if image_id(SERVICE_IMAGE) != SERVICE_IMAGE.split("@", 1)[1]:
        raise RuntimeError("fixture service image identity differs from the immutable pin")

    manifest = json.loads((ROOT / "runtime/skills-manifest.json").read_text())
    pins = {
        "image_digest": WORKER_IMAGE,
        "skills_digest": manifest["manifest_sha256"],
        "environment_digest": hashlib.sha256((ROOT / "runtime/requirements.lock").read_bytes()).hexdigest(),
    }
    restarted_s3 = boto3.client(
        "s3",
        endpoint_url=CFG.s3_endpoint,
        region_name="us-east-1",
        aws_access_key_id=(PRIVATE / "s3_access_key").read_text().strip(),
        aws_secret_access_key=(PRIVATE / "s3_secret_key").read_text().strip(),
    )
    objects.configure(restarted_s3, bucket="scientist-b5")
    checkpoints.configure_trusted_pins(
        runtime_commit=RUNTIME_COMMIT,
        image_digest=pins["image_digest"],
        skills_digest=pins["skills_digest"],
        environment_digest=pins["environment_digest"],
    )
    s3 = boto3.client(
        "s3", endpoint_url=CFG.s3_endpoint, region_name="us-east-1",
        aws_access_key_id=(PRIVATE / "s3_access_key").read_text().strip(),
        aws_secret_access_key=(PRIVATE / "s3_secret_key").read_text().strip(),
    )
    # Credentials above are read only by the harness process and never emitted.
    try:
        s3.head_bucket(Bucket="scientist-b5")
    except s3.exceptions.ClientError as exc:
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
            raise RuntimeError("synthetic MinIO bucket readiness failed") from exc
        s3.create_bucket(Bucket="scientist-b5")
        objects.configure(s3, bucket="scientist-b5")
        checkpoints.configure_trusted_pins(
        image_digest=pins["image_digest"], skills_digest=pins["skills_digest"],
        environment_digest=pins["environment_digest"], runtime_commit=RUNTIME_COMMIT,
    )
    migrate()
    supervisor_engine = DockerWorkerEngine()
    dispatch_runtime = None
    run_id = None
    worker_container = None
    run_network = None
    stage = "verify_pinned_images"
    report: dict = {
        "schema_version": 1,
        "status": "FAILED",
        "invocation": "rtk uv run --project backend python backend/tests/live/b5_native_unknown_acceptance.py",
        "docker_context": DOCKER_CONTEXT,
        "worker_image_digest": WORKER_IMAGE,
        "service_image_digest": SERVICE_IMAGE.split("@", 1)[1],
        "paid_calls": 0,
        "synthetic_transport": True,
    }
    if EVIDENCE.exists():
        previous = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        report["prior_attempts"] = previous.get("prior_attempts", [])
        if previous.get("status") == "FAILED":
            report["prior_attempts"].append({
                "status": previous.get("status"),
                "failed_at": previous.get("failed_at"),
                "failure": previous.get("failure"),
                "resources_created": previous.get("cleanup", {}).get("resources_created", False),
            })
    try:
        stage = "configure_storage_and_schema"
        with session() as db:
            stage = "create_synthetic_records"
            owner = Principal(identity=uuid4(), kind="owner")
            project_id = create_project(db, "B5 native private-service synthetic acceptance")
            chat_id = create_session(db, project_id, "Native synthetic service acceptance")
            credential_id = secrets.save_secret(db, owner, "synthetic B5 fixture credential",
                                                "synthetic-only-no-provider-access")
            provider_id = credential_id
            db.execute(text("UPDATE credentials SET project_id=:project WHERE id=:credential"),
                       {"project": project_id, "credential": credential_id})
            run = submit_run(db, owner, project_id, chat_id, uuid4().hex,
                             USER_QUESTION, [], provider_id, "fixture")
            run_id = run.run_id
            report["run_id"] = str(run_id)
            stage = "approve_plan_and_scope_credential"
            snapshot = db.execute(text("SELECT digest FROM input_snapshots WHERE run_id=:run"),
                                  {"run": run_id}).scalar_one().strip()
            plan = PlanSpec(
                input_snapshot_digest=snapshot,
                provider_id=provider_id,
            model=MODEL,
                stages=["synthesis"],
                allowed_ops=["llm"],
                data_recipients=["https://research.example"],
                packages=[],
                token_limit=20_000,
                elapsed_limit_ms=60_000,
            )
            run = revise_plan(db, owner, run_id, run.revision, plan)
            run = approve_run(db, owner, run_id, run.revision, run.plan_digest)
            db.commit()

            template = {
                "schema_version": 1,
                "runtime_commit": RUNTIME_COMMIT,
                "image_digest": pins["image_digest"],
                "skills_digest": pins["skills_digest"],
                "environment_digest": pins["environment_digest"],
                "provider_destinations": {str(provider_id): "https://research.example"},
                "secret_files": {name: secret_path(name) for name in (
                    "database_url", "broker_capability_key", "master_key",
                    "s3_access_key", "s3_secret_key",
                )},
            }
            config_path = PRIVATE / f"native-service-template-{run_id.hex}.json"
            config_path.write_text(json.dumps(template, sort_keys=True), encoding="utf-8")
            config_path.chmod(0o444)
            launcher_dir = PRIVATE / f"native-service-launches-{run_id.hex}"
            service = DispatchServiceConfig(
                image=SERVICE_IMAGE,
                image_digest=SERVICE_IMAGE.split("@", 1)[1],
                service_network="scientist-b5-services-test",
                config_path="/run/scientist/dispatch/config.json",
                secrets_dir="/run/scientist/secrets",
                host_config_file=str(config_path),
                host_secrets_dir=str(PRIVATE),
                launcher_dir=str(launcher_dir),
            )
            dispatch_runtime = DockerDispatchRuntime(service, engine=supervisor_engine)
            stage = "configure_supervisor_and_claim"
            supervisor.configure(
                image=WORKER_IMAGE,
                image_digest=WORKER_IMAGE,
                broker_url="http://127.0.0.1:8123",
                broker_ip="172.29.32.2",
                broker_port=8123,
                runtime_commit=RUNTIME_COMMIT,
                skills_digest=pins["skills_digest"],
                environment_digest=pins["environment_digest"],
                bootstrap_factory=lambda current_db, current_run, generation: build_context(
                    current_db, current_run, generation, pins),
                capability_factory=lambda current_db, current_run, generation: broker.issue_capability(
                    current_db, current_run, generation, 300),
                dispatch=dispatch_runtime,
                engine=supervisor_engine,
            )
            broker.configure(capability_key=(PRIVATE / "broker_capability_key").read_bytes().strip())
            if len(broker._key()) < 32:
                raise RuntimeError("synthetic broker capability key configuration failed")
            claim = supervisor.claim(db, max_active=3)
            if claim != (run_id, 1):
                raise RuntimeError("approved synthetic run was not claimed as generation one")
            stage = "supervisor_start_worker_and_dispatch"
            worker_container = supervisor.start(db, run_id, 1)
            stage = "probe_worker_broker_endpoint"
            report["broker_probe"] = probe_worker_broker(worker_container)
            run_network = f"scientist-run-{run_id.hex[:12]}-g1"
            identities = db.execute(text("""
                SELECT id, kind, process_incarnation, engine_id, container_id, state
                FROM runtime_executors WHERE run_id=:run AND generation=1 ORDER BY kind
            """), {"run": run_id}).mappings().all()
            by_kind = {row["kind"]: dict(row) for row in identities}
            if set(by_kind) != {"worker", "dispatch"}:
                raise RuntimeError("supervisor did not durably bind both executor identities")
            worker_identity = inspect_identity(worker_container)
            if (worker_identity["run_id"] != str(run_id)
                    or worker_identity["generation"] != "1"
                    or worker_identity["kind"] != "worker"
                    or worker_identity["executor_id"] != str(by_kind["worker"]["id"])
                    or worker_identity["image_id"] != WORKER_IMAGE):
                raise RuntimeError("worker physical identity differs from supervisor records")

            # Wait on the exact immutable worker container returned by supervisor.start.
            stage = "wait_for_native_worker"
            exit_code = docker("wait", worker_container)
        worker_identity = inspect_identity(worker_container)
        if worker_identity["run_id"] != str(run_id) or worker_identity["generation"] != "1" or worker_identity["kind"] != "worker":
            raise RuntimeError("worker identity changed before unknown-outcome verification")
        stage = "verify_unknown_outcome_and_held_reservation"
        with session() as db:
            baseline = assert_unknown_waiting(db, run_id)
            report.update({
                "worker": worker_identity,
                "worker_exit_code": int(exit_code),
                "unknown_snapshot": baseline,
                "checks": [
                    "one fixture-side durable provider attempt before timeout",
                    "unknown operation has no committed result reference",
                    "full reservation retained and owner decision required",
                ],
            })

        stage = "supervisor_recover_unknown_outcome"
        with session() as db:
            recovered = supervisor.recover(db, run_id)
            if (recovered.state, recovered.waiting_reason) != ("waiting_input", "unknown_outcome"):
                raise RuntimeError("recovery did not preserve the owner decision state")
            assert_no_resend(db, run_id, baseline)
        report["recovery"] = {"state": recovered.state, "waiting_reason": recovered.waiting_reason}

        stage = "fresh_supervisor_process_recovery"
        restart_probe = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--verify-restart", str(run_id),
             json.dumps(baseline, sort_keys=True)],
            cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
        )
        if restart_probe.returncode != 0 or restart_probe.stdout.strip() != "B5_UNKNOWN_RESTART_PASS":
            raise RuntimeError("fresh supervisor process did not preserve unknown state without resend")
        report["status"] = "PASS"
        report["fresh_supervisor_process_recovery"] = {"fresh_process": True, "result": "PASS"}

        # The supervisor owns exact executor stop/fencing. Network deletion is
        # performed only after both recorded container identities are inactive.
        stage = "supervisor_cleanup"
        with session() as db:
            assert_no_resend(db, run_id, baseline)
            current = assert_unknown_waiting(db, run_id)
            rows = db.execute(text("""
                SELECT kind, state, container_id, engine_id, proof
                FROM runtime_executors WHERE run_id=:run AND generation=1
            """), {"run": run_id}).mappings().all()
            if len(rows) != 2 or any(row["state"] != "inactive" for row in rows):
                raise RuntimeError("executor inactivity was not durably proven")
            report["cleanup"] = {
                "status": "PASS",
                "resources_created": True,
                "owner_wait_preserved": current["run"]["state"] == "waiting_input",
                "executors_inactive": True,
                "executor_proofs": [dict(row["proof"]) for row in rows],
            }
        stage = "remove_exact_run_network"
        listed = [item for item in docker(
            "network", "ls", "-q", "--filter", f"label=scientist.platform/run={run_id}",
            "--filter", "label=scientist.platform/generation=1",
        ).splitlines() if item]
        if len(listed) != 1:
            raise RuntimeError("run network identity is not unique during cleanup")
        network_info = json.loads(docker("network", "inspect", "--format", "{{json .}}", listed[0]))
        labels = network_info.get("Labels") or {}
        if (network_info.get("Name") != run_network
                or labels.get("scientist.platform/run") != str(run_id)
                or labels.get("scientist.platform/generation") != "1"):
            raise RuntimeError("network labels differ from the exact run identity")
        docker("network", "rm", listed[0])
        if docker("network", "ls", "-q", "--filter", f"id={listed[0]}").strip():
            raise RuntimeError("exact run network remains after cleanup")
        report["cleanup"].update({"network_removed": True, "network_id": listed[0]})
        report["cleanup"]["status"] = "PASS"
        EVIDENCE.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return report
    except Exception as exc:
        report["status"] = "FAILED"
        report["failure"] = "see harness exception; private values intentionally omitted"
        report["failed_at"] = stage
        report["failure_type"] = type(exc).__name__
        trace = exc.__traceback__
        failure_line = None
        while trace is not None:
            if Path(trace.tb_frame.f_code.co_filename).resolve() == Path(__file__).resolve():
                failure_line = trace.tb_lineno
            trace = trace.tb_next
        report["failure_line"] = failure_line
        if run_id is not None and dispatch_runtime is not None:
            try:
                with session() as cleanup_db:
                    state = cleanup_db.execute(text("SELECT state FROM runs WHERE id=:run"),
                                               {"run": run_id}).scalar_one_or_none()
                    if state in {"queued", "running", "recovering", "stopping"}:
                        supervisor.stop(cleanup_db, run_id, 5)
                    elif state == "waiting_input":
                        reason = cleanup_db.execute(
                            text("SELECT waiting_reason FROM runs WHERE id=:run"), {"run": run_id}
                        ).scalar_one_or_none()
                        if reason == "unknown_outcome":
                            supervisor.recover(cleanup_db, run_id)
                        else:
                            supervisor.stop(cleanup_db, run_id, 5)
                    executor_rows = cleanup_db.execute(text("""
                        SELECT kind, state, container_id
                        FROM runtime_executors
                        WHERE run_id=:run AND generation=1
                        ORDER BY kind
                    """), {"run": run_id}).mappings().all()
                if len(executor_rows) != 2 or any(row["state"] != "inactive" for row in executor_rows):
                    raise RuntimeError("executor inactivity is not durably proven after failure cleanup")
                for row in executor_rows:
                    container_id = row["container_id"]
                    if not container_id:
                        continue
                    present = docker("ps", "-aq", "--no-trunc", "--filter", f"id={container_id}").splitlines()
                    if present:
                        identity = inspect_identity(container_id)
                        if (identity["container_id"] != container_id or identity["status"] == "running"
                                or identity["run_id"] != str(run_id) or identity["generation"] != "1"):
                            raise RuntimeError("recorded executor remains live or has mismatched identity")
                leftovers = [item for item in docker(
                    "network", "ls", "-q", "--filter", f"label=scientist.platform/run={run_id}",
                    "--filter", "label=scientist.platform/generation=1",
                ).splitlines() if item]
                network_removed = bool(leftovers)
                if leftovers:
                    if len(leftovers) != 1:
                        raise RuntimeError("run network identity is ambiguous during failure cleanup")
                    details = json.loads(docker("network", "inspect", "--format", "{{json .}}", leftovers[0]))
                    labels = details.get("Labels") or {}
                    if (details.get("Name") != f"scientist-run-{run_id.hex[:12]}-g1"
                            or labels.get("scientist.platform/run") != str(run_id)
                            or labels.get("scientist.platform/generation") != "1"
                            or details.get("Containers")):
                        raise RuntimeError("run network still has unproven attached executors")
                    docker("network", "rm", leftovers[0])
                network_remaining = [item for item in docker(
                    "network", "ls", "-q", "--filter", f"label=scientist.platform/run={run_id}",
                    "--filter", "label=scientist.platform/generation=1",
                ).splitlines() if item]
                if network_remaining:
                    raise RuntimeError("run network remains after exact cleanup")
                report["cleanup"] = {
                    "status": "PASS",
                    "resources_created": bool(worker_container or executor_rows),
                    "executors_inactive": True,
                    "network_removed": network_removed,
                    "network_absent_after_cleanup": True,
                }
            except Exception:
                report["cleanup"] = {"status": "UNPROVEN; exact-resource cleanup needs operator review"}
        EVIDENCE.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        raise


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--verify-restart":
    from uuid import UUID

    restarted_run = UUID(sys.argv[2])
    baseline = json.loads(sys.argv[3])
    manifest = json.loads((ROOT / "runtime/skills-manifest.json").read_text())
    pins = {
        "image_digest": WORKER_IMAGE,
        "skills_digest": manifest["manifest_sha256"],
        "environment_digest": hashlib.sha256((ROOT / "runtime/requirements.lock").read_bytes()).hexdigest(),
    }
    config_path = PRIVATE / f"native-service-template-{restarted_run.hex}.json"
    launcher_dir = PRIVATE / f"native-service-launches-{restarted_run.hex}"
    service = DispatchServiceConfig(
        image=SERVICE_IMAGE,
        image_digest=SERVICE_IMAGE.split("@", 1)[1],
        service_network="scientist-b5-services-test",
        config_path="/run/scientist/dispatch/config.json",
        secrets_dir="/run/scientist/secrets",
        host_config_file=str(config_path),
        host_secrets_dir=str(PRIVATE),
        launcher_dir=str(launcher_dir),
    )
    restarted_engine = DockerWorkerEngine()
    restarted_dispatch = DockerDispatchRuntime(service, engine=restarted_engine)
    broker.configure(capability_key=(PRIVATE / "broker_capability_key").read_bytes().strip())
    supervisor.configure(
        image=WORKER_IMAGE,
        image_digest=WORKER_IMAGE,
        broker_url="http://127.0.0.1:8123",
        broker_ip="172.29.32.2",
        broker_port=8123,
        runtime_commit=RUNTIME_COMMIT,
        skills_digest=pins["skills_digest"],
        environment_digest=pins["environment_digest"],
        bootstrap_factory=lambda db, run_id, generation: build_context(db, run_id, generation, pins),
        capability_factory=lambda db, run_id, generation: broker.issue_capability(db, run_id, generation, 300),
        dispatch=restarted_dispatch,
        engine=restarted_engine,
    )
    with session() as restarted_db:
        recovered = supervisor.recover(restarted_db, restarted_run)
        if (recovered.state, recovered.waiting_reason) != ("waiting_input", "unknown_outcome"):
            raise RuntimeError("fresh supervisor recovery did not preserve the owner decision")
        assert_no_resend(restarted_db, restarted_run, baseline)
    print("B5_UNKNOWN_RESTART_PASS")
elif __name__ == "__main__":
    supervisor_engine = DockerWorkerEngine()
    result = main()
    print(json.dumps({"status": result["status"], "run_id": result.get("run_id"),
                      "evidence": str(EVIDENCE), "paid_calls": result["paid_calls"]}, sort_keys=True))
