"""Committed-result-before-delivery recovery fault acceptance; synthetic providers only."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import time
from uuid import uuid4

from b5_live_config import CFG

ROOT = CFG.root
PRIVATE = CFG.private_dir
EVIDENCE = CFG.evidence / "b5-native-committed-acceptance.json"
DOCKER_CONTEXT = CFG.docker_context
WORKER_IMAGE = CFG.worker_image_id
BARRIER_SERVICE_IMAGE = CFG.fixture_images["barrier"]
RECOVERY_SERVICE_IMAGE = CFG.fixture_images["counter"]
MODEL = "fixture"
RUNTIME_COMMIT = CFG.runtime_commit
EXPECTED_SYNTHESIS = "Synthetic research synthesis with no paid call."
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
from scientist.private_worker_api import RuntimePins, WorkerController
from scientist.domain import approve_run, revise_plan, submit_run
from scientist.runtime_contracts import (
    BootstrapMetadata,
    RUNTIME_COMMIT as CONTRACT_RUNTIME_COMMIT,
    RuntimeContextV1,
)
from scientist.supervisor import DockerWorkerEngine, WorkerBootstrap, ExecutorRef


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


def bootstrap_for(db, run_id, generation, pins: dict[str, str]) -> WorkerBootstrap:
    # Generation 1 builds the fresh revision-0 context. Any later generation has
    # durable checkpoints, so it MUST continue from the verified checkpoint via the
    # trusted controller; an error is raised, never a fresh-context fallback.
    if generation == 1:
        return build_context(db, run_id, generation, pins)
    row = db.execute(text("SELECT revision FROM runs WHERE id=:run"), {"run": run_id}).one()
    plan = broker._load_plan(db, run_id, row.revision)
    controller = WorkerController(
        pins=RuntimePins(runtime_commit=RUNTIME_COMMIT, image_digest=pins["image_digest"],
                         skills_digest=pins["skills_digest"], environment_digest=pins["environment_digest"]),
        provider_destinations={plan.provider_id: "https://research.example"})
    return supervisor.continuation_bootstrap(db, run_id, generation, controller)


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
        raise RuntimeError("runtime source pin differs from accepted contract")
    manifest = json.loads((ROOT / "runtime/skills-manifest.json").read_text())
    pins = {
        "image_digest": WORKER_IMAGE,
        "skills_digest": manifest["manifest_sha256"],
        "environment_digest": hashlib.sha256((ROOT / "runtime/requirements.lock").read_bytes()).hexdigest(),
    }
    s3 = boto3.client(
        "s3", endpoint_url=CFG.s3_endpoint, region_name="us-east-1",
        aws_access_key_id=(PRIVATE / "s3_access_key").read_text().strip(),
        aws_secret_access_key=(PRIVATE / "s3_secret_key").read_text().strip(),
    )
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
    supervisor_engine = None
    engine_identity = None
    run_id = None
    run_networks: set[str] = set()
    worker_container = None
    dispatch_runtime = None
    stage = "initialize"
    report: dict[str, Any] = {
        "schema_version": 1, "status": "FAILED", "docker_context": DOCKER_CONTEXT,
        "engine_id": None, "worker_image_digest": WORKER_IMAGE,
        "barrier_service_image_digest": BARRIER_SERVICE_IMAGE.split("@", 1)[1],
        "recovery_service_image_digest": RECOVERY_SERVICE_IMAGE.split("@", 1)[1],
        "paid_calls": 0,
    }
    try:
        stage = "verify_owned_engine_and_pinned_images"
        supervisor_engine = DockerWorkerEngine()  # fixed owned context/profile
        engine_identity = supervisor_engine.engine_id()  # requires Engine 29.8.2
        report["engine_id"] = engine_identity
        if image_id(WORKER_IMAGE) != WORKER_IMAGE:
            raise RuntimeError("worker image identity differs from immutable pin")
        for service_image in (BARRIER_SERVICE_IMAGE, RECOVERY_SERVICE_IMAGE):
            if image_id(service_image) != service_image.split("@", 1)[1]:
                raise RuntimeError("fixture service image identity differs from immutable pin")
        stage = "create_and_approve_synthetic_run"
        with session() as db:
            owner = Principal(identity=uuid4(), kind="owner")
            project_id = create_project(db, "B5 committed-result recovery acceptance")
            chat_id = create_session(db, project_id, "Committed-result recovery")
            credential_id = secrets.save_secret(db, owner, "synthetic fixture credential", "synthetic-only-no-provider-access")
            provider_id = credential_id
            db.execute(text("UPDATE credentials SET project_id=:project WHERE id=:credential"),
                       {"project": project_id, "credential": credential_id})
            run = submit_run(db, owner, project_id, chat_id, uuid4().hex, USER_QUESTION, [], provider_id, "fixture")
            run_id = run.run_id
            report["run_id"] = str(run_id)
            snapshot = db.execute(text("SELECT digest FROM input_snapshots WHERE run_id=:run"), {"run": run_id}).scalar_one().strip()
            plan = PlanSpec(
                input_snapshot_digest=snapshot, provider_id=provider_id, model=MODEL,
                stages=["synthesis"], allowed_ops=["llm"], data_recipients=["https://research.example"],
                packages=[], token_limit=20_000, elapsed_limit_ms=60_000,
            )
            run = revise_plan(db, owner, run_id, run.revision, plan)
            approve_run(db, owner, run_id, run.revision, run.plan_digest)
            db.commit()
        template = {
            "schema_version": 1, "runtime_commit": RUNTIME_COMMIT,
            "image_digest": pins["image_digest"], "skills_digest": pins["skills_digest"],
            "environment_digest": pins["environment_digest"],
            "provider_destinations": {str(provider_id): "https://research.example"},
            "secret_files": {name: secret_path(name) for name in (
                "database_url", "broker_capability_key", "master_key", "s3_access_key", "s3_secret_key",
            )},
        }
        config_path = PRIVATE / f"native-committed-template-{run_id.hex}.json"
        config_path.write_text(json.dumps(template, sort_keys=True), encoding="utf-8")
        config_path.chmod(0o444)
        launcher_dir = PRIVATE / f"native-committed-launches-{run_id.hex}"
        launcher_dir.mkdir(mode=0o700)

        def make_dispatch(image: str, label: str) -> DockerDispatchRuntime:
            service = DispatchServiceConfig(
                image=image, image_digest=image.split("@", 1)[1],
                service_network="scientist-b5-services-test",
                config_path="/run/scientist/dispatch/config.json",
                secrets_dir="/run/scientist/secrets",
                host_config_file=str(config_path), host_secrets_dir=str(PRIVATE),
                launcher_dir=str(launcher_dir / label),
            )
            return DockerDispatchRuntime(service, engine=supervisor_engine)

        broker.configure(capability_key=(PRIVATE / "broker_capability_key").read_bytes().strip(),
                         provider_destinations={str(provider_id): "https://research.example"},
                         # Same synthetic public resolution as the fixture images; no real DNS.
                         resolver=lambda host, port: ["8.8.8.8"])
        if len(broker._key()) < 32:
            raise RuntimeError("synthetic broker capability key configuration failed")

        def configure_runtime(dispatch: DockerDispatchRuntime) -> None:
            supervisor.configure(
                image=WORKER_IMAGE, image_digest=WORKER_IMAGE,
                broker_url="http://127.0.0.1:8123", broker_ip="172.29.32.2", broker_port=8123,
                runtime_commit=RUNTIME_COMMIT, skills_digest=pins["skills_digest"],
                environment_digest=pins["environment_digest"],
                bootstrap_factory=lambda current_db, current_run, generation: bootstrap_for(current_db, current_run, generation, pins),
                capability_factory=lambda current_db, current_run, generation: broker.issue_capability(current_db, current_run, generation, 300),
                dispatch=dispatch, engine=supervisor_engine,
            )

        stage = "launch_barrier_fixture_worker"
        dispatch_runtime = make_dispatch(BARRIER_SERVICE_IMAGE, "barrier")
        configure_runtime(dispatch_runtime)
        with session() as db:
            claim = supervisor.claim(db, max_active=3)
            if claim != (run_id, 1):
                raise RuntimeError("approved synthetic run was not claimed at generation one")
            worker_container = supervisor.start(db, run_id, 1)
            run_networks.add(f"scientist-run-{run_id.hex[:12]}-g1")
        worker_identity = inspect_identity(worker_container)
        if (worker_identity["run_id"] != str(run_id) or worker_identity["generation"] != "1"
                or worker_identity["kind"] != "worker" or worker_identity["image_id"] != WORKER_IMAGE):
            raise RuntimeError("barrier worker physical identity differs from supervisor records")

        stage = "wait_for_committed_post_barrier"
        deadline = time.monotonic() + 90
        baseline = None
        object_sha256 = None
        while time.monotonic() < deadline:
            with session() as db:
                # The fixture creates its barrier table lazily on first effect delivery.
                barrier = None
                if db.execute(text("SELECT to_regclass('b5_fixture_delivery_barriers')")).scalar() is not None:
                    barrier = db.execute(text("""SELECT reached_at, released FROM b5_fixture_delivery_barriers
                        WHERE run_id=:run AND generation=1"""), {"run": run_id}).mappings().one_or_none()
                ops = db.execute(text("""SELECT operation_id, kind, generation, state, usage_tokens, reserve_tokens, result
                    FROM operations WHERE run_id=:run ORDER BY created_at, operation_id"""), {"run": run_id}).mappings().all()
                live_run = db.execute(text("SELECT state, generation, usage_tokens, reserved_tokens, project_id, plan_digest FROM runs WHERE id=:run"),
                                      {"run": run_id}).mappings().one()
                attempts = db.execute(text("""SELECT count(*) FROM b5_fixture_provider_attempts
                    WHERE run_id=:run"""), {"run": run_id}).scalar_one_or_none()
                if barrier is not None and barrier["reached_at"] is not None and not barrier["released"] and len(ops) == 1:
                    op = ops[0]
                    result = op["result"] or {}
                    ref_data = result.get("ref")
                    if (op["kind"] != "llm" or op["state"] != "committed" or op["generation"] != 1
                            or op["usage_tokens"] != 2 or result.get("usage_known") is not True
                            or result.get("usage_tokens") != 2
                            or not ref_data or attempts != 1 or live_run["state"] != "running"
                            or op["reserve_tokens"] <= 0
                            or live_run["generation"] != 1 or live_run["usage_tokens"] != 2
                            or live_run["reserved_tokens"] != 0 or live_run["project_id"] != project_id):
                        raise RuntimeError("POST barrier was reached without the expected committed result and single attempt")
                    ref = ObjectRef.model_validate(ref_data)
                    if ref.project_id != project_id:
                        raise RuntimeError("committed result reference escaped its project")
                    with objects.open_verified(ref) as result_file:
                        result_bytes = result_file.read()
                    object_sha256 = hashlib.sha256(result_bytes).hexdigest()
                    if len(result_bytes) != ref.size or object_sha256 != ref.sha256:
                        raise RuntimeError("committed MinIO result bytes differ from the referenced hash/size")
                    prekill_checkpoint = db.execute(text("""SELECT manifest FROM checkpoints WHERE run_id=:run
                        ORDER BY revision DESC LIMIT 1"""), {"run": run_id}).mappings().one()
                    prekill_manifest = CheckpointManifest.model_validate(prekill_checkpoint["manifest"])
                    with objects.open_verified(prekill_manifest.context) as context_file:
                        prekill_context = RuntimeContextV1.model_validate_json(context_file.read())
                    prekill_users = [m for m in prekill_context.messages if m.role == "user"]
                    if (prekill_context.boundary != "before_model" or len(prekill_users) != 1
                            or prekill_users[0].content != USER_QUESTION
                            or prekill_context.project_id != project_id
                            or prekill_context.plan_digest != live_run["plan_digest"]):
                        raise RuntimeError("pre-kill checkpoint does not preserve the approved single user identity")
                    baseline = {"operation_id": op["operation_id"], "generation": op["generation"],
                                "usage_tokens": op["usage_tokens"], "reserve_tokens": op["reserve_tokens"],
                                "ref": ref.model_dump(mode="json"), "ref_sha256": object_sha256,
                                "provider_attempts": attempts, "reached_at": barrier["reached_at"].isoformat(),
                                "run_id": str(run_id), "project_id": str(project_id),
                                "plan_digest": live_run["plan_digest"],
                                "turn_id": str(prekill_context.turn_id), "user_message_count": len(prekill_users)}
                    break
            time.sleep(0.1)
        if baseline is None:
            raise TimeoutError("did not observe committed POST result while delivery barrier held")
        report["pre_kill"] = baseline

        stage = "kill_exact_worker_before_delivery"
        if worker_identity["status"] != "running":
            raise RuntimeError("worker was not alive at the held POST response")
        docker("kill", worker_container)
        exit_code = docker("wait", worker_container)
        killed = inspect_identity(worker_container)
        if (killed["container_id"] != worker_container or killed["run_id"] != str(run_id)
                or killed["generation"] != "1" or killed["kind"] != "worker"
                or killed["status"] != "exited" or int(exit_code) != 137):
            raise RuntimeError("exact barrier worker kill did not prove exit 137")

        # Reconcile with the old pinned 172 image: find() must match the old
        # container image before it can prove and remove the exact dispatcher.
        stage = "fence_old_generation_and_release_barrier"
        with session() as db:
            fenced = supervisor.recover(db, run_id)
            if fenced.state != "queued":
                raise RuntimeError(f"old generation was not safely fenced: {fenced.state}")
            old_rows = db.execute(text("""SELECT kind, state, container_id, engine_id, proof
                FROM runtime_executors WHERE run_id=:run AND generation=1"""),
                {"run": run_id}).mappings().all()
            if (len(old_rows) != 2 or {row["kind"] for row in old_rows} != {"worker", "dispatch"}
                    or any(row["state"] != "inactive" or not row["container_id"] or not row["engine_id"]
                           for row in old_rows)):
                raise RuntimeError("old worker and barrier dispatch are not durably inactive")
            old_physical = {row["kind"]: row["container_id"] for row in old_rows}
            for row in old_rows:
                proof = row["proof"] or {}
                if (proof.get("source") not in {"owned-engine-exact-container", "owned-engine-generation-fence"}
                        or proof.get("container_id") != row["container_id"]
                        or proof.get("engine_id") != row["engine_id"]):
                    raise RuntimeError("old executor inactivity proof does not bind exact physical identity")
                if docker("ps", "-aq", "--no-trunc", "--filter", f"id={row['container_id']}").strip():
                    raise RuntimeError("old executor container remains after exact generation fencing")
            # Release only after worker kill and supervisor-proven dispatch quiescence.
            updated = db.execute(text("""UPDATE b5_fixture_delivery_barriers SET released=true
                WHERE run_id=:run AND generation=1"""), {"run": run_id}).rowcount
            if updated != 1:
                raise RuntimeError("exact barrier release row was not updated")
            db.commit()

        stage = "reclaim_with_ordinary_counter_fixture"
        dispatch_runtime = make_dispatch(RECOVERY_SERVICE_IMAGE, "recovery")
        configure_runtime(dispatch_runtime)
        with session() as db:
            # Recovery under the ordinary fixture sees only inactive old executor
            # proofs; claim advances the same run into its continuation generation.
            recovered = supervisor.recover(db, run_id)
            if recovered.state != "queued":
                raise RuntimeError(f"committed-result recovery did not remain queued: {recovered.state}")
            claim = supervisor.claim(db, max_active=3)
            if claim is None or claim[0] != run_id or claim[1] < 2:
                raise RuntimeError("same run was not reclaimed in a newer generation")
            generation = claim[1]
            worker_container = supervisor.start(db, run_id, generation)
            run_networks.add(f"scientist-run-{run_id.hex[:12]}-g{generation}")
        recovered_worker = inspect_identity(worker_container)
        if (recovered_worker["run_id"] != str(run_id) or int(recovered_worker["generation"]) != generation
                or recovered_worker["kind"] != "worker" or recovered_worker["image_id"] != WORKER_IMAGE):
            raise RuntimeError("recovery worker physical identity differs from supervisor records")
        stage = "wait_for_recovered_worker"
        completion_code = docker("wait", worker_container)
        if int(completion_code) != 0:
            raise RuntimeError("recovered native worker did not exit successfully")
        final_worker_identity = inspect_identity(worker_container)
        if (final_worker_identity["run_id"] != str(run_id)
                or int(final_worker_identity["generation"]) != generation
                or final_worker_identity["status"] != "exited"):
            raise RuntimeError("recovered worker identity changed before completion checks")

        stage = "verify_exactly_once_completion"
        with session() as db:
            completed = supervisor.recover(db, run_id)
            run = db.execute(text("SELECT state, generation, usage_tokens, reserved_tokens FROM runs WHERE id=:run"),
                             {"run": run_id}).mappings().one()
            ops = db.execute(text("""SELECT operation_id, kind, generation, state, usage_tokens, reserve_tokens, result
                FROM operations WHERE run_id=:run ORDER BY created_at, operation_id"""),
                {"run": run_id}).mappings().all()
            attempts = db.execute(text("SELECT count(*) FROM b5_fixture_provider_attempts WHERE run_id=:run"),
                                  {"run": run_id}).scalar_one()
            if completed.state != "completed" or run["state"] != "completed" or run["generation"] != generation:
                raise RuntimeError("recovered run did not reach completed state")
            if run["usage_tokens"] != 2 or run["reserved_tokens"] != 0 or len(ops) != 1:
                raise RuntimeError("recovery changed final budget or operation count")
            op = ops[0]
            if (op["operation_id"] != baseline["operation_id"] or op["generation"] != baseline["generation"]
                    or op["state"] != "committed" or op["usage_tokens"] != baseline["usage_tokens"]
                    or op["reserve_tokens"] != baseline["reserve_tokens"] or attempts != 1):
                raise RuntimeError("operation identity/result/attempt count changed during recovery")
            final_ref = ObjectRef.model_validate((op["result"] or {}).get("ref"))
            if final_ref.model_dump(mode="json") != baseline["ref"]:
                raise RuntimeError("recovery changed the committed result reference")
            with objects.open_verified(final_ref) as result_file:
                final_bytes = result_file.read()
            if hashlib.sha256(final_bytes).hexdigest() != baseline["ref_sha256"]:
                raise RuntimeError("recovered result bytes differ from the pre-kill committed result")
            checkpoint_row = db.execute(text("""SELECT manifest FROM checkpoints WHERE run_id=:run
                ORDER BY revision DESC LIMIT 1"""), {"run": run_id}).mappings().one()
            checkpoint = CheckpointManifest.model_validate(checkpoint_row["manifest"])
            with objects.open_verified(checkpoint.context) as context_file:
                final_context = RuntimeContextV1.model_validate_json(context_file.read())
            user_messages = [m for m in final_context.messages if m.role == "user"]
            assistant_messages = [m for m in final_context.messages if m.role == "assistant"]
            if (final_context.boundary != "final" or len(user_messages) != 1
                    or user_messages[0].content != USER_QUESTION or not assistant_messages
                    or EXPECTED_SYNTHESIS not in (assistant_messages[-1].content or "")
                    or not final_context.operation_mappings
                    or final_context.operation_mappings[0].operation_id != baseline["operation_id"]
                    or final_context.run_id != run_id or final_context.generation != run["generation"]
                    or str(final_context.project_id) != baseline["project_id"]
                    or final_context.plan_digest != baseline["plan_digest"]
                    or str(final_context.turn_id) != baseline["turn_id"]):
                raise RuntimeError("final checkpoint does not preserve one user message and original operation identity")
            report.update({"completion": {"state": run["state"], "generation": generation,
                           "usage_tokens": run["usage_tokens"], "reserved_tokens": run["reserved_tokens"],
                           "user_message_count": len(user_messages), "operation_id": op["operation_id"],
                           "provider_attempts": attempts, "result_sha256": baseline["ref_sha256"],
                           "checkpoint_revision": checkpoint.revision}})

        stage = "exact_resource_cleanup"
        with session() as db:
            # Reconcile the stopped old generation and completed current generation.
            supervisor.recover(db, run_id)
            stopped = supervisor.stop(db, run_id, 5)
            if stopped.state != "completed":
                raise RuntimeError("stop changed the completed terminal state")
            rows = db.execute(text("""SELECT generation, kind, state, container_id, engine_id, proof
                FROM runtime_executors WHERE run_id=:run ORDER BY generation, kind"""),
                {"run": run_id}).mappings().all()
            if (not rows or {row["kind"] for row in rows} != {"worker", "dispatch"}
                    or any(row["state"] != "inactive" or not row["container_id"] or not row["engine_id"]
                           for row in rows)):
                raise RuntimeError("all old/current executor rows are not durably inactive")
            for row in rows:
                proof = row["proof"] or {}
                if (proof.get("source") not in {"owned-engine-exact-container", "owned-engine-generation-fence"}
                        or proof.get("container_id") != row["container_id"]
                        or proof.get("engine_id") != row["engine_id"]):
                    raise RuntimeError("executor cleanup proof does not bind exact physical identity")
                if docker("ps", "-aq", "--no-trunc", "--filter", f"id={row['container_id']}").strip():
                    raise RuntimeError("executor container remains after cleanup proof")
            report["stop_preserved_completed"] = stopped.state == "completed"
            report["executors"] = [{"generation": row["generation"], "kind": row["kind"],
                                    "state": row["state"], "container_id": row["container_id"],
                                    "engine_id": row["engine_id"], "proof": row["proof"]} for row in rows]
        all_networks = [item for item in docker("network", "ls", "-q", "--filter", f"label=scientist.platform/run={run_id}").splitlines() if item]
        for network_id in all_networks:
            details = json.loads(docker("network", "inspect", "--format", "{{json .}}", network_id))
            labels = details.get("Labels") or {}
            network_generation = labels.get("scientist.platform/generation")
            if (not network_generation or not network_generation.isdigit()
                    or details.get("Name") != f"scientist-run-{run_id.hex[:12]}-g{network_generation}"
                    or labels.get("scientist.platform/run") != str(run_id) or details.get("Containers")):
                raise RuntimeError("network still has attached or mismatched resources")
            docker("network", "rm", network_id)
        remaining = [item for item in docker("network", "ls", "-q", "--filter", f"label=scientist.platform/run={run_id}").splitlines() if item]
        if remaining:
            raise RuntimeError("run-labeled networks remain after exact cleanup")
        report["cleanup"] = {"status": "PASS", "network_absent": True,
                              "executors_inactive": True, "owned_engine_id": engine_identity}
        report["status"] = "PASS"
        report["checks"] = ["POST response barrier held after commit", "MinIO result bytes hash-verified before kill",
                            "exact old worker exit 137", "old dispatch quiesced before barrier release",
                            "ordinary counter fixture used for continuation", "stable run/user/operation/result identity",
                            "no provider resend", "eventual completed run", "stop preserved completed state",
                            "exact network and executor cleanup"]
        EVIDENCE.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return report
    except Exception as exc:
        report["status"] = "FAILED"
        report["failed_at"] = stage
        report["failure_type"] = type(exc).__name__
        report["failure"] = "see harness exception; private values intentionally omitted"
        if run_id is not None:
            try:
                with session() as db:
                    if dispatch_runtime is not None:
                        # Recovery is exact-identity fenced and never retries unknown operations.
                        supervisor.recover(db, run_id)
                    rows = db.execute(text("""SELECT generation, kind, state, container_id
                        FROM runtime_executors WHERE run_id=:run ORDER BY generation, kind"""),
                        {"run": run_id}).mappings().all()
                    report["cleanup"] = {"status": "PARTIAL", "executor_rows": len(rows),
                                          "inactive_rows": sum(row["state"] == "inactive" for row in rows)}
            except Exception:
                report["cleanup"] = {"status": "UNPROVEN; operator review required"}
        EVIDENCE.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        raise


if __name__ == "__main__":
    result = main()
    print(json.dumps({"status": result["status"], "run_id": result.get("run_id"),
                      "evidence": str(EVIDENCE), "paid_calls": result["paid_calls"]}, sort_keys=True))
