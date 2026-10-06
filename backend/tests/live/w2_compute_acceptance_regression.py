"""Offline regression probes for the live compute pre-start inspect gate."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import UUID

from scientist.runtime_contracts import ComputeLaunchSpec
from scientist.supervisor import ExecutorRef

import w2_compute_acceptance as acceptance


IMAGE = "sha256:" + "a" * 64
ENGINE_ID = "owned-engine"
CID = "b" * 64


class OfflineEngine:
    context = "colima-scientist-platform-test"

    @staticmethod
    def engine_id() -> str:
        return ENGINE_ID


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
    print("PASS: safe baseline accepted; all six unsafe Docker inspect mutations rejected")


if __name__ == "__main__":
    run()
