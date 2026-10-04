#!/usr/bin/env python3
"""Exercise the production DockerWorkerEngine create/start bootstrap path.

Creates one uniquely labeled worker in the owned Colima profile, leaves the
readiness marker absent, verifies bootstrap files as UID 65532, then removes
only the exact worker and its generated per-run network.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from scientist import runtime_contracts, supervisor
from scientist.contracts import PlanSpec

from b5_live_config import CFG


CONTEXT = CFG.docker_context
PROFILE = CFG.colima_profile
IMAGE_ID = CFG.worker_image_id
IMAGE_REF = CFG.worker_image
ENGINE_VERSION = CFG.engine_version
REPORT = CFG.evidence / "b5-worker-bootstrap-final-acceptance.json"


class AcceptanceError(RuntimeError):
    pass


def docker(*args: str, timeout: int = 45, check: bool = True) -> str:
    cp = subprocess.run(["docker", "--context", CONTEXT, *args], stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, timeout=timeout, check=False)
    if check and cp.returncode:
        raise AcceptanceError(f"Docker failed ({cp.returncode}) for {args[:3]!r}: {cp.stderr[-1000:]}")
    return cp.stdout.strip()


def make_bootstrap(run_id, project_id, provider_id, image_digest, workspace_payload: bytes):
    snapshot_digest = hashlib.sha256(b"synthetic immutable input snapshot").hexdigest()
    skills_digest = hashlib.sha256(b"synthetic skills manifest").hexdigest()
    environment_digest = hashlib.sha256(b"synthetic environment manifest").hexdigest()
    plan = PlanSpec(
        input_snapshot_digest=snapshot_digest,
        provider_id=provider_id,
        model="bootstrap-smoke-model",
        stages=["Verify production worker bootstrap"],
        allowed_ops=["llm"],
        data_recipients=["https://example.invalid"],
        packages=[],
        token_limit=100,
        elapsed_limit_ms=10_000,
    )
    plan_digest = hashlib.sha256(
        runtime_contracts.canonical_bytes(plan.model_dump(mode="json"))
    ).hexdigest()
    workspace_path = "acceptance/input.txt"
    workspace_digest = hashlib.sha256(workspace_payload).hexdigest()
    manifest = runtime_contracts.WorkspaceEntry(
        path=workspace_path, sha256=workspace_digest, size=len(workspace_payload)
    )
    context = runtime_contracts.RuntimeContextV1(
        schema_version=1,
        run_id=run_id,
        project_id=project_id,
        generation=1,
        revision=1,
        input_snapshot_digest=snapshot_digest,
        plan_digest=plan_digest,
        runtime_commit=runtime_contracts.RUNTIME_COMMIT,
        image_digest=image_digest,
        skills_digest=skills_digest,
        environment_digest=environment_digest,
        provider_id=provider_id,
        provider_endpoint="https://example.invalid",
        model=plan.model,
        plan=plan,
        turn_id=uuid4(),
        system_prompt="Synthetic acceptance bootstrap; perform no model or tool operation.",
        messages=[{"role": "user", "content": "Read the synthetic acceptance fixture."}],
        native_message_metadata=[],
        current_turn_user_index=0,
        native_turn_timestamp=None,
        todo=runtime_contracts.TodoSnapshot(todos=[], revision=0),
        compacted_context=None,
        boundary="before_model",
        pending_assistant=None,
        operation_mappings=[],
        operation_sequence=0,
        workspace_manifest=[manifest],
    )
    workspace = runtime_contracts.WorkspaceFile(
        path=workspace_path,
        sha256=workspace_digest,
        size=len(workspace_payload),
        data_base64=base64.b64encode(workspace_payload).decode("ascii"),
    )
    metadata = runtime_contracts.BootstrapMetadata(schema_version=1, checkpoint_revision=0)
    bootstrap = supervisor.WorkerBootstrap(
        context=context.model_dump_json().encode("utf-8"), workspace=[workspace], metadata=metadata
    )
    # Reuse production validation before allocating any engine resources.
    supervisor._bootstrap_files(bootstrap, "acceptance-capability-never-valid")
    return bootstrap, context, workspace, metadata


def inspect_container(cid: str) -> dict:
    return json.loads(docker("inspect", cid))[0]


def resource_is_absent(resource_type: str, name: str) -> bool:
    cp = subprocess.run(["docker", "--context", CONTEXT, resource_type, "inspect", name],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                        timeout=20, check=False)
    if cp.returncode == 0:
        return False
    diagnostic = cp.stderr.lower()
    if (resource_type == "container" and "no such container" in diagnostic) or (
            resource_type == "network" and (
                "no such network" in diagnostic or f"network {name.lower()} not found" in diagnostic
            )):
        return True
    raise AcceptanceError(f"could not verify {resource_type} cleanup: {cp.stderr[-500:]}")


def exec_as_worker(cid: str, code: str, *args: str) -> dict:
    output = docker("exec", cid, "/opt/python/bin/python3.14", "-c", code, *args, timeout=20)
    return json.loads(output)


def main() -> int:
    if os.environ.get("DOCKER_CONTEXT") not in (None, "", CONTEXT):
        raise AcceptanceError("DOCKER_CONTEXT would redirect the owned engine harness")
    if docker("context", "show") != CONTEXT:
        raise AcceptanceError("unexpected Docker context")
    version = docker("version", "--format", "{{.Server.Version}}")
    if version != ENGINE_VERSION:
        raise AcceptanceError(f"expected owned test engine {ENGINE_VERSION}, got {version}")
    reported_engine_id = docker("info", "--format", "{{.ID}}")
    actual_image = docker("image", "inspect", IMAGE_REF, "--format", "{{.Id}}")
    if actual_image != IMAGE_ID:
        raise AcceptanceError(f"worker image digest mismatch: {actual_image}")

    run_id, project_id, provider_id = uuid4(), uuid4(), uuid4()
    executor_id, incarnation = uuid4(), uuid4()
    token = f"synthetic-capability-{uuid4()}"
    token_digest = hashlib.sha256(token.encode()).hexdigest()
    payload = b"Synthetic workspace payload for tmpfs bootstrap verification.\n"
    payload_digest = hashlib.sha256(payload).hexdigest()
    bootstrap, context, workspace, metadata = make_bootstrap(
        run_id, project_id, provider_id, IMAGE_ID, payload
    )
    engine = supervisor.DockerWorkerEngine()
    original_config = supervisor._config
    supervisor._config = SimpleNamespace(image=IMAGE_REF, image_digest=IMAGE_ID)
    network = None
    container_id = None
    engine_id = engine.engine_id()
    result: dict = {
        "status": "running",
        "scope": "actual create/start, firewall, tmpfs bootstrap readback, and readiness release on the final candidate image",
        "docker_context": CONTEXT,
        "colima_profile": PROFILE,
        "engine_version": version,
        "engine_id_preflight": reported_engine_id,
        "image_ref": IMAGE_REF,
        "image_id": actual_image,
        "historical_8cb_bootstrap_evidence": {
            "file": "b5-worker-bootstrap-acceptance.json",
            "image_id": "sha256:8cbde14ca35ea7cf9977c77260d6fe8d95dfc275e2155a37151f88042c75a04d",
            "historical_only": True,
        },
        "run_id": str(run_id),
        "executor_id": str(executor_id),
        "container_id": None,
        "network": None,
        "readiness_released": False,
        "expected_hashes": {
            "capability_sha256": token_digest,
            "workspace_payload_sha256": payload_digest,
        },
    }
    try:
        if engine.engine_id() != reported_engine_id:
            raise AcceptanceError("owned engine identity changed during preflight")
        network, broker_ip = engine.create_run_network(run_id, 1, executor_id)
        result["network"] = network
        result["broker_ip"] = broker_ip
        # Keep production image/config pinning and create the exact labelled
        # worker while stopped. Read-only tmpfs copies happen only after start.
        container_id, created_engine_id = engine.create_worker(
            IMAGE_REF, run_id, 1, executor_id, incarnation, network,
            f"http://{broker_ip}:8123",
        )
        if created_engine_id != engine_id:
            raise AcceptanceError("created worker engine identity changed")
        result["container_id"] = container_id
        result["engine_id"] = engine_id
        before = inspect_container(container_id)
        if before["State"].get("Running") or not before["HostConfig"].get("ReadonlyRootfs"):
            raise AcceptanceError("created worker is not stopped or rootfs is writable")
        if before["HostConfig"].get("Tmpfs", {}).get("/run/scientist/bootstrap") is None:
            raise AcceptanceError("production bootstrap tmpfs mount is missing")
        result["stopped_container"] = {
            "running": before["State"].get("Running"),
            "user": before["Config"].get("User"),
            "tmpfs_mounts": sorted((before["HostConfig"].get("Tmpfs") or {}).keys()),
            "readonly_rootfs": before["HostConfig"].get("ReadonlyRootfs"),
        }

        engine.start_worker(container_id)
        # Production installs the namespace firewall immediately after start;
        # do so before installing bootstrap or inspecting the runtime user.
        policy = engine.install_network_policy(container_id, broker_ip, 8123)
        result["firewall_readback"] = bool(policy.get("readback"))
        if not result["firewall_readback"]:
            raise AcceptanceError("production firewall policy did not return readback evidence")
        readback_hashes = engine.install_bootstrap(container_id, bootstrap, token)
        result["bootstrap_uid_readback_sha256"] = readback_hashes

        deadline = time.monotonic() + 12
        while True:
            state = inspect_container(container_id)["State"]
            if state.get("Running"):
                break
            if time.monotonic() > deadline:
                raise AcceptanceError(f"production runtime exited before bootstrap check: {state.get('ExitCode')}")
            time.sleep(0.2)

        verify_code = r'''
import base64, hashlib, json, os, sys
import stat
from pathlib import Path
root=Path('/run/scientist/bootstrap')
context=json.loads((root/'context.json').read_text())
workspace=json.loads((root/'workspace.json').read_text())
metadata=json.loads((root/'metadata.json').read_text())
token=Path('/run/scientist/capability/token').read_bytes()
expected_token_sha, expected_payload_sha=sys.argv[1:3]
payload=base64.b64decode(workspace[0]['data_base64'], validate=True)
ready=Path('/run/scientist/readiness/ready')
workspace_dir=Path('/workspace')
paths=[root/'context.json',root/'workspace.json',root/'metadata.json',Path('/run/scientist/capability/token')]
file_stats={str(path):{'uid':path.stat().st_uid,'gid':path.stat().st_gid,'mode':stat.S_IMODE(path.stat().st_mode)} for path in paths}
report={'euid':os.geteuid(), 'run_id':context['run_id'], 'generation':context['generation'],
        'context_valid':context['schema_version']==1, 'metadata_valid':metadata=={'schema_version':1,'checkpoint_revision':0},
        'workspace_manifest_valid':len(workspace)==1 and workspace[0]['path']=='acceptance/input.txt',
        'capability_sha256':hashlib.sha256(token).hexdigest(), 'capability_length':len(token),
        'workspace_payload_sha256':hashlib.sha256(payload).hexdigest(),
        'bootstrap_file_stats':file_stats,
        'workspace_materialized_before_ready':(workspace_dir/'acceptance/input.txt').exists(),
        'readiness_marker_present':ready.exists(),
        'workspace_empty_before_ready':not any(workspace_dir.iterdir())}
print(json.dumps(report,sort_keys=True))
'''
        checked = exec_as_worker(container_id, verify_code, token_digest, payload_digest)
        if checked["euid"] != 65532 or checked["run_id"] != str(run_id) or checked["generation"] != 1:
            raise AcceptanceError(f"bootstrap identity or runtime UID mismatch: {checked}")
        if (not checked["context_valid"] or not checked["metadata_valid"]
                or not checked["workspace_manifest_valid"]
                or checked["capability_sha256"] != token_digest
                or checked["workspace_payload_sha256"] != payload_digest
                or checked["readiness_marker_present"]
                or checked["workspace_materialized_before_ready"]
                or not checked["workspace_empty_before_ready"]):
            raise AcceptanceError(f"started runtime bootstrap proof failed: {checked}")
        expected_stats = {
            "/run/scientist/bootstrap/context.json": {"uid": 0, "gid": 65532, "mode": 0o444},
            "/run/scientist/bootstrap/workspace.json": {"uid": 0, "gid": 65532, "mode": 0o444},
            "/run/scientist/bootstrap/metadata.json": {"uid": 0, "gid": 65532, "mode": 0o444},
            "/run/scientist/capability/token": {"uid": 0, "gid": 65532, "mode": 0o440},
        }
        if checked["bootstrap_file_stats"] != expected_stats:
            raise AcceptanceError(f"bootstrap ownership or mode mismatch: {checked['bootstrap_file_stats']}")
        result["runtime_uid_bootstrap_readback"] = checked

        exposure = inspect_container(container_id)
        env_names = [entry.partition("=")[0] for entry in exposure["Config"].get("Env", [])]
        sensitive_env = [name for name in env_names if re.search(
            r"SECRET|TOKEN|PASSWORD|CREDENTIAL|API_KEY|OPENAI|ANTHROPIC|AWS_|S3_|POSTGRES|DATABASE|DB_",
            name, re.IGNORECASE)]
        mounts = exposure.get("Mounts", [])
        non_tmpfs = [mount.get("Type") for mount in mounts if mount.get("Type") != "tmpfs"]
        port_bindings = exposure["HostConfig"].get("PortBindings") or {}
        if exposure["HostConfig"].get("Binds") or non_tmpfs or sensitive_env or port_bindings:
            raise AcceptanceError("worker unexpectedly has host mounts, secret env names or published ports")
        result["exposure"] = {
            "user": exposure["Config"].get("User"),
            "host_mount_types": non_tmpfs,
            "sensitive_environment_names": sensitive_env,
            "port_bindings": port_bindings,
            "declared_ports": list((exposure["Config"].get("ExposedPorts") or {}).keys()),
        }
        # Release only after policy, file hashes, UID, and exposure checks all pass.
        engine.release_worker(container_id)
        release_code = r'''
import hashlib, json, os, stat, sys
from pathlib import Path
ready=Path('/run/scientist/readiness/ready')
context=Path('/run/scientist/bootstrap/context.json')
workspace_manifest=Path('/run/scientist/bootstrap/workspace.json')
metadata=Path('/run/scientist/bootstrap/metadata.json')
token=Path('/run/scientist/capability/token')
payload=Path('/workspace/acceptance/input.txt')
expected_payload_sha, expected_token_sha=sys.argv[1:3]
ready_info=ready.stat()
protected={str(p):{'uid':p.stat().st_uid,'gid':p.stat().st_gid,'mode':stat.S_IMODE(p.stat().st_mode)}
           for p in (context,workspace_manifest,metadata,token)}
print(json.dumps({'euid':os.geteuid(), 'ready_present':ready.is_file(), 'ready_bytes':ready.read_bytes().decode(),
  'ready_uid':ready_info.st_uid,'ready_gid':ready_info.st_gid,'ready_mode':stat.S_IMODE(ready_info.st_mode),
  'payload_present':payload.is_file(), 'payload_sha256':hashlib.sha256(payload.read_bytes()).hexdigest(),
  'payload_uid':payload.stat().st_uid,'payload_gid':payload.stat().st_gid,'payload_mode':stat.S_IMODE(payload.stat().st_mode),
  'token_sha256':hashlib.sha256(token.read_bytes()).hexdigest(), 'protected_files':protected},sort_keys=True))
'''
        readiness = exec_as_worker(container_id, release_code, payload_digest, token_digest)
        expected_protected = {
            "/run/scientist/bootstrap/context.json": {"uid": 0, "gid": 65532, "mode": 0o444},
            "/run/scientist/bootstrap/workspace.json": {"uid": 0, "gid": 65532, "mode": 0o444},
            "/run/scientist/bootstrap/metadata.json": {"uid": 0, "gid": 65532, "mode": 0o444},
            "/run/scientist/capability/token": {"uid": 0, "gid": 65532, "mode": 0o440},
        }
        expected_release = {
            "euid": 65532, "ready_present": True, "ready_bytes": "ready",
            "ready_uid": 0, "ready_gid": 65532, "ready_mode": 0o440,
            "payload_present": True, "payload_sha256": payload_digest,
            "payload_uid": 65532, "payload_gid": 65532, "payload_mode": 0o600,
            "token_sha256": token_digest, "protected_files": expected_protected,
        }
        if readiness != expected_release:
            raise AcceptanceError(f"readiness, protected bootstrap, or allowed workspace delivery failed: {readiness}")
        result["readiness_and_delivery_readback"] = readiness
        result["readiness_released"] = True
        result["status"] = "pass"
        return 0
    except Exception as exc:
        result["status"] = "fail"
        result["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        cleanup_errors = []
        worker_absent = container_id is None
        network_absent = network is None
        try:
            try:
                if not container_id and engine_id:
                    candidates = docker(
                        "ps", "-aq", "--no-trunc",
                        "--filter", f"label=scientist.platform/run={run_id}",
                        "--filter", "label=scientist.platform/generation=1",
                        "--filter", f"label=scientist.platform/executor={executor_id}",
                    ).splitlines()
                    for candidate in candidates:
                        info = inspect_container(candidate)
                        labels = info["Config"].get("Labels") or {}
                        if (
                            candidate != info.get("Id")
                            or info.get("Image") != IMAGE_ID
                            or labels.get("scientist.platform/run") != str(run_id)
                            or labels.get("scientist.platform/generation") != "1"
                            or labels.get("scientist.platform/executor") != str(executor_id)
                            or labels.get("scientist.platform/incarnation") != str(incarnation)
                            or labels.get("scientist.platform/kind") != "worker"
                        ):
                            raise AcceptanceError("orphan worker identity did not match this harness")
                        container_id = candidate
                        result["container_id"] = candidate
                if container_id and engine_id:
                    ref = supervisor.ExecutorRef(executor_id, run_id, 1, "worker", None,
                                                 incarnation, engine_id, container_id)
                    if not engine.stop_worker(ref, 0):
                        raise AcceptanceError("exact worker cleanup could not prove stopped")
                    worker_absent = resource_is_absent("container", container_id)
                    if not worker_absent:
                        raise AcceptanceError("worker container remains after exact cleanup")
                    result["worker_cleanup"] = "exact identity removed and absence verified"
                elif container_id:
                    docker("rm", "--force", container_id, check=False)
                    worker_absent = resource_is_absent("container", container_id)
                    if not worker_absent:
                        raise AcceptanceError("worker container remains after forced exact cleanup")
            except Exception as cleanup_exc:
                cleanup_errors.append(f"worker: {type(cleanup_exc).__name__}: {cleanup_exc}")
            try:
                if network:
                    docker("network", "rm", network, check=False)
                    network_absent = resource_is_absent("network", network)
                    if not network_absent:
                        raise AcceptanceError("owned run network remains after cleanup")
            except Exception as cleanup_exc:
                cleanup_errors.append(f"network: {type(cleanup_exc).__name__}: {cleanup_exc}")
        finally:
            supervisor._config = original_config
            result["cleanup"] = {
                "exact": not cleanup_errors and worker_absent and network_absent,
                "worker_absent": worker_absent,
                "network_absent": network_absent,
                "errors": cleanup_errors,
            }
            if cleanup_errors or not worker_absent or not network_absent:
                result["status"] = "fail"
                if not result.get("failure"):
                    result["failure"] = "; ".join(cleanup_errors) or "owned resources remain"
            REPORT.parent.mkdir(parents=True, exist_ok=True)
            REPORT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            if cleanup_errors or not worker_absent or not network_absent:
                raise AcceptanceError(result["failure"])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
