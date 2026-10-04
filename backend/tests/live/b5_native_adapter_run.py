"""Run the native-adapter acceptance harness in the pinned isolated image."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from pathlib import Path

from b5_live_config import CFG

ROOT = CFG.root
IMAGE = CFG.worker_image_id
CONTEXT = CFG.docker_context
SCRIPT = Path(__file__).with_name("b5_native_adapter_acceptance.py")
OUTPUT = CFG.evidence / "b5-native-adapter-acceptance.json"
TARGET = "/tmp/b5_native_adapter_acceptance.py"


def main() -> int:
    inspect = [
        "docker", "--context", CONTEXT, "image", "inspect", IMAGE,
        "--format", "{{.Id}}",
    ]
    image_id = subprocess.run(inspect, check=True, capture_output=True, text=True).stdout.strip()
    if image_id != IMAGE:
        raise SystemExit(f"Pinned image mismatch: {image_id}")

    command = [
        "docker", "--context", CONTEXT, "run", "--rm", "--pull=never",
        "--network", "none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--user", "65532:65532",
        "--memory=768m", "--cpus=1", "--pids-limit=64",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--tmpfs", "/home/scientist:rw,noexec,nosuid,nodev,size=16m,uid=65532,gid=65532,mode=0700",
        "--tmpfs", "/run/scientist:rw,noexec,nosuid,nodev,size=16m,uid=65532,gid=65532,mode=0700",
        "--tmpfs", "/run/hermes-home:rw,noexec,nosuid,nodev,size=16m,uid=65532,gid=65532,mode=0700",
        "--tmpfs", "/workspace:rw,noexec,nosuid,nodev,size=64m,uid=65532,gid=65532,mode=0700",
        "--mount", f"type=bind,src={SCRIPT},dst={TARGET},readonly",
        "--entrypoint", "/opt/python/bin/python3.14", IMAGE, TARGET, CFG.worker_image_id, CFG.runtime_commit,
    ]
    started = time.time()
    result = subprocess.run(command, capture_output=True, text=True, timeout=240)
    elapsed = round(time.time() - started, 3)
    payload: dict[str, object] = {
        "schema_version": 1,
        "docker_context": CONTEXT,
        "image_digest": IMAGE,
        "image_id_verified": image_id,
        "command": command,
        "command_string": shlex.join(command),
        "network": "none",
        "read_only_root": True,
        "capabilities_dropped": "ALL",
        "user": "65532:65532",
        "memory_limit": "768m",
        "cpu_limit": "1",
        "pids_limit": 64,
        "private_tmpfs_mounts": [
            "/tmp", "/home/scientist", "/run/scientist", "/run/hermes-home", "/workspace"
        ],
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
        "elapsed_seconds": elapsed,
        "paid_calls": 0,
    }
    decoder = json.JSONDecoder()
    parsed: dict[str, object] | None = None
    for index, char in enumerate(result.stdout):
        if char != "{":
            continue
        try:
            candidate, _end = decoder.raw_decode(result.stdout, index)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            parsed = candidate
    if parsed is not None:
        payload["harness_result"] = parsed
    else:
        payload["harness_result_parse_error"] = True
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"exit_code": result.returncode, "evidence": str(OUTPUT), "elapsed_seconds": elapsed}))
    return result.returncode if parsed is not None and parsed.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
