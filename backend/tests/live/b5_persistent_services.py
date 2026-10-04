#!/usr/bin/env python3
"""Owner-approved 2026-10-04 (option A): label-only cleanup of run 38f67b90 leftovers and
recreation of the isolated b5 PostgreSQL/MinIO test services on project-owned named volumes.

Phases (each refuses unless every precondition re-proves against the pinned manifest):
  plan      read-only: print every command that `apply` would run
  cleanup   stop/remove ONLY run 38f67b90's dispatch + worker by full ID, then its empty network
  services  remove the (already wiped) tmpfs services by full ID, create two labelled volumes,
            set ownership/mode, recreate both services with identical security/network/env
  verify    persistence probe: marker row + object with SHA-256, docker restart both, re-read
Docker context is fixed; the engine ID is checked before any mutation. No secret is read or
printed: MinIO credentials are only bind-mounted from the same files as before.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from b5_live_config import CFG  # noqa: E402

ROOT = CFG.root
CTX = CFG.docker_context
ENGINE_ID = CFG.expected_engine_id
EVIDENCE = CFG.evidence / "b5-persistent-services-20261004"

RUN_ID = "38f67b90-4c38-4a14-9394-a7241ffb8df3"
RUN_NETWORK = "scientist-run-38f67b904c38-g1"
RUN_NETWORK_ID_PREFIX = "dcb432833f79"
RUN_CONTAINERS = {
    "91a84cf1853a38b17db44a569a8687ae7af23ecd4c27c8dc5747aad8323e27d9": ("dispatch", "b9dfdafa-8d0e-436f-8929-5a1d5a796b91"),
    "7e4ad8f8f92072ac2d0fabadb1ea9abe3e5441f61f64fdcbe1aa02d590080e7e": ("worker", "fefaa8d4-8e46-4798-af1a-ed1f0f8340ee"),
}

SERVICES_NETWORK = "scientist-b5-services-test"
PURPOSE = {"scientist.platform/purpose": "b5-services-test"}
PG_IMAGE = CFG.postgres_image
MINIO_IMAGE = CFG.minio_image
OLD_SERVICES = {
    "scientist-b5-postgres": "5ee2152ab632d1246260dfaae570077528ee3782ecc28f46403cffab408108f3",
    "scientist-b5-minio": "598d7a8b87496cb5ea7c81ad9600695013c70ce51989ea2e255b9d2a9d3c07a4",
}
PG_VOLUME, MINIO_VOLUME = "scientist-b5-pgdata", "scientist-b5-minio-data"
FORBIDDEN_VOLUMES = {"scientist-minio-cache", "scientist-minio-scratch"}
MIN_FREE_KIB = 2 * 1024 * 1024        # refuse below 2 GiB free in the VM
VOLUME_BUDGET_BYTES = 1536 * 1024**2  # monitoring ceiling for the two volumes together

COMMON = ["--detach", "--restart", "no", "--read-only", "--cap-drop", "ALL",
          "--security-opt", "no-new-privileges:true", "--memory", "256m", "--memory-swap", "256m",
          "--cpus", "0.5", "--pids-limit", "64", "--network", SERVICES_NETWORK, "--pull", "never",
          "--label", "scientist.platform/purpose=b5-services-test"]
PG_RUN = ["run", "--name", "scientist-b5-postgres", *COMMON, "--ip", "172.19.0.2", "--user", "70:70",
          "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=1048576",
          "--tmpfs", "/var/run/postgresql:rw,nosuid,nodev,size=1048576,mode=0700,uid=70,gid=70",
          "--mount", f"type=volume,src={PG_VOLUME},dst=/var/lib/postgresql,volume-nocopy",
          "--env", "POSTGRES_HOST_AUTH_METHOD=trust", "--env", "POSTGRES_DB=scientist_b5",
          PG_IMAGE, "postgres"]
MINIO_RUN = ["run", "--name", "scientist-b5-minio", *COMMON, "--ip", "172.19.0.3", "--user", "501:501",
             "--network-alias", "scientist-minio",
             "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16777216",
             "--mount", f"type=volume,src={MINIO_VOLUME},dst=/data,volume-nocopy",
             "--mount", f"type=bind,src={CFG.private_dir}/s3_secret_key,dst=/run/secrets/password,readonly",
             "--mount", f"type=bind,src={CFG.private_dir}/s3_access_key,dst=/run/secrets/user,readonly",
             "--env", "MINIO_ROOT_PASSWORD_FILE=/run/secrets/password", "--env", "MINIO_ROOT_USER_FILE=/run/secrets/user",
             "--env", "MINIO_API_SELECT_PARQUET=off",
             MINIO_IMAGE, "server", "/data", "--address", ":9000", "--console-address", ":9001"]


def init_cmd(volume: str, uid: int) -> list[str]:
    # One-shot ownership/mode set inside a no-network container; only CHOWN/FOWNER are granted.
    return ["run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--user", "0:0",
            "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "FOWNER",
            "--security-opt", "no-new-privileges:true", "--mount", f"type=volume,src={volume},dst=/v",
            "--entrypoint", "sh", PG_IMAGE, "-c", f"chown {uid}:{uid} /v && chmod 0700 /v && stat -c '%u:%g %a' /v"]


def docker(*args: str, check: bool = True) -> str:
    out = subprocess.run(["docker", "--context", CTX, *args], capture_output=True, text=True, timeout=180,
                         env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "HOME": str(Path.home())})
    if check and out.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {out.stderr.strip()[-300:]}")
    return out.stdout.strip()


def inspect(kind: str, ref: str) -> dict | None:
    raw = docker(kind, "inspect", ref, check=False)
    items = json.loads(raw) if raw.startswith("[") else []
    return items[0] if items else None


def record(name: str, data) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / name
    if path.exists():
        path.rename(path.with_suffix(path.suffix + f".prev-{int(time.time())}"))
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def preflight() -> None:
    if docker("info", "--format", "{{.ID}}") != ENGINE_ID:
        raise RuntimeError("engine identity differs from the authorized test engine")
    names = set(docker("volume", "ls", "--format", "{{.Name}}").splitlines())
    if FORBIDDEN_VOLUMES - names:
        raise RuntimeError("shared MinIO build volumes not as expected; refusing")


def vm_free_kib() -> int:
    line = docker("run", "--rm", "--pull", "never", "--network", "none", "--entrypoint", "df", PG_IMAGE, "-k", "/").splitlines()[-1]
    return int(line.split()[3])


def volume_kib() -> dict[str, int]:
    # du as each volume's owning UID (dirs are 0700); no network, read-only mount.
    sizes = {}
    for volume, uid in ((PG_VOLUME, 70), (MINIO_VOLUME, 501)):
        out = docker("run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--user", f"{uid}:{uid}",
                     "--cap-drop", "ALL", "--mount", f"type=volume,src={volume},dst=/v,readonly",
                     "--entrypoint", "du", PG_IMAGE, "-sk", "/v")
        sizes[volume] = int(out.split()[0])
    return sizes


def phase_cleanup(apply: bool) -> None:
    plan, already_absent = [], []
    for cid, (kind, executor) in RUN_CONTAINERS.items():
        c = inspect("container", cid)
        if c is None:
            already_absent.append(cid)  # resumed run: approved full ID already removed
            continue
        labels = c["Config"]["Labels"] or {}
        if (c["Id"] != cid or labels.get("scientist.platform/run") != RUN_ID or labels.get("scientist.platform/kind") != kind
                or labels.get("scientist.platform/executor") != executor or labels.get("scientist.platform/generation") != "1"):
            raise RuntimeError(f"{kind} container identity differs from the owner-approved manifest")
        plan += [["stop", "--time", "10", cid]] if c["State"]["Running"] else []
        plan.append(["rm", cid])
    net = inspect("network", RUN_NETWORK)
    if net is None and not docker("network", "ls", "-q", "--filter", f"label=scientist.platform/run={RUN_ID}"):
        already_absent.append(RUN_NETWORK)
    elif (net is None or not net["Id"].startswith(RUN_NETWORK_ID_PREFIX)
            or net["Labels"].get("scientist.platform/executor") != "fefaa8d4-8e46-4798-af1a-ed1f0f8340ee" or net["Labels"].get("scientist.platform/run") != RUN_ID
            or net["Labels"].get("scientist.platform/generation") != "1" or not net["Internal"]):
        raise RuntimeError("run network identity differs from the owner-approved manifest")
    if net is not None:
        attached = set((net.get("Containers") or {}).keys())
        if attached - set(RUN_CONTAINERS):
            raise RuntimeError("run network has a container outside the approved set")
        plan.append(["network", "rm", net["Id"]])
    print(json.dumps({"phase": "cleanup", "commands": plan, "already_absent": already_absent}))
    if not apply:
        return
    for cmd in plan:
        if cmd[0] == "network":
            again = inspect("network", cmd[-1])
            if again and again.get("Containers"):
                raise RuntimeError("run network still has attached containers")
        docker(*cmd)
    remaining = docker("ps", "-aq", "--filter", f"label=scientist.platform/run={RUN_ID}")
    if remaining or docker("network", "ls", "-q", "--filter", f"label=scientist.platform/run={RUN_ID}"):
        raise RuntimeError("run 38f67b90 resources remain after cleanup")
    record("cleanup-38f67b90.json", {"run_id": RUN_ID, "removed_containers": {c: k for c, (k, _) in RUN_CONTAINERS.items()},
                                     "removed_network": net["Id"] if net else None, "already_absent": already_absent, "basis": "owner-approved label-only cleanup; no durable DB ownership proof exists (records erased by tmpfs restart)",
                                     "status": "REMOVED"})


def stat_volume(volume: str) -> str:
    return docker("run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--user", "0:0", "--cap-drop", "ALL",
                  "--mount", f"type=volume,src={volume},dst=/v,readonly,volume-nocopy", "--entrypoint", "stat", PG_IMAGE,
                  "-c", "%u:%g %a", "/v").splitlines()[-1]


def ip_probe() -> None:
    # Prove static --ip is accepted on the services network BEFORE removing anything.
    cid = docker("create", "--pull", "never", "--network", SERVICES_NETWORK, "--ip", "172.19.255.250",
                 "--label", "scientist.platform/purpose=b5-ip-probe", "--entrypoint", "true", PG_IMAGE)
    docker("rm", "-v", cid)  # also drops the anonymous volume the image VOLUME declaration creates


SERVICE_SPECS = {"scientist-b5-postgres": (PG_RUN, PG_VOLUME, PG_IMAGE, "70:70 700"),
                 "scientist-b5-minio": (MINIO_RUN, MINIO_VOLUME, MINIO_IMAGE, "501:501 700")}


def phase_services(apply: bool) -> None:
    """Idempotent and resumable; never removes a volume."""
    free = vm_free_kib()
    if free < MIN_FREE_KIB:
        raise RuntimeError(f"VM free space {free} KiB below 2 GiB floor; refusing")
    running = set(docker("ps", "-q", "--no-trunc", "--filter", "label=scientist.platform/run").split())
    if running - (set() if apply else set(RUN_CONTAINERS)):
        raise RuntimeError("a run container is running; refusing to replace shared test services")
    steps: list[tuple[str, list[str]]] = []
    for name, cid in OLD_SERVICES.items():
        c = inspect("container", name)
        if c is not None and c["Id"] == cid:
            if (c["Config"]["Labels"] or {}).get("scientist.platform/purpose") != "b5-services-test":
                raise RuntimeError(f"{name} label differs from the manifest")
            steps += [("old", ["stop", "--time", "20", cid]), ("old", ["rm", cid])]
    for volume, uid in ((PG_VOLUME, 70), (MINIO_VOLUME, 501)):
        v = inspect("volume", volume)
        if v is None:
            steps.append(("volume", ["volume", "create", "--label", "scientist.platform/purpose=b5-services-test", volume]))
        elif v.get("Driver") != "local" or (v.get("Labels") or {}).get("scientist.platform/purpose") != "b5-services-test":
            raise RuntimeError(f"existing volume {volume} is not ours; refusing")
        users = set(docker("ps", "-aq", "--no-trunc", "--filter", f"volume={volume}").split())
        allowed = {c["Id"] for n in SERVICE_SPECS if (c := inspect("container", n)) and c["Id"] not in OLD_SERVICES.values()}
        if users - allowed:
            raise RuntimeError(f"volume {volume} is used by an unexpected container; refusing")
        steps.append(("init", init_cmd(volume, uid)))
    print(json.dumps({"phase": "services", "vm_free_kib": free, "commands": [c for _, c in steps],
                      "then": [spec[0] for spec in SERVICE_SPECS.values()]}))
    if not apply:
        return
    ip_probe()
    for kind, cmd in steps:
        docker(*cmd)
    created = {}
    for name, (run_cmd, volume, image, want) in SERVICE_SPECS.items():
        c = inspect("container", name)
        if c is not None:
            labels = c["Config"]["Labels"] or {}
            mounts = {m.get("Name") for m in c.get("Mounts", [])}
            if labels.get("scientist.platform/purpose") != "b5-services-test" or c["Config"]["Image"] != image or volume not in mounts:
                raise RuntimeError(f"existing {name} is not the expected service; refusing")
            if not c["State"]["Running"]:
                docker("rm", c["Id"])  # keeps the volume
                c = None
        if c is None:
            docker(*run_cmd)
            c = inspect("container", name)
        got = stat_volume(volume)
        if got != want:
            raise RuntimeError(f"{volume} ownership/mode {got!r} differs from {want!r}")
        created[name] = {"id": c["Id"], "image": image, "volume": volume, "volume_stat": got,
                         "ip": c["NetworkSettings"]["Networks"][SERVICES_NETWORK]["IPAddress"]}
    record("services-created.json", {"services": created, "vm_free_kib_before": free,
                                     "volume_budget_bytes": VOLUME_BUDGET_BYTES, "min_free_kib": MIN_FREE_KIB,
                                     "replaced": OLD_SERVICES})


def phase_verify() -> None:
    """Persistence proof on the same containers; post-restart path is read-only and never recreates data."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import uuid
    from datetime import datetime, timezone

    import b5_matrix_common as c
    from botocore.exceptions import ClientError
    from sqlalchemy import create_engine, text

    def fail(reason: str) -> None:
        record(f"persistence-probe-{marker_id}.json", {"marker_id": marker_id, "status": "FAIL", "reason": reason})
        raise SystemExit(f"persistence FAIL: {reason}")

    marker_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
    body = f"b5 persistence probe {marker_id}\n".encode()
    digest = hashlib.sha256(body).hexdigest()
    key = f"b5-persistence-probe-{marker_id}"
    earlier = [json.loads(p.read_text()) for p in sorted(EVIDENCE.glob("persistence-probe-*.json"))]
    earlier = [e for e in earlier if e.get("status") == "PASS"]
    eng = create_engine(c.DB_URL, connect_args={"connect_timeout": 3})
    s3 = c.s3_client(fast=True)
    # Earlier markers must still exist before anything new is written.
    for e in earlier:
        try:
            with eng.connect() as conn:
                n = conn.execute(text("SELECT count(*) FROM b5_persistence_probe WHERE marker_id=:m AND sha256=:d"),
                                 {"m": e["marker_id"], "d": e["marker_sha256"]}).scalar_one()
            got = hashlib.sha256(s3.get_object(Bucket=c.BUCKET, Key=e["object_key"])["Body"].read()).hexdigest()
        except Exception as exc:
            fail(f"earlier marker {e['marker_id']} unreadable: {type(exc).__name__}")
        if n != 1 or got != e["marker_sha256"]:
            fail(f"earlier marker {e['marker_id']} lost")
    try:
        s3.head_bucket(Bucket=c.BUCKET)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchBucket"} or earlier:
            fail("bucket missing after a previous PASS" if earlier else "bucket head failed")
        s3.create_bucket(Bucket=c.BUCKET)
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS b5_persistence_probe (marker_id text PRIMARY KEY, sha256 text NOT NULL)"))
        if conn.execute(text("INSERT INTO b5_persistence_probe VALUES (:m, :d)"), {"m": marker_id, "d": digest}).rowcount != 1:
            fail("marker insert did not affect exactly one row")
    s3.put_object(Bucket=c.BUCKET, Key=key, Body=body)
    eng.dispose()
    before = {n: inspect("container", n) for n in SERVICE_SPECS}
    record(f"persistence-probe-{marker_id}.json", {"marker_id": marker_id, "marker_sha256": digest, "object_key": key,
                                                   "status": "PENDING_RESTART",
                                                   "containers": {n: (x["Id"], x["State"]["StartedAt"]) for n, x in before.items()}})
    docker("restart", "--time", "20", *SERVICE_SPECS)
    after = {n: inspect("container", n) for n in SERVICE_SPECS}
    for n in SERVICE_SPECS:
        if after[n]["Id"] != before[n]["Id"] or after[n]["State"]["StartedAt"] == before[n]["State"]["StartedAt"]:
            fail(f"{n} was not restarted in place")
    deadline, last = time.monotonic() + 120, "timeout"
    while True:  # read-only: no CREATE, no bucket creation, no writes
        try:
            eng = create_engine(c.DB_URL, connect_args={"connect_timeout": 3})
            with eng.connect() as conn:
                n_rows = conn.execute(text("SELECT count(*) FROM b5_persistence_probe WHERE marker_id=:m AND sha256=:d"),
                                      {"m": marker_id, "d": digest}).scalar_one()
            got = hashlib.sha256(c.s3_client(fast=True).get_object(Bucket=c.BUCKET, Key=key)["Body"].read()).hexdigest()
            eng.dispose()
            break
        except Exception as exc:
            last = type(exc).__name__
            if time.monotonic() > deadline:
                fail(f"post-restart read failed: {last}")
            time.sleep(2)
    sizes, free = volume_kib(), vm_free_kib()
    ok = n_rows == 1 and got == digest and sum(sizes.values()) * 1024 <= VOLUME_BUDGET_BYTES and free >= MIN_FREE_KIB
    record(f"persistence-probe-{marker_id}.json", {
        "marker_id": marker_id, "marker_sha256": digest, "object_key": key, "db_row_survived_restart": n_rows == 1,
        "object_sha256_after_restart": got, "earlier_markers_verified": [e["marker_id"] for e in earlier],
        "containers_before": {n: (x["Id"], x["State"]["StartedAt"]) for n, x in before.items()},
        "containers_after": {n: (x["Id"], x["State"]["StartedAt"]) for n, x in after.items()},
        "volume_kib": sizes, "vm_free_kib": free, "status": "PASS" if ok else "FAIL"})
    print(json.dumps({"persistence": "PASS" if ok else "FAIL", "marker_id": marker_id, "vm_free_kib": free, "volumes": sizes}))
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "plan"
    apply = "--owner-approved" in sys.argv
    preflight()
    if phase == "plan":
        phase_cleanup(False)
        phase_services(False)
    elif phase == "cleanup" and apply:
        phase_cleanup(True)
    elif phase == "services" and apply:
        phase_services(True)
    elif phase == "verify" and apply:
        phase_verify()
    else:
        raise SystemExit("usage: plan | cleanup|services|verify --owner-approved")
