"""Narrow trusted-host runtime for the fixed, offline CSV computation."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import selectors
import stat
import subprocess
import tarfile
import time
import xml.etree.ElementTree as ET
import csv
from datetime import datetime
from dataclasses import replace
from pathlib import Path
from typing import Any

from scientist.runtime_contracts import ComputeLaunchSpec, MAX_COMPUTE_OUTPUT_BYTES
from scientist.supervisor import ExecutorRef

_RUN = "scientist.platform/run"
_GEN = "scientist.platform/generation"
_EXEC = "scientist.platform/executor"
_KIND = "scientist.platform/kind"
_INCARNATION = "scientist.platform/incarnation"
_OPERATION = "scientist.platform/operation"
_IMAGE = "scientist.platform/image-digest"
_RECIPE_FILES = ("csv_describe.py", "cpu_recipes.py", "scientific_render.py")
_INPUT_FILES = ("data.csv", "params.json")
_OUTPUT_TYPES = {
    "summary.json": "application/json",
    "summary.csv": "text/csv",
    "chart.svg": "image/svg+xml",
    "report.md": "text/markdown",
}
_MAX_INPUT_BYTES = 1_048_576
_MAX_PARAMS_BYTES = 16_384
_MAX_ARCHIVE_BYTES = 512 * 1024
_MAX_ARCHIVE_STDERR_BYTES = 16 * 1024
_WALL_SECONDS = 30
_DOCKER_CONTEXT = "colima-scientist-platform-test"
_DOCKER_TIMEOUT = 5
_OUTPUT_ARCHIVE_PRODUCER = r'''import os,stat,sys,tarfile
root=sys.argv[1]
if not stat.S_ISDIR(os.lstat(root).st_mode):
    raise SystemExit("output path is not a directory")
with os.scandir(root) as scan:
    entries=sorted(scan,key=lambda item:item.name)
for entry in entries:
    metadata=entry.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SystemExit("output entry is not an unlinked regular file")
with tarfile.open(fileobj=sys.stdout.buffer,mode="w|") as archive:
    for entry in entries:
        archive.add(entry.path,arcname=entry.name,recursive=False)
'''


def _run_docker(engine: Any, *args: str, input: bytes | None = None) -> str:
    if getattr(engine, "context", None) != _DOCKER_CONTEXT:
        raise RuntimeError("compute runtime only permits the owned Docker context")
    try:
        result = subprocess.run(
            ["docker", "--context", engine.context, *args], input=input,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            timeout=_DOCKER_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("bounded compute Docker command timed out") from exc
    if result.returncode != 0:
        raise RuntimeError("bounded compute Docker command failed: " + result.stderr.decode("utf-8", "replace")[:1000])
    return result.stdout.decode("utf-8", "replace").strip()


def _engine_identity(engine: Any) -> str:
    engine_id = _run_docker(engine, "info", "--format", "{{.ID}}")
    version = _run_docker(engine, "version", "--format", "{{.Server.Version}}")
    if not engine_id or len(engine_id) > 200 or version != "29.8.2":
        raise RuntimeError("owned Docker engine identity is unavailable")
    return engine_id


def _accepted_image_id(engine: Any, image_digest: str) -> str:
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_digest):
        raise RuntimeError("compute image must be an accepted immutable digest")
    image_id = _run_docker(engine, "image", "inspect", "--format", "{{.Id}}", image_digest)
    if image_id != image_digest:
        raise RuntimeError("compute image differs from the accepted immutable digest")
    return image_id


def recipe_manifest_sha256(directory: Path) -> str:
    """Hash the canonical manifest of the three exact guest source files."""
    directory = Path(directory)
    if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():
        raise ValueError("recipe directory must be a real absolute directory")
    files: list[dict[str, str]] = []
    entries = list(directory.iterdir())
    if {entry.name for entry in entries} != set(_RECIPE_FILES):
        raise ValueError("recipe directory must contain exactly the reviewed files")
    for name in _RECIPE_FILES:
        path = directory / name
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode) or path.stat().st_size > 1_048_576:
            raise ValueError("recipe files must be regular files")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append({"name": name, "sha256": digest})
    manifest = json.dumps(
        {"schema_version": 1, "files": files},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(manifest).hexdigest()


def _exact_directory(directory: Path, expected: tuple[str, ...], limits: dict[str, int]) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("compute staging path must be a real directory")
    if {entry.name for entry in directory.iterdir()} != set(expected):
        raise ValueError("compute staging directory has an unexpected file set")
    for name, limit in limits.items():
        path = directory / name
        if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > limit:
            raise ValueError("compute staging file is not regular or exceeds its limit")


def _validate_ref(executor: ExecutorRef) -> None:
    if (
        executor.kind != "compute"
        or executor.operation_id is None
        or not str(executor.operation_id)
        or not executor.engine_id
    ):
        raise RuntimeError("compute executor identity is incomplete")


def _verify_container(
    engine: Any,
    executor: ExecutorRef,
    *,
    expected_image_digest: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    _validate_ref(executor)
    if not re.fullmatch(r"[a-f0-9]{64}", executor.container_id or ""):
        raise RuntimeError("compute container identity is not bound")
    if _engine_identity(engine) != executor.engine_id:
        raise RuntimeError("owned Docker engine identity changed")
    inspected = _run_docker(
        engine,
        "inspect", "--format",
        "{{json .Config.Labels}}|{{.Id}}|{{.Image}}|{{json .State}}",
        executor.container_id,
    )
    try:
        labels_raw, container_id, image_id, state_raw = inspected.split("|", 3)
        labels = json.loads(labels_raw)
        state = json.loads(state_raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("compute container identity inspection is invalid") from exc
    expected = {
        _RUN: str(executor.run_id),
        _GEN: str(executor.generation),
        _EXEC: str(executor.executor_id),
        _KIND: "compute",
        _INCARNATION: str(executor.process_incarnation),
        _OPERATION: str(executor.operation_id),
    }
    if (
        not isinstance(labels, dict)
        or container_id != executor.container_id
        or any(labels.get(key) != value for key, value in expected.items())
    ):
        raise RuntimeError("compute container labels differ from the durable identity")
    accepted_digest = labels.get(_IMAGE)
    if not isinstance(accepted_digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", accepted_digest):
        raise RuntimeError("compute accepted image identity is missing")
    if accepted_digest != expected_image_digest:
        raise RuntimeError("compute container image differs from the accepted image")
    if image_id != accepted_digest:
        raise RuntimeError("compute container image differs from the accepted image")
    return labels, state


def create_compute(engine: Any, executor: ExecutorRef, spec: ComputeLaunchSpec) -> ExecutorRef:
    """Create and bind an isolated compute container. This never starts it."""
    _validate_ref(executor)
    engine_id = _engine_identity(engine)
    if executor.engine_id != engine_id or executor.container_id is not None:
        raise RuntimeError("compute create requires its persisted engine and an unbound container")
    if spec.recipe_manifest_sha256 != recipe_manifest_sha256(spec.recipe_directory):
        raise RuntimeError("compute recipe files differ from the approved manifest")
    _exact_directory(spec.input_directory, _INPUT_FILES, {
        "data.csv": _MAX_INPUT_BYTES, "params.json": _MAX_PARAMS_BYTES,
    })
    image_id = _accepted_image_id(engine, spec.image_digest)
    labels = [
        "--label", f"{_RUN}={executor.run_id}",
        "--label", f"{_GEN}={executor.generation}",
        "--label", f"{_EXEC}={executor.executor_id}",
        "--label", f"{_KIND}=compute",
        "--label", f"{_INCARNATION}={executor.process_incarnation}",
        "--label", f"{_OPERATION}={executor.operation_id}",
        "--label", f"{_IMAGE}={spec.image_digest}",
    ]
    mounts = [
        "--mount", f"type=bind,src={spec.recipe_directory},dst=/recipe,readonly",
        "--mount", f"type=bind,src={spec.input_directory},dst=/inputs,readonly",
    ]
    limits = [
        "--read-only", "--user", "65532:65532", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--network", "none",
        "--cpus", "1", "--memory", "1073741824", "--memory-swap", "1073741824",
        "--pids-limit", "128", "--restart", "no",
        "--tmpfs", "/work:rw,noexec,nosuid,nodev,size=67108864,mode=0700,uid=65532,gid=65532",
    ]
    container_id = _run_docker(engine,
        "create", *labels, *limits, *mounts, "--entrypoint", "python3.14",
        spec.image_digest, "-I", "-S", "/recipe/csv_describe.py",
    )
    if not re.fullmatch(r"[a-f0-9]{64}", container_id):
        raise RuntimeError("Docker did not return a full compute container ID")
    bound = replace(executor, container_id=container_id)
    _verify_container(engine, bound, expected_image_digest=spec.image_digest)
    if image_id != spec.image_digest:
        raise RuntimeError("accepted compute image changed during creation")
    return bound


def find_compute(engine: Any, executor: ExecutorRef, *, expected_image_digest: str) -> ExecutorRef | None:
    """Recover a create whose acknowledgment was lost; ambiguity stays unknown."""
    _validate_ref(executor)
    if _engine_identity(engine) != executor.engine_id:
        raise RuntimeError("owned Docker engine identity changed")
    if executor.container_id is not None:
        present = _run_docker(
            engine, "ps", "-aq", "--no-trunc", "--filter", f"id={executor.container_id}",
        )
        if not present:
            if _engine_identity(engine) != executor.engine_id:
                raise RuntimeError("owned Docker engine identity changed")
            return None
        if present.splitlines() != [executor.container_id]:
            raise RuntimeError("durable compute container identity is ambiguous")
        _verify_container(engine, executor, expected_image_digest=expected_image_digest)
        return executor
    filters = (
        (_KIND, "compute"), (_RUN, str(executor.run_id)),
        (_GEN, str(executor.generation)), (_EXEC, str(executor.executor_id)),
        (_INCARNATION, str(executor.process_incarnation)),
        (_OPERATION, str(executor.operation_id)),
    )
    args = ["ps", "-aq", "--no-trunc"]
    for key, value in filters:
        args.extend(("--filter", f"label={key}={value}"))
    ids = [value for value in _run_docker(engine, *args).splitlines() if value]
    if not ids:
        return None
    if len(ids) != 1 or not re.fullmatch(r"[a-f0-9]{64}", ids[0]):
        raise RuntimeError("compute create recovery identity is ambiguous")
    bound = replace(executor, container_id=ids[0])
    _verify_container(engine, bound, expected_image_digest=expected_image_digest)
    return bound


def start_compute(engine: Any, executor: ExecutorRef, *, expected_image_digest: str) -> None:
    _, state = _verify_container(engine, executor, expected_image_digest=expected_image_digest)
    if state.get("Running") is True:
        return
    if state.get("Status") not in {"created", "restarting"}:
        raise RuntimeError("compute container is not in its one permitted pre-start state")
    _run_docker(engine, "start", executor.container_id)
    _verify_container(engine, executor, expected_image_digest=expected_image_digest)


def stop_compute(engine: Any, executor: ExecutorRef, *, expected_image_digest: str) -> None:
    _, state = _verify_container(engine, executor, expected_image_digest=expected_image_digest)
    if state.get("Running") is True:
        _run_docker(engine, "stop", "--time", "0", executor.container_id)
    _run_docker(engine, "rm", "--force", executor.container_id)
    absent = _run_docker(engine, "ps", "-aq", "--no-trunc", "--filter", f"id={executor.container_id}")
    if absent or _engine_identity(engine) != executor.engine_id:
        raise RuntimeError("compute stop could not prove exact physical absence")


def _started_at(state: dict[str, Any]) -> float:
    value = state.get("StartedAt")
    if not isinstance(value, str):
        raise RuntimeError("compute start timestamp is unavailable")
    try:
        started = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("compute start timestamp is invalid") from exc
    if started.tzinfo is None:
        raise RuntimeError("compute start timestamp has no timezone")
    return started.timestamp()


def _ready_marker(engine: Any, executor: ExecutorRef) -> bool:
    script = (
        "import pathlib,stat,sys; p=pathlib.Path('/work/result-ready'); "
        "s=p.lstat() if p.exists() else None; "
        "sys.stdout.write('ready' if s and stat.S_ISREG(s.st_mode) "
        "and s.st_size == 6 and p.read_bytes() == b'ready\\n' else 'pending')"
    )
    status = _run_docker(
        engine, "exec", executor.container_id, "python3.14", "-I", "-S", "-c", script,
    )
    if status not in {"ready", "pending"}:
        raise RuntimeError("compute result-ready marker is invalid")
    return status == "ready"


def poll_compute(engine: Any, executor: ExecutorRef, *, expected_image_digest: str) -> int | None:
    _, state = _verify_container(engine, executor, expected_image_digest=expected_image_digest)
    if state.get("Running") is True:
        if time.time() - _started_at(state) >= _WALL_SECONDS:
            stop_compute(engine, executor, expected_image_digest=expected_image_digest)
            return 124
        if _ready_marker(engine, executor):
            return 0
        return None
    code = state.get("ExitCode")
    if state.get("Status") not in {"exited", "dead"}:
        raise RuntimeError("compute container is neither running nor finished")
    if isinstance(code, bool) or not isinstance(code, int) or code < 0:
        raise RuntimeError("compute exit status is unavailable")
    if code == 0:
        raise RuntimeError("compute guest exited without a live result-ready container")
    return code


def _container_archive_command(engine: Any, container_id: str) -> list[str]:
    """Build the single fixed in-container archive command for the bound CID."""
    if getattr(engine, "context", None) != _DOCKER_CONTEXT:
        raise RuntimeError("compute runtime only permits the owned Docker context")
    if not re.fullmatch(r"[a-f0-9]{64}", container_id or ""):
        raise RuntimeError("compute output read requires a full bound container ID")
    return [
        "docker", "--context", engine.context, "exec", container_id,
        "python3.14", "-I", "-S", "-c", _OUTPUT_ARCHIVE_PRODUCER, "/work/outputs",
    ]


def _container_archive(engine: Any, container_id: str) -> bytes:
    """Stream a trusted stdlib tar producer from the exact live guest namespace."""
    deadline = time.monotonic() + _DOCKER_TIMEOUT
    process = subprocess.Popen(
        _container_archive_command(engine, container_id),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_total = stderr_total = 0
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            events = selector.select(remaining) if remaining > 0 else []
            if not events:
                process.kill()
                raise RuntimeError("compute output archive read timed out")
            for key, _ in events:
                stream = key.fileobj
                chunk = os.read(stream.fileno(), 16 * 1024)
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                if key.data == "stdout":
                    stdout_total += len(chunk)
                    if stdout_total > _MAX_ARCHIVE_BYTES:
                        process.kill()
                        raise RuntimeError("compute output archive exceeds its bounded read")
                    chunks.append(chunk)
                else:
                    stderr_total += len(chunk)
                    if stderr_total > _MAX_ARCHIVE_STDERR_BYTES:
                        process.kill()
                        raise RuntimeError("compute output archive stderr exceeds its bounded read")
                    stderr_chunks.append(chunk)
        return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        if return_code != 0:
            detail = b"".join(stderr_chunks).decode("utf-8", "replace")[:1000]
            raise RuntimeError("Docker could not read compute outputs" + (f": {detail}" if detail else ""))
    except subprocess.TimeoutExpired as exc:
        process.kill()
        raise RuntimeError("compute output archive read timed out") from exc
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    return b"".join(chunks)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("compute JSON contains a non-finite number")
    return parsed


def _validate_output(name: str, data: bytes) -> None:
    if name == "summary.json":
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_json_object,
            parse_float=_finite_float,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non-finite JSON number")),
        )
        if not isinstance(value, dict):
            raise ValueError("compute summary must be a JSON object")
    elif name in {"summary.csv", "report.md"}:
        text = data.decode("utf-8")
        if "\x00" in text:
            raise ValueError("compute text output contains NUL")
        if name == "summary.csv":
            rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
            if not rows or not rows[0] or any(len(row) != len(rows[0]) for row in rows):
                raise ValueError("compute CSV output is malformed")
    elif name == "chart.svg":
        text = data.decode("utf-8")
        if "<?" in text:
            raise ValueError("compute SVG processing instruction is not permitted")
        lowered = data.lower()
        if any(token in lowered for token in (b"<!doctype", b"<!entity", b"<script", b"foreignobject", b"url(", b"@import")):
            raise ValueError("compute SVG contains active or external content")
        root = ET.fromstring(data)
        if root.tag.rsplit("}", 1)[-1] != "svg":
            raise ValueError("compute chart is not SVG")
        allowed_attributes = {
            "svg": {"viewBox", "role", "aria-labelledby"},
            "title": {"id"},
            "desc": {"id"},
            "text": {"x", "y"},
            "rect": {"x", "y", "width", "height", "fill"},
        }
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1].lower()
            if tag not in allowed_attributes:
                raise ValueError("compute SVG contains an unreviewed or active element")
            attributes = set(element.attrib)
            if attributes != allowed_attributes[tag]:
                raise ValueError("compute SVG contains an unreviewed attribute")
            if tag == "svg" and (
                element.tag != "{http://www.w3.org/2000/svg}svg"
                or element.attrib.get("role") != "img"
                or element.attrib.get("aria-labelledby") != "title desc"
                or not re.fullmatch(r"0 0 720 [0-9]{1,4}", element.attrib.get("viewBox", ""))
            ):
                raise ValueError("compute SVG root differs from the reviewed static chart")
            if tag in {"title", "desc"} and element.attrib.get("id") != tag:
                raise ValueError("compute SVG description identity is invalid")
            if tag in {"text", "rect"}:
                numeric_attributes = ("x", "y") if tag == "text" else ("x", "y", "width", "height")
                if any(not re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3})?", element.attrib[key]) for key in numeric_attributes):
                    raise ValueError("compute SVG geometry is invalid")
            if tag == "rect" and element.attrib["fill"] != "#3568a8":
                raise ValueError("compute SVG color differs from the reviewed chart")


def _parse_archive(raw: bytes) -> dict[str, bytes]:
    if len(raw) > _MAX_ARCHIVE_BYTES:
        raise ValueError("compute output archive exceeds its transport limit")
    try:
        archive = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
    except tarfile.TarError as exc:
        raise ValueError("compute output archive is invalid") from exc
    with archive:
        members = archive.getmembers()
        if len(members) != len(_OUTPUT_TYPES):
            raise ValueError("compute output archive must contain exactly four files")
        sizes = 0
        by_name: dict[str, tarfile.TarInfo] = {}
        for member in members:
            if (
                member.name not in _OUTPUT_TYPES
                or member.name in by_name
                or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE)
            ):
                raise ValueError("compute output archive contains an unknown or non-regular entry")
            if member.size < 0:
                raise ValueError("compute output archive contains an invalid size")
            sizes += member.size
            if sizes > MAX_COMPUTE_OUTPUT_BYTES:
                raise ValueError("compute outputs exceed 256 KiB")
            by_name[member.name] = member
        if set(by_name) != set(_OUTPUT_TYPES):
            raise ValueError("compute output archive is missing a required file")
        outputs: dict[str, bytes] = {}
        for name in _OUTPUT_TYPES:
            stream = archive.extractfile(by_name[name])
            if stream is None:
                raise ValueError("compute output archive file cannot be read")
            data = stream.read(by_name[name].size + 1)
            if len(data) != by_name[name].size:
                raise ValueError("compute output archive member size changed")
            _validate_output(name, data)
            outputs[name] = data
        return outputs


def read_compute_outputs(
    engine: Any,
    executor: ExecutorRef,
    *,
    expected_image_digest: str,
) -> dict[str, bytes]:
    _, state = _verify_container(engine, executor, expected_image_digest=expected_image_digest)
    if state.get("Running") is not True or not _ready_marker(engine, executor):
        raise RuntimeError("compute outputs require the live, exact result-ready container")
    return _parse_archive(_container_archive(engine, executor.container_id))
