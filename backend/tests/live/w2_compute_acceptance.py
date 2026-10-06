#!/usr/bin/env python3
"""Operator-run exact-image acceptance for the fixed offline CSV compute runtime.

This runner never builds, pulls, or substitutes an image. It creates two
containers from the supplied immutable image and reviewed recipe/input hashes:
one normal result readback, then one untouched guest SIGALRM/host-stop probe.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from scientist import compute_runtime
from scientist.compute_runtime import (
    create_compute,
    find_compute,
    poll_compute,
    read_compute_outputs,
    recipe_manifest_sha256,
    start_compute,
    stop_compute,
)
from scientist.runtime_contracts import ComputeLaunchSpec
from scientist.supervisor import DockerWorkerEngine, ExecutorRef


NOT_RUN = 77
_DATA = b"x,y\n1,2\n3,\n5,6\n"
_PARAMS = b'{"numeric_columns":["x","y"]}'
_IMAGE_RE = re.compile(r"sha256:[a-f0-9]{64}\Z")
_DIGEST_RE = re.compile(r"[a-f0-9]{64}\Z")
_ALARM_SECONDS = 30
_POLL_SECONDS = 0.25
_EXPECTED_STATS = {
    "schema_version": 1,
    "rows": 3,
    "columns": [
        {"name": "x", "valid": 3, "missing": 0, "min": 1, "max": 5,
         "mean": 3.0, "median": 3, "sample_sd": 2.0},
        {"name": "y", "valid": 2, "missing": 1, "min": 2, "max": 6,
         "mean": 4.0, "median": 4.0, "sample_sd": 2.8284271247461903},
    ],
}
_EXPECTED_OUTPUT_SHA256 = {
    "summary.json": "ef5dc95d5e181dbefbcce636891e41f6ecaa6da3cd0718aac988d74d1e29fbb4",
    "summary.csv": "6de30257e7864c36b34f434ca98e0967a1dccb43a4c0713acc42fb460ec3799f",
    "chart.svg": "6096f14c712a1d4d398256b5d6d9458ef336bf2a98996446d8a78321bc33eba5",
    "report.md": "0afd9ac9953fb38970ec36b26924c024367562a8582b6530b9288c88ddac1ddf",
}


class NotRun(RuntimeError):
    """A prerequisite is absent; no compute acceptance was attempted."""


class AcceptanceFailure(RuntimeError):
    """A required live assertion failed."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceFailure(message)


def _inspect(engine: DockerWorkerEngine, ref: ExecutorRef) -> dict[str, Any]:
    if not ref.container_id:
        raise AcceptanceFailure("container reference is not CID-bound")
    raw = compute_runtime._run_docker(engine, "inspect", ref.container_id)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AcceptanceFailure("Docker returned invalid container inspection") from exc
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise AcceptanceFailure("Docker inspection did not identify exactly one container")
    container = value[0]
    if container.get("Id") != ref.container_id:
        raise AcceptanceFailure("Docker inspection CID differs from the bound reference")
    return container


