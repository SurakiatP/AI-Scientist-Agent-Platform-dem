from __future__ import annotations

import json
import io
import hashlib
import tarfile

import pytest
from types import SimpleNamespace

from scientist import supervisor
from scientist.supervisor import DockerWorkerEngine, ExecutorRef, _tar_bytes


def test_worker_image_pin_accepts_only_the_selected_immutable_image(monkeypatch):
    engine = DockerWorkerEngine()
    digest = "sha256:" + "a" * 64
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return digest + "|[]"

    monkeypatch.setattr(engine, "_docker", docker)

    assert engine._verified_image_id(digest, digest) == digest
    assert calls == [("image", "inspect", "--format", "{{.Id}}|{{json .RepoDigests}}", digest)]

    named = "example.invalid/scientist@" + digest
    monkeypatch.setattr(
        engine,
        "_docker",
        lambda *args, **kwargs: digest + "|[\"" + named + "\"]",
    )
    assert engine._verified_image_id(named, digest) == digest


def test_worker_image_pin_rejects_mismatched_raw_or_repo_digest(monkeypatch):
    engine = DockerWorkerEngine()
    digest = "sha256:" + "a" * 64
    other = "sha256:" + "b" * 64

    with pytest.raises(RuntimeError, match="worker image does not match configured immutable digest"):
        engine._verified_image_id(other, digest)

    monkeypatch.setattr(
        engine,
        "_docker",
        lambda *args, **kwargs: digest + "|[\"example.invalid/scientist:latest@" + digest + "\"]",
    )
    with pytest.raises(RuntimeError, match="worker image does not match configured immutable digest"):
        engine._verified_image_id("example.invalid/scientist@" + digest, digest)


def test_supervisor_dispatch_inactivity_uses_durable_binding_and_exact_probe(monkeypatch):
    db = object()
    ref = ExecutorRef(
        executor_id=supervisor.uuid4(),
        run_id=supervisor.uuid4(),
        generation=4,
        kind="dispatch",
        operation_id=None,
        process_incarnation=supervisor.uuid4(),
        engine_id="owned-engine",
        container_id="c" * 64,
    )
    observed = []

    class Dispatch:
        def inactive(self, db, executor, operation_id):
            observed.append((executor, operation_id))
            return True

    def authority(database, run_id, operation_id, generation, *, probe):
        assert database is db
        assert (run_id, operation_id, generation) == (ref.run_id, "op-1", 4)
        assert probe(ref.process_incarnation, ref.engine_id, ref.container_id)
        assert not probe(ref.process_incarnation, "other-engine", ref.container_id)
        assert not probe(ref.process_incarnation, ref.engine_id, "d" * 64)
        return True

    monkeypatch.setattr(supervisor, "dispatch_is_inactive", authority)

    assert supervisor._dispatch_operation_is_inactive(db, Dispatch(), ref, "op-1")
    assert observed == [(ref, "op-1")]


def test_recovered_unbound_dispatch_executor_binds_only_the_exact_physical_identity():
    run_id, executor_id, incarnation = supervisor.uuid4(), supervisor.uuid4(), supervisor.uuid4()
    ref = ExecutorRef(executor_id, run_id, 2, "dispatch", None, incarnation, "owned-engine", "a" * 64)
    row = {
        "id": executor_id,
        "run_id": run_id,
        "generation": 2,
        "kind": "dispatch",
        "operation_id": None,
        "process_incarnation": incarnation,
        "state": "starting",
        "container_id": None,
        "engine_id": None,
    }

    class Result:
        rowcount = 1

    class DB:
        def execute(self, statement, params):
            self.statement = str(statement)
            self.params = params
            return Result()

    db = DB()
    assert supervisor._bind_recovered_dispatch(db, row, ref)
    assert db.params["container"] == ref.container_id
    assert db.params["engine"] == ref.engine_id
    assert "run_id=:run" in db.statement
    assert "process_incarnation=:incarnation" in db.statement
    assert "state=:state" in db.statement


def test_recovered_dispatch_executor_rejects_conflicting_bound_identity():
    run_id, executor_id, incarnation = supervisor.uuid4(), supervisor.uuid4(), supervisor.uuid4()
    ref = ExecutorRef(executor_id, run_id, 2, "dispatch", None, incarnation, "owned-engine", "a" * 64)
    row = {"container_id": "b" * 64, "engine_id": "owned-engine", "state": "starting"}

    class DB:
        def execute(self, *args):
            raise AssertionError("conflicting bound identity must not be rebound")

    assert not supervisor._bind_recovered_dispatch(DB(), row, ref)


