#!/usr/bin/env python3
"""Opt-in, read-only scientific-result check across owned PG/MinIO restarts."""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID
from uuid import UUID

NOT_RUN = 77
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROOF = ROOT / ".local/scientific-w1-20261006/evidence-round14/scientific_host_http_proof.json"
SERVICES = (
    ("scientist-b5-postgres", "postgres_container_id", "postgres_image", "scientist-b5-pgdata", "/var/lib/postgresql"),
    ("scientist-b5-minio", "minio_container_id", "minio_image", "scientist-b5-minio-data", "/data"),
)
MAX_OBJECT_BYTES = 64 * 1024 * 1024
EXPECTED_DOCKER_CONTEXT = "colima-scientist-platform-test"
EXPECTED_DOCKER_VERSION = "29.8.2"
EXPECTED_ENGINE_ID = "e3285329-4f64-4c8c-a566-40a098af03da"
EXPECTED_RUN_ID = "42fede76-9a92-4b08-9a58-41fc1a0c96e7"
EXPECTED_PROJECT_ID = "4fca562e-71be-4190-8db7-8992b2173e47"
EXPECTED_ARTIFACT_ID = "db6aaaa1-80e3-4eca-9420-5ff1e5fdf01c"
EXPECTED_ARTIFACT_SHA256 = "30542dd2903c038a7338f3eab6f922657d68428af50aae8ca432e32dd34c4291"
EXPECTED_ARTIFACT_SIZE = 332
EXPECTED_SERVICE_IDS = {
    "scientist-b5-postgres": "728ae5444bb3398bb9b4a5a16f4301ee14f363f4a6ae58e4ba7f3b2361ecc893",
    "scientist-b5-minio": "ce1520e0397f5bfbac9c9635299383f9d1761a215e8b99d0cfa997baafa62556",
}
ACTIVE_OPERATION_STATES = {"pending", "reserved", "running", "submitted", "unknown"}
ACTIVE_STEP = "preflight"
RUN_PROGRESS: dict[str, Any] = {"service_restarts": {}}


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"bytes_sha256": hashlib.sha256(bytes(value)).hexdigest(), "size": len(value)}
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "hex") and value.__class__.__name__ == "UUID":
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def snapshot_hash(value: Any) -> str:
    encoded = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _covers(path: str, target: str) -> bool:
    try:
        return posixpath.commonpath((posixpath.normpath(path), posixpath.normpath(target))) == posixpath.normpath(path)
    except ValueError:
        return False


def validate_engine(actual: str, expected: str) -> None:
    if not actual or actual != expected:
        raise ValueError("engine_identity_mismatch")


def validate_service_binding(proof_ids: dict[str, str], config_ids: dict[str, str]) -> None:
    if proof_ids != EXPECTED_SERVICE_IDS or config_ids != EXPECTED_SERVICE_IDS:
        raise ValueError("service_container_identity_mismatch")


def validate_executor_bindings(executors: list[dict[str, Any]]) -> None:
    for row in executors:
        container_id = row.get("container_id")
        if not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", container_id) or row.get("engine_id") != EXPECTED_ENGINE_ID:
            raise ValueError("target_executor_binding_mismatch")


def _config_digest(info: dict[str, Any]) -> str:
    config = {key: info.get(key) for key in ("Config", "HostConfig", "Mounts")}
    return snapshot_hash(config)