def _verify_containment(
    engine: DockerWorkerEngine,
    ref: ExecutorRef,
    spec: ComputeLaunchSpec,
    expected_engine_id: str,
) -> dict[str, Any]:
    """Pre-start gate: inspect the exact created container before permitting start."""
    container = _inspect(engine, ref)
    config = container.get("Config") or {}
    host = container.get("HostConfig") or {}
    state = container.get("State") or {}
    labels = config.get("Labels") or {}
    wanted_labels = {
        "scientist.platform/run": str(ref.run_id),
        "scientist.platform/generation": str(ref.generation),
        "scientist.platform/executor": str(ref.executor_id),
        "scientist.platform/kind": "compute",
        "scientist.platform/incarnation": str(ref.process_incarnation),
        "scientist.platform/operation": str(ref.operation_id),
        "scientist.platform/image-digest": spec.image_digest,
    }
    mounts = container.get("Mounts") or []
    by_destination = {mount.get("Destination"): mount for mount in mounts if isinstance(mount, dict)}
    recipe_mount = by_destination.get("/recipe") or {}
    input_mount = by_destination.get("/inputs") or {}
    tmpfs = host.get("Tmpfs") or {}
    tmpfs_value = tmpfs.get("/work", "")
    tmpfs_options = set(tmpfs_value.split(",")) if isinstance(tmpfs_value, str) else set()
    if "mode=700" in tmpfs_options:
        tmpfs_options.remove("mode=700")
        tmpfs_options.add("mode=0700")
    security_options = host.get("SecurityOpt") or []
    restart_policy = host.get("RestartPolicy") or {}
    checks = {
        "engine_id": engine.engine_id() == expected_engine_id == ref.engine_id,
        "immutable_image_id": container.get("Image") == spec.image_digest,
        "configured_image": config.get("Image") == spec.image_digest,
        "exact_labels": all(labels.get(key) == value for key, value in wanted_labels.items()),
        "created_not_running": state.get("Status") == "created" and state.get("Running") is False,
        "readonly_root": host.get("ReadonlyRootfs") is True,
        "unprivileged": host.get("Privileged") is False and config.get("User") == "65532:65532",
        "no_capabilities": "ALL" in (host.get("CapDrop") or []) and not host.get("CapAdd"),
        "closed_security_options": (
            len(security_options) == 1
            and security_options[0] in {"no-new-privileges", "no-new-privileges:true"}
        ),
        "no_network": host.get("NetworkMode") == "none" and not host.get("PortBindings"),
        "bounded_resources": (
            host.get("NanoCpus") == 1_000_000_000
            and host.get("Memory") == 1_073_741_824
            and host.get("MemorySwap") == 1_073_741_824
            and host.get("PidsLimit") == 128
        ),
        "fixed_tmpfs": (
            set(tmpfs) == {"/work"}
            and tmpfs_options == {
                "rw", "noexec", "nosuid", "nodev", "size=67108864", "mode=0700", "uid=65532", "gid=65532"
            }
        ),
        "no_restart_policy": restart_policy == {"Name": "no", "MaximumRetryCount": 0},
        "no_host_pid_namespace": host.get("PidMode", "") == "",
        "private_ipc_namespace": host.get("IpcMode", "private") in {"", "private"},
        "no_devices": all(
            host.get(key) in (None, []) for key in ("Devices", "DeviceRequests", "DeviceCgroupRules")
        ),
        "no_auto_remove": host.get("AutoRemove") is False,
        "reviewed_command": (
            config.get("Entrypoint") == ["python3.14"]
            and config.get("Cmd") == ["-I", "-S", "/recipe/csv_describe.py"]
        ),
        "readonly_recipe_mount": (
            recipe_mount.get("Type") == "bind"
            and Path(recipe_mount.get("Source", "")).resolve() == spec.recipe_directory.resolve()
            and recipe_mount.get("RW") is False
        ),
        "readonly_input_mount": (
            input_mount.get("Type") == "bind"
            and Path(input_mount.get("Source", "")).resolve() == spec.input_directory.resolve()
            and input_mount.get("RW") is False
        ),
        "exactly_two_mounts": len(mounts) == 2,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise AcceptanceFailure("actual container containment check failed: " + ", ".join(failed))
    return {
        "status": "PASS",
        "cid": ref.container_id,
        "run_id": str(ref.run_id),
        "generation": ref.generation,
        "executor_id": str(ref.executor_id),
        "incarnation": str(ref.process_incarnation),
        "operation_id": ref.operation_id,
        "image_id": container.get("Image"),
        "network_mode": host.get("NetworkMode"),
        "memory_bytes": host.get("Memory"),
        "memory_swap_bytes": host.get("MemorySwap"),
        "nano_cpus": host.get("NanoCpus"),
        "pids_limit": host.get("PidsLimit"),
        "mount_destinations": sorted(by_destination),
        "checks": checks,
    }


def _new_ref(engine_id: str) -> ExecutorRef:
    return ExecutorRef(
        executor_id=uuid4(), run_id=uuid4(), generation=1, kind="compute",
        operation_id=f"w2-compute-{uuid4().hex}", process_incarnation=uuid4(),
        engine_id=engine_id, container_id=None,  # type: ignore[arg-type]
    )


def _spec(image_digest: str, recipe: Path, inputs: Path, staging_root: Path, recipe_hash: str) -> ComputeLaunchSpec:
    return ComputeLaunchSpec(
        profile_id="prof.csv-stdlib@py3.14.7", profile_version="1",
        image_digest=image_digest, recipe_manifest_sha256=recipe_hash,
        recipe_directory=recipe, input_directory=inputs,
        output_directory=staging_root / "unused-parent-output",
    )


def _stop_exact(
    engine: DockerWorkerEngine,
    ref: ExecutorRef | None,
    image_digest: str,
    attempted_stops: set[str],
) -> bool:
    if ref is None:
        return False
    if not ref.container_id or ref.container_id in attempted_stops:
        raise AcceptanceFailure("exact compute stop was already attempted or CID is missing")
    attempted_stops.add(ref.container_id)
    stop_compute(engine, ref, expected_image_digest=image_digest)
    absent = find_compute(engine, ref, expected_image_digest=image_digest) is None
    _require(absent, "exact compute CID remains present after stop")
    return True


def _capture_failure_diagnostics(
    engine: DockerWorkerEngine,
    refs: list[ExecutorRef],
    phase: str,
    error: Exception,
) -> dict[str, Any]:
    """Capture bounded state and stdout/stderr for only the exact synthetic CIDs."""
    result: dict[str, Any] = {
        "phase": phase,
        "error_type": type(error).__name__,
        "error_message": str(error)[:1200],
        "containers": [],
    }
    for ref in refs:
        if not ref.container_id:
            continue
        item: dict[str, Any] = {"cid": ref.container_id}
        try:
            raw_state = compute_runtime._run_docker(
                engine, "inspect", "--format", "{{json .State}}", ref.container_id,
            )
            item["state"] = json.loads(raw_state)
        except Exception as diagnostic_error:
            item["state_error_type"] = type(diagnostic_error).__name__
        try:
            # The fixed guest reads only the public synthetic fixture. Never inspect
            # Config.Env or list containers to collect diagnostics.
            logs = compute_runtime._run_docker(engine, "logs", "--tail", "60", ref.container_id)
            item["log_tail"] = logs[-8000:]
        except Exception as diagnostic_error:
            item["log_error_type"] = type(diagnostic_error).__name__
        result["containers"].append(item)
    return result


def _create_bound(
    engine: DockerWorkerEngine,
    unbound: ExecutorRef,
    spec: ComputeLaunchSpec,
) -> ExecutorRef:
    try:
        return create_compute(engine, unbound, spec)
    except Exception:
        # A lost create acknowledgement is inspected once using only this durable
        # operation identity. No retry, label sweep, or prune fallback is allowed.
        recovered = find_compute(engine, unbound, expected_image_digest=spec.image_digest)
        if recovered is not None:
            try:
                _stop_exact(engine, recovered, spec.image_digest, set())
            except Exception as cleanup_error:
                raise AcceptanceFailure(
                    "create acknowledgement was unknown and exact recovered cleanup failed"
                ) from cleanup_error
        raise


def _reject_wrong_pin(engine: DockerWorkerEngine, ref: ExecutorRef, image_digest: str) -> dict[str, bool]:
    wrong_pin = "sha256:" + ("0" if image_digest[-1] != "0" else "1") * 64
    outcomes: dict[str, bool] = {}
    for name, operation in (
        ("poll", lambda: poll_compute(engine, ref, expected_image_digest=wrong_pin)),
        ("read", lambda: read_compute_outputs(engine, ref, expected_image_digest=wrong_pin)),
    ):
        try:
            operation()
        except RuntimeError:
            outcomes[name] = True
        else:
            outcomes[name] = False
    _require(all(outcomes.values()), "compute seam accepted the wrong approved-image pin")
    state = (_inspect(engine, ref).get("State") or {})
    _require(state.get("Status") == "created" and state.get("Running") is False,
             "wrong-pin rejection changed the created container state")
    return outcomes


def _validate_outputs(outputs: dict[str, bytes], expected_input_hash: str) -> dict[str, str]:
    wanted = {"summary.json", "summary.csv", "chart.svg", "report.md"}
    _require(set(outputs) == wanted, "compute returned a different output file set")
    try:
        summary = json.loads(outputs["summary.json"].decode("utf-8"))
        rows = list(csv.reader(outputs["summary.csv"].decode("utf-8").splitlines()))
        chart = ET.fromstring(outputs["chart.svg"].decode("utf-8"))
        report = outputs["report.md"].decode("utf-8")
    except (UnicodeError, ValueError, ET.ParseError, csv.Error) as exc:
        raise AcceptanceFailure("compute output format could not be read strictly") from exc
    _require(isinstance(summary, dict), "summary is not a JSON object")
    _require(summary.get("input_sha256") == expected_input_hash, "summary input hash differs")
    _require(summary.get("schema_version") == _EXPECTED_STATS["schema_version"], "summary schema version differs")
    _require(summary.get("rows") == _EXPECTED_STATS["rows"], "summary row count differs")
    columns = summary.get("columns")
    _require(isinstance(columns, list) and len(columns) == 2, "summary columns differ")
    for actual, expected in zip(columns, _EXPECTED_STATS["columns"], strict=True):
        _require(isinstance(actual, dict), "summary column is not an object")
        for key, value in expected.items():
            observed = actual.get(key)
            if isinstance(value, float):
                _require(type(observed) in (int, float) and abs(observed - value) < 1e-12,
                         f"summary statistic differs: {key}")
            else:
                _require(observed == value, f"summary field differs: {key}")
    _require(rows == [
        ["name", "valid", "missing", "min", "max", "mean", "median", "sample_sd"],
        ["x", "3", "0", "1.0", "5.0", "3.0", "3.0", "2.0"],
        ["y", "2", "1", "2.0", "6.0", "4.0", "4.0", "2.8284271247461903"],
    ], "summary CSV values differ from the known fixture result")
    _require(chart.tag == "{http://www.w3.org/2000/svg}svg", "chart is not SVG")
    _require("Descriptive summary" in report and "Rows: 3" in report,
             "report does not describe the known fixture result")
    hashes = {name: _sha256(data) for name, data in outputs.items()}
    _require(hashes == _EXPECTED_OUTPUT_SHA256,
             "compute output hashes differ from the approved fixture result")
    return hashes


def _run_ready_with_pin(engine: DockerWorkerEngine, ref: ExecutorRef, image_digest: str) -> tuple[int, float]:
    started = time.monotonic()
    # Leave margin before poll_compute's own 30-second stop path so a failed
    # readiness probe is cleaned up by this runner exactly once.
    deadline = started + _ALARM_SECONDS - 3
    while time.monotonic() < deadline:
        result = poll_compute(engine, ref, expected_image_digest=image_digest)
        if result is not None:
            return result, time.monotonic() - started
        time.sleep(_POLL_SECONDS)
    raise AcceptanceFailure("compute did not become result-ready within the guest wall limit")


def _wait_for_guest_alarm(
    engine: DockerWorkerEngine,
    ref: ExecutorRef,
    started_monotonic: float,
) -> tuple[dict[str, Any], float]:
    deadline = started_monotonic + _ALARM_SECONDS + 5
    last = _inspect(engine, ref)
    while time.monotonic() < deadline:
        state = last.get("State") or {}
        if state.get("Running") is False and state.get("Status") in {"exited", "dead"}:
            elapsed = time.monotonic() - started_monotonic
            return state, elapsed
        time.sleep(_POLL_SECONDS)
        last = _inspect(engine, ref)
    raise AcceptanceFailure("guest did not exit on its original 30-second SIGALRM")


def _write_evidence(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def _preflight(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    if not _IMAGE_RE.fullmatch(args.image_digest):
        raise NotRun("image digest must be sha256:<64 lowercase hex>")
    if not _DIGEST_RE.fullmatch(args.recipe_manifest_sha256):
        raise NotRun("recipe manifest hash must be 64 lowercase hex characters")
    if not _DIGEST_RE.fullmatch(args.expected_input_sha256):
        raise NotRun("input hash must be 64 lowercase hex characters")
    if args.engine_context != compute_runtime._DOCKER_CONTEXT:
        raise NotRun("engine context differs from the compute runtime's owned context")
    if not args.expected_engine_id or len(args.expected_engine_id) > 200:
        raise NotRun("expected engine ID is missing or invalid")
    if _sha256(_DATA) != args.expected_input_sha256:
        raise NotRun("expected input hash does not match the fixed known fixture")
    recipe = Path(args.recipe_dir)
    staging_root = Path(args.staging_root)
    evidence = Path(args.evidence)
    if not recipe.is_absolute() or not staging_root.is_absolute() or not evidence.is_absolute():
        raise NotRun("recipe, staging, and evidence paths must be absolute")
    if recipe.is_symlink() or not recipe.is_dir():
        raise NotRun("recipe directory must be a real directory")
    if staging_root.is_symlink() or not staging_root.is_dir():
        raise NotRun("staging root must be a real existing directory")
    for path in (recipe, staging_root):
        if ".." in path.parts or any(ord(c) < 32 or ord(c) == 127 for c in str(path)):
            raise NotRun("recipe and staging paths must be canonical and control-free")
    if not (recipe.stat().st_mode & 0o005) == 0o005:
        raise NotRun("recipe directory must be searchable and readable by the guest UID")
    for name in ("csv_describe.py", "cpu_recipes.py", "scientific_render.py"):
        source = recipe / name
        if not source.is_file() or not source.stat().st_mode & 0o004:
            raise NotRun("reviewed recipe files must be readable by the guest UID")
    try:
        actual_recipe_hash = recipe_manifest_sha256(recipe)
    except (OSError, ValueError) as exc:
        raise NotRun("supplied recipe directory does not match the closed source set") from exc
    if actual_recipe_hash != args.recipe_manifest_sha256:
        raise NotRun("recipe sources differ from the approved manifest hash")
    if not evidence.parent.is_dir() or evidence.exists() or evidence.is_symlink():
        raise NotRun("evidence output parent must exist and output path must be unused")
    if ".." in evidence.parts:
        raise NotRun("evidence path must be canonical")
    if "," in str(recipe) or "," in str(staging_root):
        raise NotRun("bind-mount paths cannot contain commas")
    return recipe, staging_root, evidence


def run_acceptance(
    *,
    engine: DockerWorkerEngine,
    expected_engine_id: str,
    image_digest: str,
    recipe_directory: Path,
    recipe_manifest_hash: str,
    staging_root: Path,
    expected_input_hash: str,
    before_start_gate: Callable[[DockerWorkerEngine, ExecutorRef, ComputeLaunchSpec], dict[str, Any]],
) -> dict[str, Any]:
    """Run the normal result and natural guest-alarm cases through real Docker."""
    engine_id = engine.engine_id()
    _require(engine.context == compute_runtime._DOCKER_CONTEXT,
             "engine context is outside the compute runtime's owned context")
    _require(engine_id == expected_engine_id, "owned Docker engine ID differs from supplied pin")
    input_dir = staging_root / f"w2-compute-input-{uuid4().hex}"
    input_dir.mkdir(mode=0o700)
    (input_dir / "data.csv").write_bytes(_DATA)
    (input_dir / "params.json").write_bytes(_PARAMS)
    for path in input_dir.iterdir():
        path.chmod(0o444)
    input_dir.chmod(0o555)
    spec = _spec(image_digest, recipe_directory, input_dir, staging_root, recipe_manifest_hash)
    base: dict[str, Any] = {
        "schema_version": 1,
        "scope": "standalone exact-image offline CSV compute runtime acceptance",
        "network": "none",
        "engine_context": engine.context,
        "engine_id": engine_id,
        "image_digest": image_digest,
        "recipe_manifest_sha256": recipe_manifest_hash,
        "input_sha256": expected_input_hash,
        "expected_guest_alarm_seconds": _ALARM_SECONDS,
        "paid_calls": 0,
        "external_requests": 0,
        "durable_journal_coverage": False,
    }
    refs: list[ExecutorRef] = []
    attempted_stops: set[str] = set()
    phase = "normal_create"
    base["normal_case"] = {"status": "IN PROGRESS"}
    base["guest_alarm_case"] = {"status": "NOT STARTED"}
    try:
        # Normal compute: exact pre-start callback, strict wrong-pin rejects,
        # known values and output hashes, then exact physical stop.
        phase = "normal_create"
        normal = _new_ref(engine_id)
        bound = _create_bound(engine, normal, spec)
        refs.append(bound)
        phase = "normal_pre_start_gate"
        normal_containment = before_start_gate(engine, bound, spec)
        base["normal_case"] = {"cid": bound.container_id, "containment": normal_containment}
        phase = "normal_wrong_pin_rejections"
        wrong_pin = _reject_wrong_pin(engine, bound, image_digest)
        base["normal_case"]["wrong_pin_rejections"] = wrong_pin
        normal_started = time.monotonic()
        phase = "normal_start"
        start_compute(engine, bound, expected_image_digest=image_digest)
        phase = "normal_poll_ready"
        ready_status, _ = _run_ready_with_pin(engine, bound, image_digest)
        _require(ready_status == 0, "normal compute did not publish its ready result")
        phase = "normal_read_outputs"
        normal_outputs = read_compute_outputs(engine, bound, expected_image_digest=image_digest)
        phase = "normal_validate_outputs"
        normal_hashes = _validate_outputs(normal_outputs, expected_input_hash)
        normal_elapsed = time.monotonic() - normal_started
        phase = "normal_exact_stop"
        normal_stopped = _stop_exact(engine, bound, image_digest, attempted_stops)
        refs.remove(bound)
        base["normal_case"].update({
            "result_ready": True,
            "elapsed_seconds": round(normal_elapsed, 3),
            "output_sha256": normal_hashes,
            "exact_stop_and_absence": normal_stopped,
            "status": "PASS",
        })

        # Alarm probe: same immutable source and fixture. Observe readiness, then
        # make no guest/host control call for >30 seconds so PID 1's original
        # SIGALRM, rather than the host poll timeout, ends the process.
        phase = "alarm_create"
        alarm_unbound = _new_ref(engine_id)
        alarm_ref = _create_bound(engine, alarm_unbound, spec)
        refs.append(alarm_ref)
        phase = "alarm_pre_start_gate"
        alarm_containment = before_start_gate(engine, alarm_ref, spec)
        base["guest_alarm_case"] = {"cid": alarm_ref.container_id, "containment": alarm_containment}
        alarm_started = time.monotonic()
        phase = "alarm_start"
        start_compute(engine, alarm_ref, expected_image_digest=image_digest)
        phase = "alarm_poll_ready"
        ready_status, ready_elapsed = _run_ready_with_pin(engine, alarm_ref, image_digest)
        _require(ready_status == 0, "alarm probe did not reach result-ready before SIGALRM")
        phase = "alarm_wait_guest_sigalrm"
        state, alarm_elapsed = _wait_for_guest_alarm(engine, alarm_ref, alarm_started)
        exit_code = state.get("ExitCode")
        _require(exit_code == 142, "guest did not exit with SIGALRM status 142")
        phase = "alarm_exact_stop"
        alarm_stopped = _stop_exact(engine, alarm_ref, image_digest, attempted_stops)
        refs.remove(alarm_ref)
        base["guest_alarm_case"].update({
            "result_ready_before_wait": True,
            "ready_elapsed_seconds": round(ready_elapsed, 3),
            "guest_exit_status": state.get("Status"),
            "guest_exit_code": exit_code,
            "elapsed_since_start_seconds": round(alarm_elapsed, 3),
            "guest_alarm_30s_observed": alarm_elapsed >= _ALARM_SECONDS,
            "exact_stop_and_absence": alarm_stopped,
        })

        input_dir.chmod(0o700)
        shutil.rmtree(input_dir)
        _require(not input_dir.exists(), "synthetic fixture staging directory remains")
        base.update({
            "status": "PASS",
        })
        _require(base["guest_alarm_case"]["guest_alarm_30s_observed"],
                 "guest alarm exited before the 30-second boundary")
        return base
    except Exception as exc:
        base["failure"] = _capture_failure_diagnostics(engine, refs, phase, exc)
        cleanup_errors = []
        for ref in reversed(refs):
            try:
                if ref.container_id and ref.container_id not in attempted_stops:
                    _stop_exact(engine, ref, image_digest, attempted_stops)
            except Exception as cleanup_error:
                cleanup_errors.append(type(cleanup_error).__name__)
        base["status"] = "FAIL"
        base["cleanup_errors"] = cleanup_errors
        base["fixture_staging_retained"] = input_dir.exists()
        return base


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-context", required=True)
    parser.add_argument("--expected-engine-id", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--recipe-dir", required=True)
    parser.add_argument("--recipe-manifest-sha256", required=True)
    parser.add_argument("--expected-input-sha256", required=True)
    parser.add_argument("--staging-root", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--validate-only", action="store_true",
                        help="validate supplied hashes and paths without contacting Docker")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    evidence_path: Path | None = None
    evidence: dict[str, Any] = {}
    try:
        recipe, staging_root, evidence_path = _preflight(args)
        if args.validate_only:
            print(json.dumps({"status": "VALID", "docker_contacted": False,
                              "recipe_manifest_sha256": args.recipe_manifest_sha256,
                              "input_sha256": args.expected_input_sha256}, sort_keys=True))
            return 0
        engine = DockerWorkerEngine(context=args.engine_context)
        engine_id = engine.engine_id()
        if engine_id != args.expected_engine_id:
            raise NotRun("owned engine ID differs from the supplied expectation")
        evidence = run_acceptance(
            engine=engine,
            expected_engine_id=args.expected_engine_id,
            image_digest=args.image_digest,
            recipe_directory=recipe,
            recipe_manifest_hash=args.recipe_manifest_sha256,
            staging_root=staging_root,
            expected_input_hash=args.expected_input_sha256,
            before_start_gate=lambda e, r, s: _verify_containment(
                e, r, s, args.expected_engine_id,
            ),
        )
        _write_evidence(evidence_path, evidence)
        print(json.dumps({"status": evidence["status"], "evidence": str(evidence_path)}, sort_keys=True))
        return 0 if evidence["status"] == "PASS" else 1
    except NotRun as exc:
        evidence = {"schema_version": 1, "status": "NOT RUN", "reason": str(exc)}
        if evidence_path is not None and not evidence_path.exists():
            _write_evidence(evidence_path, evidence)
        print(json.dumps(evidence, sort_keys=True))
        return NOT_RUN
    except Exception as exc:
        evidence = {"schema_version": 1, "status": "FAIL", "error_type": type(exc).__name__}
        if evidence_path is not None and not evidence_path.exists():
            _write_evidence(evidence_path, evidence)
        print(json.dumps(evidence, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