def test_worker_command_enforces_readonly_disk_and_bounded_memory_writes(monkeypatch):
    engine = DockerWorkerEngine()
    image_id = "sha256:" + "a" * 64
    created_id = "b" * 64
    commands = []
    write_payloads = []
    monkeypatch.setattr(supervisor, "_require_config", lambda: SimpleNamespace(image=image_id, image_digest=image_id))
    monkeypatch.setattr(engine, "engine_id", lambda: "owned-engine")
    files = {"context.json": b"context", "workspace.json": b"[]", "metadata.json": b"{}"}
    token = b"synthetic-capability"
    monkeypatch.setattr(supervisor, "_bootstrap_files", lambda *_args: (files, token))

    def docker(*args, **kwargs):
        commands.append(args)
        if args[:2] == ("image", "inspect"):
            return image_id + "|[]"
        if args[0] == "create":
            return created_id
        if args[:2] == ("inspect", "--format") and args[2] == "{{.Image}}":
            return image_id
        if args[0] == "exec":
            if args[2] == "0:65532":
                write_payloads.append(kwargs["input"])
                return ""
            if args[-1] == "/run/scientist/readiness/ready":
                return "ready"
            return json.dumps({
                path: hashlib.sha256(data).hexdigest()
                for path, data in zip(args[7:], [*files.values(), token], strict=True)
            })
        raise AssertionError(f"unexpected Docker command: {args}")

    monkeypatch.setattr(engine, "_docker", docker)
    result = engine.create_worker(
        image_id, supervisor.uuid4(), 1, supervisor.uuid4(), supervisor.uuid4(),
        "run-network", "http://172.29.32.2:8123",
    )

    assert result == (created_id, "owned-engine")
    expected = engine.install_bootstrap(created_id, object(), "synthetic-capability")
    assert len(expected) == 4
    assert any(command[:3] == ("exec", "--user", "65532:65532") for command in commands)
    engine.release_worker(created_id)
    writes = [command for command in commands if command[0] == "exec" and command[2] == "0:65532"]
    assert len(writes) == 2
    assert b"context.json" in write_payloads[0]
    assert b"readiness/ready" in write_payloads[1]
    create = next(command for command in commands if command[0] == "create")
    assert "--read-only" in create
    assert create[create.index("--cpus") + 1] == "1"
    assert create[create.index("--memory") + 1] == create[create.index("--memory-swap") + 1] == "1073741824"
    assert create[create.index("--pids-limit") + 1] == "128"
    assert create[create.index("--shm-size") + 1] == "16777216"
    writable_mounts = [create[i + 1] for i, value in enumerate(create[:-1]) if value == "--tmpfs"]
    assert any(mount.startswith("/workspace:rw,") and "size=67108864" in mount for mount in writable_mounts)
    assert all("size=" in mount for mount in writable_mounts)
    assert sum(int(mount.split("size=", 1)[1].split(",", 1)[0]) for mount in writable_mounts) < 300 * 1024 * 1024


def test_three_run_networks_have_disjoint_internal_address_spaces(monkeypatch):
    engine = DockerWorkerEngine()
    created = []

    def docker(*args, **kwargs):
        if args[:2] == ("network", "ls"):
            return ""
        if args[:2] == ("network", "create"):
            created.append(args)
            return f"network-{len(created)}"
        raise AssertionError(f"unexpected Docker command: {args}")

    monkeypatch.setattr(engine, "_docker", docker)
    result = [engine.create_run_network(supervisor.uuid4(), 1, supervisor.uuid4()) for _ in range(3)]

    assert len({network for network, _ in result}) == 3
    assert len({command[command.index("--subnet") + 1] for command in created}) == 3
    assert len({command[command.index("--subnet", command.index("--subnet") + 1) + 1] for command in created}) == 3
    assert all("--internal" in command and "--ipv6" in command for command in created)


def test_vm_nsenter_uses_sudo_only_for_verified_owned_worker(monkeypatch):
    engine = DockerWorkerEngine()
    container_id = "a" * 64
    calls = []

    def docker(*args, **kwargs):
        return f"{container_id}|4312|true|worker"

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(engine, "_docker", docker)
    monkeypatch.setattr("scientist.supervisor.subprocess.run", run)

    engine._vm(container_id, 4312, "iptables", "-w", "-F", "OUTPUT")

    assert calls == [[
        "colima", "ssh", "--profile", "scientist-platform-test", "--",
        "sudo", "-n", "nsenter", "-t", "4312", "-n",
        "iptables", "-w", "-F", "OUTPUT",
    ]]


def test_vm_nsenter_fails_closed_when_worker_identity_mismatches(monkeypatch):
    engine = DockerWorkerEngine()
    calls = []

    def docker(*args, **kwargs):
        return f"{'b' * 64}|4312|true|worker"

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(engine, "_docker", docker)
    monkeypatch.setattr("scientist.supervisor.subprocess.run", run)

    with pytest.raises(RuntimeError, match="owned worker namespace identity"):
        engine._vm("a" * 64, 4312, "iptables", "-w", "-F", "OUTPUT")

    assert calls == []


def test_worker_supervisor_refuses_shared_docker_targets():
    with pytest.raises(ValueError, match="owned test VM"):
        DockerWorkerEngine(context="colima", profile="default")
    with pytest.raises(ValueError, match="owned test VM"):
        DockerWorkerEngine(context="default", profile="scientist-platform-test")


