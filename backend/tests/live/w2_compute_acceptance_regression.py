"""Offline regression probes for the live compute pre-start inspect gate."""
from __future__ import annotations

from copy import deepcopy
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import UUID

from scientist.runtime_contracts import ComputeLaunchSpec
from scientist.supervisor import ExecutorRef

try:
    from . import w2_compute_acceptance as acceptance
except ImportError:
    import w2_compute_acceptance as acceptance


IMAGE = "sha256:" + "a" * 64
ENGINE_ID = "owned-engine"
CID = "b" * 64
OUTPUTS = {
    "summary.json": b'{"rows":3}',
    "summary.csv": b"name,value\nrows,3\n",
    "chart.svg": (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 140" role="img" aria-labelledby="title desc">'
        b'<title id="title">Means</title><desc id="desc">CSV means</desc>'
        b'<text x="12" y="54">x</text><rect x="440.000" y="40" width="150.000" height="22" fill="#3568a8"/>'
        b"</svg>"
    ),
    "report.md": b"# Report\n",
}


class OfflineEngine:
    context = "colima-scientist-platform-test"

    @staticmethod
    def engine_id() -> str:
        return ENGINE_ID


def _run_local_producer(output_dir: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-I", "-S", "-c", acceptance.compute_runtime._OUTPUT_ARCHIVE_PRODUCER,
         str(output_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=5,
    )


def _produce_local(output_dir: Path) -> bytes:
    result = _run_local_producer(output_dir)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout


def _files(root: Path, values: dict[str, bytes] = OUTPUTS) -> None:
    root.mkdir()
    for name, data in values.items():
        (root / name).write_bytes(data)


def _archive_with_traversal() -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data in OUTPUTS.items():
            info = tarfile.TarInfo("../escape" if name == "summary.json" else name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


def _archive_with_nonregular_output(name: str, member_type: bytes, *, linkname: str = "") -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for output_name, data in OUTPUTS.items():
            info = tarfile.TarInfo(output_name)
            if output_name == name:
                info.type = member_type
                info.linkname = linkname
                archive.addfile(info)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


def _fixture(root: Path) -> tuple[ExecutorRef, ComputeLaunchSpec, dict[str, object]]:
    recipe = root / "recipe"
    inputs = root / "inputs"
    recipe.mkdir()
    inputs.mkdir()
    ref = ExecutorRef(
        executor_id=UUID("00000000-0000-0000-0000-000000000001"),
        run_id=UUID("00000000-0000-0000-0000-000000000002"),
        generation=1,
        kind="compute",
        operation_id="offline-compute-1",
        process_incarnation=UUID("00000000-0000-0000-0000-000000000003"),
        engine_id=ENGINE_ID,
        container_id=CID,
    )
    spec = ComputeLaunchSpec(
        profile_id="prof.csv-stdlib@py3.14.7",
        profile_version="1",
        image_digest=IMAGE,
        recipe_manifest_sha256="c" * 64,
        recipe_directory=recipe,
        input_directory=inputs,
        output_directory=root / "outputs",
    )
    container: dict[str, object] = {
        "Id": CID,
        "Image": IMAGE,
        "Config": {
            "Image": IMAGE,
            "Labels": {
                "scientist.platform/run": str(ref.run_id),
                "scientist.platform/generation": "1",
                "scientist.platform/executor": str(ref.executor_id),
                "scientist.platform/kind": "compute",
                "scientist.platform/incarnation": str(ref.process_incarnation),
                "scientist.platform/operation": ref.operation_id,
                "scientist.platform/image-digest": IMAGE,
            },
            "User": "65532:65532",
            "Entrypoint": ["python3.14"],
            "Cmd": ["-I", "-S", "/recipe/csv_describe.py"],
        },
        "HostConfig": {
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CapDrop": ["ALL"],
            "CapAdd": None,
            "SecurityOpt": ["no-new-privileges:true"],
            "NetworkMode": "none",
            "PortBindings": None,
            "NanoCpus": 1_000_000_000,
            "Memory": 1_073_741_824,
            "MemorySwap": 1_073_741_824,
            "PidsLimit": 128,
            "Tmpfs": {
                "/work": "rw,noexec,nosuid,nodev,size=67108864,mode=0700,uid=65532,gid=65532"
            },
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "PidMode": "",
            "IpcMode": "private",
            "Devices": None,
            "DeviceRequests": None,
            "DeviceCgroupRules": None,
            "AutoRemove": False,
        },
        "State": {"Status": "created", "Running": False},
        "Mounts": [
            {"Type": "bind", "Source": str(recipe), "Destination": "/recipe", "RW": False},
            {"Type": "bind", "Source": str(inputs), "Destination": "/inputs", "RW": False},
        ],
    }
    return ref, spec, container


def run() -> None:
    engine = OfflineEngine()
    commands: list[list[str]] = []
    local_popen = acceptance.compute_runtime.subprocess.Popen

    def local_stream(command: list[str], **kwargs: object) -> subprocess.Popen:
        commands.append(command)
        return local_popen(
            [sys.executable, "-I", "-S", "-c", "import sys;sys.stdout.buffer.write(b'local stream')"],
            **kwargs,
        )

    with patch.object(acceptance.compute_runtime.subprocess, "Popen", side_effect=local_stream):
        assert acceptance.compute_runtime._container_archive(engine, CID) == b"local stream"
    command = commands[0]
    assert command[0:4] == ["docker", "--context", engine.context, "exec"]
    assert command[4:8] == [CID, "python3.14", "-I", "-S"]
    assert command[-1] == "/work/outputs"
    assert "cp" not in command
    with TemporaryDirectory(prefix="w2-compute-tar-") as raw:
        root = Path(raw)
        valid = root / "valid"
        _files(valid)
        archive = _produce_local(valid)
        assert acceptance.compute_runtime._parse_archive(archive) == OUTPUTS

        socket_dir = root / "socket"
        _files(socket_dir)
        socket_path = socket_dir / "unexpected.sock"
        unexpected_socket = socket.socket(socket.AF_UNIX)
        unexpected_socket.bind(str(socket_path))
        try:
            result = _run_local_producer(socket_dir)
            assert result.returncode != 0, "archive producer silently omitted an unexpected UNIX socket"
        finally:
            unexpected_socket.close()
            socket_path.unlink()

        symlink = root / "symlink"
        _files(symlink)
        (symlink / "report.md").unlink()
        (symlink / "report.md").symlink_to("summary.md")
        assert _run_local_producer(symlink).returncode != 0, "archive producer accepted a symlink output"

        hardlink = root / "hardlink"
        _files(hardlink)
        (hardlink / "summary.csv").unlink()
        os.link(hardlink / "summary.json", hardlink / "summary.csv")
        assert _run_local_producer(hardlink).returncode != 0, "archive producer accepted a hardlink output"

        directory = root / "directory"
        _files(directory)
        (directory / "report.md").unlink()
        (directory / "report.md").mkdir()
        assert _run_local_producer(directory).returncode != 0, "archive producer accepted a directory output"

        for malicious in (
            _archive_with_nonregular_output("report.md", tarfile.SYMTYPE, linkname="summary.md"),
            _archive_with_nonregular_output("summary.csv", tarfile.LNKTYPE, linkname="summary.json"),
            _archive_with_nonregular_output("report.md", tarfile.DIRTYPE),
            _archive_with_nonregular_output("report.md", b"S"),
        ):
            try:
                acceptance.compute_runtime._parse_archive(malicious)
            except ValueError:
                pass
            else:
                raise AssertionError("host archive parser accepted a non-regular output")

        extra = root / "extra"
        _files(extra)
        (extra / "unexpected.txt").write_bytes(b"extra")
        try:
            acceptance.compute_runtime._parse_archive(_produce_local(extra))
        except ValueError:
            pass
        else:
            raise AssertionError("host archive parser accepted an extra output")

        for unsafe in (_archive_with_traversal(),):
            try:
                acceptance.compute_runtime._parse_archive(unsafe)
            except ValueError:
                pass
            else:
                raise AssertionError("host archive parser accepted traversal")

        oversized = root / "oversized"
        _files(oversized, {**OUTPUTS, "report.md": b"x" * (256 * 1024)})
        try:
            acceptance.compute_runtime._parse_archive(_produce_local(oversized))
        except ValueError:
            pass
        else:
            raise AssertionError("host archive parser accepted oversized output bytes")

        active_svg = root / "active-svg"
        _files(active_svg, {**OUTPUTS, "chart.svg": b"<svg><script>alert(1)</script></svg>"})
        try:
            acceptance.compute_runtime._parse_archive(_produce_local(active_svg))
        except ValueError:
            pass
        else:
            raise AssertionError("host archive parser accepted active SVG MIME content")

        malformed_json = root / "malformed-json"
        _files(malformed_json, {**OUTPUTS, "summary.json": b'{"value":NaN}'})
        try:
            acceptance.compute_runtime._parse_archive(_produce_local(malformed_json))
        except ValueError:
            pass
        else:
            raise AssertionError("host archive parser accepted malformed JSON bytes")
    with TemporaryDirectory(prefix="w2-compute-gate-") as raw:
        ref, spec, baseline = _fixture(Path(raw))
        with patch.object(acceptance, "_inspect", return_value=baseline):
            assert acceptance._verify_containment(engine, ref, spec, ENGINE_ID)["status"] == "PASS"

            mutations = {
                "restart policy": lambda c: c["HostConfig"].__setitem__(
                    "RestartPolicy", {"Name": "always", "MaximumRetryCount": 0}
                ),
                "host PID namespace": lambda c: c["HostConfig"].__setitem__("PidMode", "host"),
                "host IPC namespace": lambda c: c["HostConfig"].__setitem__("IpcMode", "host"),
                "host device mapping": lambda c: c["HostConfig"].__setitem__(
                    "Devices", [{"PathOnHost": "/dev/sda", "PathInContainer": "/dev/sda", "CgroupPermissions": "rwm"}]
                ),
                "unconfined seccomp": lambda c: c["HostConfig"]["SecurityOpt"].append("seccomp=unconfined"),
                "extra executable tmpfs": lambda c: c["HostConfig"]["Tmpfs"].__setitem__("/extra", "rw,exec"),
            }
            for name, mutate in mutations.items():
                changed = deepcopy(baseline)
                mutate(changed)
                with patch.object(acceptance, "_inspect", return_value=changed):
                    try:
                        acceptance._verify_containment(engine, ref, spec, ENGINE_ID)
                    except acceptance.AcceptanceFailure:
                        continue
                    raise AssertionError(f"unsafe {name} mutation passed the containment gate")
        diagnostic_root = Path(raw) / "diagnostic"
        diagnostic_root.mkdir()
        ref, _spec_value, _container = _fixture(diagnostic_root)
        calls: list[tuple[str, ...]] = []

        def fake_docker(_engine: object, *args: str, input: bytes | None = None) -> str:
            calls.append(args)
            if args[:3] == ("inspect", "--format", "{{json .State}}"):
                return '{"Running":true,"Status":"running"}'
            if args[:3] == ("logs", "--tail", "60"):
                return "synthetic guest traceback"
            raise AssertionError(args)

        with patch.object(acceptance.compute_runtime, "_run_docker", side_effect=fake_docker):
            failure = acceptance._capture_failure_diagnostics(
                engine, [ref], "normal_read_outputs", RuntimeError("bounded archive read failed"),
            )
        assert failure["phase"] == "normal_read_outputs"
        assert failure["error_message"] == "bounded archive read failed"
        assert failure["containers"][0]["log_tail"] == "synthetic guest traceback"
        assert calls == [
            ("inspect", "--format", "{{json .State}}", CID),
            ("logs", "--tail", "60", CID),
        ]
    print("PASS: safe baseline accepted; unsafe containment and archive mutations rejected")


if __name__ == "__main__":
    run()