def validate_container(
    info: dict[str, Any], *, expected_id: str, expected_name: str,
    expected_image: str, volume_name: str, data_path: str,
) -> dict[str, str]:
    if info.get("Id") != expected_id:
        raise ValueError("container_identity_mismatch")
    if info.get("Name", "").lstrip("/") != expected_name:
        raise ValueError("container_name_mismatch")
    if (info.get("Config") or {}).get("Image") != expected_image:
        raise ValueError("container_image_mismatch")
    if not info.get("Image"):
        raise ValueError("container_image_id_missing")
    labels = (info.get("Config") or {}).get("Labels") or {}
    if labels.get("scientist.platform/purpose") != "b5-services-test":
        raise ValueError("container_purpose_label_mismatch")
    state = info.get("State") or {}
    started = state.get("StartedAt")
    if state.get("Running") is not True or not isinstance(started, str) or not started:
        raise ValueError("container_not_running")
    tmpfs = (info.get("HostConfig") or {}).get("Tmpfs") or {}
    if any(_covers(path, data_path) or _covers(data_path, path) for path in tmpfs):
        raise ValueError("data_path_is_tmpfs")
    mounts = [m for m in info.get("Mounts", []) if m.get("Destination") == data_path]
    if len(mounts) != 1:
        raise ValueError("durable_mount_missing_or_ambiguous")
    mount = mounts[0]
    if mount.get("Type") != "volume" or mount.get("Name") != volume_name or mount.get("RW") is not True:
        raise ValueError("durable_volume_mismatch")
    pgdata = next((item.partition("=")[2] for item in (info.get("Config") or {}).get("Env", []) if item.startswith("PGDATA=")), data_path)
    if not _covers(data_path, pgdata):
        raise ValueError("database_data_path_not_covered")
    if any(m is not mount and _covers(m.get("Destination", "/"), pgdata)
           or m is not mount and _covers(pgdata, m.get("Destination", "/"))
           for m in info.get("Mounts", [])):
        raise ValueError("data_path_shadowed_by_mount")
    if any(_covers(path, pgdata) or _covers(pgdata, path) for path in tmpfs):
        raise ValueError("data_path_shadowed_by_tmpfs")
    return {"id": expected_id, "started_at": started, "image": expected_image, "image_id": info["Image"], "volume": volume_name, "mount_source": mount.get("Source", ""), "config_sha256": _config_digest(info)}


def validate_restart(before: dict[str, str], after: dict[str, str]) -> None:
    if any(before[key] != after[key] for key in ("id", "image", "image_id", "volume", "mount_source", "config_sha256", "volume_config_sha256")):
        raise ValueError("container_replaced_or_reconfigured")
    if _started_at(after["started_at"]) <= _started_at(before["started_at"]):
        raise ValueError("container_start_time_not_increased")


def validate_volume(info: dict[str, Any], *, expected_name: str, mount_source: str) -> dict[str, str]:
    labels = info.get("Labels") or {}
    options = info.get("Options") or {}
    mountpoint = info.get("Mountpoint")
    if (info.get("Name") != expected_name or info.get("Driver") != "local" or not mountpoint
            or str(options.get("type", "")).lower() == "tmpfs"
            or labels.get("scientist.platform/purpose") != "b5-services-test"
            or posixpath.normpath(mountpoint) != posixpath.normpath(mount_source)):
        raise ValueError("durable_volume_unverified")
    return {"name": expected_name, "mountpoint": mountpoint,
            "config_sha256": snapshot_hash({key: info.get(key) for key in ("Driver", "Options", "Labels", "Scope", "Mountpoint")})}


def _started_at(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)", value)
    if not match:
        raise ValueError("container_start_time_invalid")
    timestamp = datetime.fromisoformat(match.group(1) + ("+00:00" if match.group(3) == "Z" else match.group(3)))
    return calendar.timegm(timestamp.utctimetuple()), int((match.group(2) or "").ljust(9, "0"))