def test_namespace_firewall_is_read_back_before_readiness(monkeypatch):
    engine = DockerWorkerEngine()
    calls = []

    def docker(*args, **kwargs):
        assert args[:2] == ("inspect", "--format")
        return json.dumps({"Pid": 4312, "Running": True})

    def vm(*args):
        calls.append(args)
        if args[-2:] == ("-t", "filter"):
            if "ip6tables-save" in args:
                return "*filter\n:OUTPUT DROP [0:0]\nCOMMIT"
            return "*filter\n:OUTPUT DROP [0:0]\n-A OUTPUT -d 172.29.77.2/32 -p tcp -m tcp --dport 8123 -m conntrack --ctstate NEW -j ACCEPT\n-A OUTPUT -d 127.0.0.11 -j DROP\nCOMMIT"
        return ""

    monkeypatch.setattr(engine, "_docker", docker)
    monkeypatch.setattr(engine, "_vm", vm)
    proof = engine.install_network_policy("a" * 64, "172.29.77.2", 8123)

    assert proof["readback"] is True
    assert proof["worker_pid"] == 4312
    assert all(call[0:2] == ("a" * 64, 4312) for call in calls)
    assert all(call[2] in {"iptables", "ip6tables", "iptables-save", "ip6tables-save"} for call in calls)
    assert any("-P" in call and "OUTPUT" in call and "DROP" in call for call in calls)
    assert any("127.0.0.11" in call for call in calls)
    commands = [call[2:] for call in calls]
    dns_drop = commands.index(("iptables", "-w", "-A", "OUTPUT", "-d", "127.0.0.11", "-j", "DROP"))
    loopback_accept = commands.index(("iptables", "-w", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"))
    assert dns_drop < loopback_accept


def test_real_engine_operations_are_explicitly_bound_to_owned_profile(monkeypatch):
    engine = DockerWorkerEngine()
    observed = []

    class Result:
        returncode = 0
        stdout = b"29.8.2"
        stderr = b""

    def run(args, **kwargs):
        observed.append(args)
        return Result()

    monkeypatch.setattr("scientist.supervisor.subprocess.run", run)
    assert engine._docker("version", "--format", "{{.Server.Version}}") == "29.8.2"
    assert observed == [["docker", "--context", "colima-scientist-platform-test",
                         "version", "--format", "{{.Server.Version}}"]]


def test_capability_mount_is_readable_only_to_worker_group():
    archive = tarfile.open(fileobj=io.BytesIO(_tar_bytes({
        "run/scientist/capability/token": b"fixture-capability",
        "run/scientist/readiness/ready": b"ready",
    })), mode="r:")
    token = archive.getmember("run/scientist/capability/token")
    ready = archive.getmember("run/scientist/readiness/ready")
    assert token.uid == 0 and token.gid == 65532 and token.mode == 0o440
    assert ready.uid == 0 and ready.gid == 0 and ready.mode == 0o444



def test_stale_bound_worker_is_recovered_when_exact_container_was_garbage_collected(monkeypatch):
    engine = DockerWorkerEngine()
    ref = ExecutorRef(supervisor.uuid4(), supervisor.uuid4(), 1, "worker", None,
                      supervisor.uuid4(), "owned-engine", "a" * 64)
    commands = []
    monkeypatch.setattr(engine, "engine_id", lambda: "owned-engine")

    def docker(*args, **kwargs):
        commands.append(args)
        assert args == ("ps", "-aq", "--no-trunc", "--filter", f"id={ref.container_id}")
        return ""

    monkeypatch.setattr(engine, "_docker", docker)
    assert engine.stop_worker(ref, 0) is True
    assert commands == [("ps", "-aq", "--no-trunc", "--filter", f"id={ref.container_id}")]

    checks = iter(("owned-engine", "changed-engine"))
    monkeypatch.setattr(engine, "engine_id", lambda: next(checks))
    assert engine.stop_worker(ref, 0) is False


def test_stop_worker_absence_proof_fails_closed_on_wrong_engine_or_inspection_error(monkeypatch):
    engine = DockerWorkerEngine()
    ref = ExecutorRef(supervisor.uuid4(), supervisor.uuid4(), 1, "worker", None,
                      supervisor.uuid4(), "owned-engine", "b" * 64)

    monkeypatch.setattr(engine, "engine_id", lambda: "different-engine")
    monkeypatch.setattr(engine, "_docker", lambda *_args, **_kwargs: pytest.fail("unexpected Docker access"))
    assert engine.stop_worker(ref, 0) is False

    monkeypatch.setattr(engine, "engine_id", lambda: "owned-engine")

    def inspection_error(*args, **kwargs):
        if args[0] == "ps":
            return ref.container_id
        raise RuntimeError("daemon inspection failed")

    monkeypatch.setattr(engine, "_docker", inspection_error)
    assert engine.stop_worker(ref, 0) is False