def self_check() -> None:
    digest = "a" * 64
    base = {
        "Id": digest,
        "Image": "sha256:" + "c" * 64,
        "Name": "/scientist-b5-postgres",
        "State": {"Running": True, "StartedAt": "2026-10-06T00:00:00Z"},
        "HostConfig": {"Tmpfs": {}},
        "Config": {"Image": f"postgres@sha256:{digest}", "Labels": {"scientist.platform/purpose": "b5-services-test"}, "Env": ["PGDATA=/var/lib/postgresql/18/docker"]},
        "Mounts": [{"Type": "volume", "Name": "scientist-b5-pgdata", "Destination": "/var/lib/postgresql", "RW": True}],
    }
    expected = dict(expected_id=digest, expected_name="scientist-b5-postgres", expected_image=f"postgres@sha256:{digest}", volume_name="scientist-b5-pgdata", data_path="/var/lib/postgresql")
    accepted = validate_container(base, **expected)
    accepted["volume_config_sha256"] = "d" * 64
    validate_engine("engine", "engine")
    validate_service_binding(EXPECTED_SERVICE_IDS, EXPECTED_SERVICE_IDS)
    try:
        validate_executor_bindings([{"container_id": None, "engine_id": EXPECTED_ENGINE_ID}])
    except ValueError:
        pass
    else:
        raise AssertionError("unbound executor accepted")
    restarted = {**accepted, "started_at": "2026-10-06T00:01:00Z"}
    validate_restart(accepted, restarted)
    validate_restart(accepted, {**accepted, "started_at": "2026-10-06T00:00:00.000000001Z"})
    for mutate, kwargs in (
        (lambda x: x.update(Id="b" * 64), {}),
        (lambda x: x["HostConfig"].update(Tmpfs={"/var/lib/postgresql/data": "rw"}), {}),
        (lambda x: x["Mounts"][0].update(Name="wrong-volume"), {}),
        (lambda x: x["Config"].update(Image="wrong-image"), {}),
        (lambda x: x["Mounts"].append({"Type": "tmpfs", "Destination": "/var/lib/postgresql/18/docker"}), {}),
    ):
        bad = json.loads(json.dumps(base))
        mutate(bad)
        try:
            validate_container(bad, **expected)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid container preflight accepted")
    try:
        validate_engine("wrong", "engine")
    except ValueError:
        pass
    else:
        raise AssertionError("wrong engine accepted")
    try:
        validate_service_binding({**EXPECTED_SERVICE_IDS, "scientist-b5-minio": "wrong"}, EXPECTED_SERVICE_IDS)
    except ValueError:
        pass
    else:
        raise AssertionError("service IDs not bound to proof")
    try:
        validate_restart(accepted, accepted)
    except ValueError:
        pass
    else:
        raise AssertionError("unchanged container start time accepted")
    try:
        validate_restart(accepted, {**accepted, "started_at": "2026-10-05T23:59:59Z"})
    except ValueError:
        pass
    else:
        raise AssertionError("earlier container start time accepted")
    try:
        validate_restart(accepted, {**accepted, "started_at": "invalid"})
    except ValueError:
        pass
    else:
        raise AssertionError("invalid container start time accepted")
    try:
        validate_restart(accepted, {**restarted, "config_sha256": "b" * 64})
    except ValueError:
        pass
    else:
        raise AssertionError("changed container config accepted")
    if snapshot_hash({"operations": [{"state": "committed"}]}) == snapshot_hash({"operations": [{"state": "pending"}]}):
        raise AssertionError("database snapshot mutation was not detected")
    print(json.dumps({"status": "PASS", "mode": "self-check", "checks": 18}, sort_keys=True))


def _write_report(directory: Path, report: dict[str, Any]) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    path = directory / "scientific-storage-restart.json"
    payload = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _update_report(path: Path, report: dict[str, Any]) -> None:
    payload = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _docker(h: Any, *args: str) -> str:
    return h.docker(*args)


def _inspect(h: Any, container_id: str) -> dict[str, Any]:
    return json.loads(_docker(h, "inspect", container_id))[0]


def _read_objects(h: Any, rows: list[dict[str, Any]], bucket: str) -> dict[str, dict[str, Any]]:
    result = {}
    for row in rows:
        key, expected_hash, size = row["key"], str(row["sha256"]).strip(), int(row["size"])
        if size < 0 or size > MAX_OBJECT_BYTES or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("object_metadata_out_of_bounds")
        response = h.s3.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        try:
            data = body.read(size + 1)
        finally:
            body.close()
        actual = hashlib.sha256(data).hexdigest()
        if len(data) != size or actual != expected_hash:
            raise ValueError("object_bytes_mismatch")
        result[key] = {"sha256": actual, "size": size}
    return result


def _capture(h: Any, run_id: str, proof: dict[str, Any], bucket: str) -> dict[str, Any]:
    from sqlalchemy import text
    from scientist.db import session

    def query(db: Any, sql: str, **params: Any) -> list[dict[str, Any]]:
        bindings = {"run": UUID(run_id)} if ":run" in sql else {}
        bindings.update(params)
        return [dict(row) for row in db.execute(text(sql), bindings).mappings()]

    with session() as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        runs = query(db, "SELECT * FROM runs WHERE id=:run")
        if len(runs) != 1:
            raise ValueError("target_run_missing_or_ambiguous")
        run = runs[0]
        project_id = run["project_id"]
        if str(run["id"]) != run_id or run.get("state") != "completed" or int(run.get("reserved_tokens", -1)) != 0:
            raise ValueError("target_run_not_quiescent")
        if str(proof.get("project_id")) != str(project_id):
            raise ValueError("source_proof_project_mismatch")
        operations = query(db, "SELECT * FROM operations WHERE run_id=:run ORDER BY created_at,operation_id")
        if any(str(row.get("state", "")).lower() in ACTIVE_OPERATION_STATES for row in operations):
            raise ValueError("target_operation_active")
        executors = query(db, "SELECT * FROM runtime_executors WHERE run_id=:run ORDER BY generation,kind")
        if not executors or any(str(row.get("state", "")).lower() != "inactive" for row in executors):
            raise ValueError("target_executor_not_inactive")
        validate_executor_bindings(executors)
        checkpoints = query(db, "SELECT * FROM checkpoints WHERE run_id=:run ORDER BY revision,id")
        boundaries = query(db, "SELECT * FROM checkpoint_boundaries WHERE run_id=:run ORDER BY checkpoint_id,boundary_id")
        receipts = query(db, "SELECT * FROM scientific_artifact_receipts WHERE run_id=:run ORDER BY artifact_id")
        if len(receipts) != 1 or str(receipts[0].get("artifact_id")) != str(proof["artifact_id"]):
            raise ValueError("target_artifact_receipt_mismatch")
        artifacts = query(db, "SELECT * FROM artifacts WHERE run_id=:run ORDER BY id")
        matching_artifacts = [row for row in artifacts if str(row.get("id")) == str(proof["artifact_id"])
                              and str(row.get("sha256", "")).strip() == proof["artifact_sha256"]
                              and int(row.get("size", -1)) == int(proof["artifact_size"])]
        if len(matching_artifacts) != 1:
            raise ValueError("target_artifact_row_mismatch")
        objects = query(db, "SELECT key,sha256,size FROM stored_objects WHERE project_id=:project AND sha256=:sha", project=project_id, sha=proof["artifact_sha256"])
        if len(objects) != 1 or int(objects[0]["size"]) != int(proof["artifact_size"]):
            raise ValueError("target_artifact_object_mismatch")
        llm_result_refs = []
        llm_result_object_rows = []
        for operation in operations:
            if str(operation.get("kind", "")).lower() != "llm" or str(operation.get("state", "")).lower() != "committed":
                continue
            result = operation.get("result")
            result_ref = result.get("ref") if isinstance(result, dict) else None
            if (
                not isinstance(result_ref, dict)
                or str(result_ref.get("project_id")) != str(project_id)
                or not isinstance(result_ref.get("key"), str)
                or not result_ref["key"]
                or not isinstance(result_ref.get("sha256"), str)
                or not re.fullmatch(r"[a-fA-F0-9]{64}", result_ref["sha256"])
                or type(result_ref.get("size")) is not int
                or result_ref["size"] < 0
                or not isinstance(result_ref.get("content_type"), str)
                or not result_ref["content_type"]
            ):
                raise ValueError("committed_llm_result_reference_invalid")
            stored_rows = query(
                db,
                "SELECT key,sha256,size,content_type FROM stored_objects WHERE project_id=:project AND key=:key",
                project=project_id,
                key=result_ref["key"],
            )
            if (
                len(stored_rows) != 1
                or stored_rows[0]["key"] != result_ref["key"]
                or str(stored_rows[0]["sha256"]).strip() != result_ref["sha256"].lower()
                or int(stored_rows[0]["size"]) != result_ref["size"]
                or stored_rows[0]["content_type"] != result_ref["content_type"]
            ):
                raise ValueError("committed_llm_stored_object_mismatch")
            llm_result_refs.append(result_ref)
            llm_result_object_rows.extend(stored_rows)
        events = query(db, "SELECT * FROM events WHERE run_id=:run ORDER BY sequence")
        table_names = ("runs", "operations", "runtime_executors", "checkpoints", "checkpoint_boundaries", "events", "artifacts", "scientific_artifact_receipts", "stored_objects")
        counts = {name: int(query(db, f"SELECT count(*) AS n FROM {name}")[0]["n"]) for name in table_names}
    from scientist import db as scientist_db
    scientist_db.engine().dispose()
    target = {
        "run": run,
        "operations": operations,
        "executors": executors,
        "checkpoints": checkpoints,
        "boundaries": boundaries,
        "events": events,
            "artifacts": artifacts,
            "receipts": receipts,
            "artifact_object_rows": objects,
            "llm_result_object_rows": llm_result_object_rows,
        }
    refs = list(objects)
    for checkpoint in checkpoints:
        manifest = checkpoint.get("manifest") or {}
        for ref in [manifest.get("context"), *(manifest.get("workspace") or [])]:
            if ref:
                refs.append({"key": ref["key"], "sha256": ref["sha256"], "size": ref["size"]})
    for result_ref in llm_result_refs:
        refs.append({"key": result_ref["key"], "sha256": result_ref["sha256"], "size": result_ref["size"]})
    unique = {row["key"]: row for row in refs}
    if len(unique) != len(refs):
        for ref in refs:
            prior = unique[ref["key"]]
            if (str(prior["sha256"]).strip(), int(prior["size"])) != (str(ref["sha256"]).strip(), int(ref["size"])):
                raise ValueError("object_reference_conflict")
    object_state = _read_objects(h, list(unique.values()), bucket)
    container_ids = sorted({str(row["container_id"]) for row in executors if row.get("container_id")})
    if not container_ids:
        raise ValueError("target_executor_container_missing")
    return {
        "database_sha256": snapshot_hash(target),
        "counts": counts,
        "objects": object_state,
        "run_state": run["state"],
        "generation": int(run["generation"]),
        "operation_count": len(operations),
        "checkpoint_count": len(checkpoints),
        "boundary_count": len(boundaries),
        "receipt_count": len(receipts),
        "artifact_count": len(artifacts),
        "executor_container_ids": container_ids,
    }


def _assert_absent_containers(h: Any, container_ids: list[str]) -> None:
    for container_id in container_ids:
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise ValueError("target_executor_container_id_invalid")
        found = _docker(h, "ps", "--all", "--no-trunc", "--filter", f"id={container_id}", "--format", "{{.ID}}")
        if any(line.strip() == container_id for line in found.splitlines()):
            raise ValueError("target_executor_container_still_present")


def _postgres_ready() -> None:
    import psycopg
    from sqlalchemy.engine import make_url
    from scientist.settings import DATABASE_URL

    conninfo = make_url(DATABASE_URL).set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(conninfo, connect_timeout=3, options="-c statement_timeout=2000") as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            if conn.execute("SELECT 1").fetchone()[0] != 1:
                raise RuntimeError("postgres_readiness_invalid")


def _restart_one(h: Any, spec: tuple[str, str, str, str, str], cfg: Any, before: dict[str, str]) -> dict[str, str]:
    name, _, _, _, _ = spec
    completed = subprocess.run(
        ["rtk", "proxy", "docker", "--context", cfg.docker_context, "restart", "--time", "20", before["id"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=240,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"restart_failed_{name}")
    after_info = _inspect(h, before["id"])
    after = validate_container(
        after_info,
        expected_id=before["id"],
        expected_name=name,
        expected_image=before["image"],
        volume_name=before["volume"],
        data_path=spec[4],
    )
    volume_info = json.loads(_docker(h, "volume", "inspect", before["volume"]))[0]
    volume_state = validate_volume(volume_info, expected_name=before["volume"], mount_source=after["mount_source"])
    after["volume_config_sha256"] = volume_state["config_sha256"]
    validate_restart(before, after)
    return after


def _run(proof_path: Path, allow_restart: bool) -> dict[str, Any]:
    global ACTIVE_STEP, RUN_PROGRESS
    RUN_PROGRESS = {"service_restarts": {}}
    ACTIVE_STEP = "config"
    if not allow_restart:
        raise ValueError("explicit_restart_flag_required")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import scientific_host_http_acceptance as http  # noqa: PLC0415

    extension, _ = http.load_config()
    import b5_matrix_common as common  # noqa: PLC0415
    common.BUCKET = extension["scientific_bucket"]

    proof = json.loads(proof_path.read_text())
    if proof.get("status") != "PASS" or proof.get("final_state") != "completed":
        raise ValueError("source_proof_not_passed")
    run_id = str(proof["run_id"])
    if (run_id != EXPECTED_RUN_ID or proof.get("project_id") != EXPECTED_PROJECT_ID
            or proof.get("artifact_id") != EXPECTED_ARTIFACT_ID
            or proof.get("artifact_sha256") != EXPECTED_ARTIFACT_SHA256
            or proof.get("artifact_size") != EXPECTED_ARTIFACT_SIZE):
        raise ValueError("source_proof_identity_mismatch")
    configured_ids = {name: extension[id_key] for name, id_key, *_ in SERVICES}
    validate_service_binding(proof.get("service_container_ids", {}), configured_ids)
    h = common.H("scientific-storage-restart", fast_s3=True)
    h.eng = common.DockerWorkerEngine()
    h.engine_id = h.eng.engine_id()
    cfg = common.CFG
    h.run_id = UUID(run_id)
    ACTIVE_STEP = "engine_preflight"
    if cfg.docker_context != EXPECTED_DOCKER_CONTEXT or cfg.expected_engine_id != EXPECTED_ENGINE_ID:
        raise ValueError("docker_context_mismatch")
    validate_engine(h.engine_id, EXPECTED_ENGINE_ID)
    if _docker(h, "version", "--format", "{{.Server.Version}}").strip() != EXPECTED_DOCKER_VERSION:
        raise ValueError("docker_server_version_mismatch")
    ACTIVE_STEP = "storage_headroom_preflight"
    common.guard_storage_headroom()
    ACTIVE_STEP = "service_preflight"
    inspections = {}
    for name, id_key, image_key, volume, data_path in SERVICES:
        expected_id = extension[id_key]
        info = _inspect(h, expected_id)
        inspections[name] = validate_container(
            info,
            expected_id=expected_id,
            expected_name=name,
            expected_image=getattr(cfg, image_key),
            volume_name=volume,
            data_path=data_path,
        )
        volume_info = json.loads(_docker(h, "volume", "inspect", volume))[0]
        volume_state = validate_volume(volume_info, expected_name=volume, mount_source=inspections[name]["mount_source"])
        inspections[name]["volume_config_sha256"] = volume_state["config_sha256"]
        image_id = _docker(h, "image", "inspect", getattr(cfg, image_key), "--format", "{{.Id}}").strip()
        if image_id != inspections[name]["image_id"]:
            raise ValueError(f"pinned_image_identity_mismatch_{name}")
    ACTIVE_STEP = "pre_restart_snapshot"
    before = _capture(h, run_id, proof, common.BUCKET)
    _assert_absent_containers(h, before["executor_container_ids"])
    restarts = {}
    for spec in SERVICES:
        name = spec[0]
        ACTIVE_STEP = f"{name}_quiescence"
        if _capture(h, run_id, proof, common.BUCKET) != before:
            raise ValueError("target_state_changed_before_restart")
        _assert_absent_containers(h, before["executor_container_ids"])
        if _docker(h, "ps", "--no-trunc", "--filter", "label=scientist.platform/run", "--format", "{{.ID}}").strip():
            raise ValueError("active_run_actors_present")
        common.guard_storage_headroom()
        ACTIVE_STEP = f"restart_{name}"
        RUN_PROGRESS["service_restarts"][name] = {"attempted": True, "outcome": "unknown_until_inspected", "container_id": inspections[name]["id"]}
        restarted = _restart_one(h, spec, cfg, inspections[name])
        restarts[name] = {"before_started_at": inspections[name]["started_at"], "after_started_at": restarted["started_at"], "container_id": restarted["id"], "config_sha256": restarted["config_sha256"], "volume_config_sha256": restarted["volume_config_sha256"]}
        RUN_PROGRESS["service_restarts"][name] = restarts[name]
        if name == "scientist-b5-postgres":
            ACTIVE_STEP = "postgres_readiness"
            deadline = time.monotonic() + 120
            while True:
                try:
                    _postgres_ready()
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("postgres_readiness_timeout") from None
                    time.sleep(1)
        else:
            ACTIVE_STEP = "minio_readiness"
            deadline = time.monotonic() + 120
            while True:
                try:
                    h.s3.head_bucket(Bucket=common.BUCKET)
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("minio_readiness_timeout") from None
                    time.sleep(1)
    ACTIVE_STEP = "post_restart_snapshot"
    after = _capture(h, run_id, proof, common.BUCKET)
    if before != after:
        raise ValueError("durable_state_changed_across_restart")
    return {
        "status": "PASS",
        "phase": "scientific_storage_restart",
        "source_run_id": run_id,
        "source_artifact_id": proof["artifact_id"],
        "configured_service_container_ids": configured_ids,
        "target_executor_container_ids": before["executor_container_ids"],
        "artifact_sha256": proof["artifact_sha256"],
        "artifact_size": int(proof["artifact_size"]),
        "database_sha256": after["database_sha256"],
        "database_counts": after["counts"],
        "object_sha256": {key: value["sha256"] for key, value in after["objects"].items()},
        "object_count": len(after["objects"]),
        "operation_count": after["operation_count"],
        "checkpoint_count": after["checkpoint_count"],
        "boundary_count": after["boundary_count"],
        "receipt_count": after["receipt_count"],
        "artifact_count": after["artifact_count"],
        "service_restarts": restarts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--restart-owned-services", action="store_true", help="explicitly authorize one restart of each exact configured PG/MinIO container")
    parser.add_argument("--proof", type=Path, default=DEFAULT_PROOF)
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return 0
    output = os.environ.get("SCIENTIFIC_STORAGE_RESTART_EVIDENCE_DIR")
    if not args.restart_owned_services or not output:
        print(json.dumps({"status": "NOT RUN", "reason": "explicit restart flag and fresh evidence directory are required"}, sort_keys=True))
        return NOT_RUN
    started = datetime.now(timezone.utc).isoformat()
    report: dict[str, Any] = {"status": "RUNNING", "phase": "preflight", "source_proof": args.proof.name, "started_at": started}
    destination = Path(output)
    if not destination.is_absolute():
        destination = ROOT / destination
    try:
        resolved = destination.resolve()
        if not resolved.is_relative_to(ROOT / ".local"):
            raise ValueError("evidence_directory_must_be_under_local")
        path = _write_report(resolved, report)
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "phase": "evidence_preflight", "error_class": type(exc).__name__}, sort_keys=True))
        return 1
    try:
        report.update(_run(args.proof, args.restart_owned_services))
    except Exception as exc:
        report.update({"status": "FAIL", "failed_step": ACTIVE_STEP, "error_class": type(exc).__name__, **RUN_PROGRESS})
    report["started_at"] = started
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    try:
        _update_report(path, report)
    except Exception:
        print(json.dumps({"status": "FAIL", "phase": "evidence_write", "evidence": str(path.relative_to(ROOT)), "error_class": "EvidenceWriteError"}, sort_keys=True))
        return 1
    print(json.dumps({"status": report["status"], "evidence": str(path.relative_to(ROOT)), "phase": report.get("phase")}, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
