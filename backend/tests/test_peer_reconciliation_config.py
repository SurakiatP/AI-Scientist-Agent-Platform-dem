from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from uuid import UUID, uuid4
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from scientist import private_dispatch_entrypoint
from scientist.dispatch_runtime import (
    DockerDispatchRuntime,
    DispatchIdentity,
    DispatchServiceConfig,
    DispatchTemplate,
    PeerReconciliationTarget,
    _parse_template,
    parse_dispatch_config,
)
from scientist.supervisor import ExecutorRef
from scientist.runtime_contracts import RUNTIME_COMMIT


PEER_ID = UUID("abcdefab-cdef-4abc-8def-abcdefabcdef")
OPERATION_ID = "peer-op-123"
ATTEMPT = 2


def _identity_values(**overrides):
    values = {
        "schema_version": 1,
        "generation": 1,
        "engine_id": "engine-1",
        "run_id": uuid4(),
        "executor_id": uuid4(),
        "process_incarnation": uuid4(),
        "runtime_commit": RUNTIME_COMMIT,
        "image_digest": "sha256:" + "a" * 64,
        "skills_digest": "b" * 64,
        "environment_digest": "c" * 64,
        "provider_destinations": {},
        "peer_destinations": {PEER_ID: "https://peer.example"},
        "secret_files": {
            name: name
            for name in (
                "database_url",
                "broker_capability_key",
                "master_key",
                "s3_access_key",
                "s3_secret_key",
            )
        },
    }
    values.update(overrides)
    return values


def _template_values():
    return {
        "schema_version": 1,
        "runtime_commit": RUNTIME_COMMIT,
        "image_digest": "sha256:" + "a" * 64,
        "skills_digest": "b" * 64,
        "environment_digest": "c" * 64,
        "provider_destinations": {},
        "peer_destinations": {PEER_ID: "https://peer.example"},
        "secret_files": {
            name: name
            for name in (
                "database_url",
                "broker_capability_key",
                "master_key",
                "s3_access_key",
                "s3_secret_key",
            )
        },
    }


def test_reconciliation_target_is_frozen_bounded_and_has_no_remote_authority_fields():
    target = PeerReconciliationTarget(operation_id=OPERATION_ID, attempt=ATTEMPT)
    assert target.model_dump() == {"operation_id": OPERATION_ID, "attempt": ATTEMPT}
    with pytest.raises(ValidationError):
        PeerReconciliationTarget(operation_id="  ", attempt=1)
    with pytest.raises(ValidationError):
        PeerReconciliationTarget(operation_id="x" * 201, attempt=1)
    for attempt in (0, 11, False):
        with pytest.raises(ValidationError):
            PeerReconciliationTarget(operation_id=OPERATION_ID, attempt=attempt)
    for extra in ("remote_task_id", "url", "credential"):
        with pytest.raises(ValidationError):
            PeerReconciliationTarget(operation_id=OPERATION_ID, attempt=1, **{extra: "untrusted"})
    with pytest.raises(ValidationError):
        target.attempt = 3


def test_dispatch_identity_defaults_to_effects_and_enforces_reconciliation_pairing():
    effects = DispatchIdentity.model_validate(_identity_values())
    assert effects.mode == "effects"
    assert effects.peer_reconciliation is None

    target = PeerReconciliationTarget(operation_id=OPERATION_ID, attempt=ATTEMPT)
    identity = DispatchIdentity.model_validate(
        _identity_values(mode="peer_get_task", peer_reconciliation=target)
    )
    parsed = parse_dispatch_config(identity.model_dump_json().encode())
    assert parsed.mode == "peer_get_task"
    assert parsed.peer_reconciliation == target
    with pytest.raises(ValidationError):
        DispatchIdentity.model_validate(_identity_values(mode="peer_get_task"))
    with pytest.raises(ValidationError):
        DispatchIdentity.model_validate(
            _identity_values(mode="effects", peer_reconciliation=target)
        )
    with pytest.raises(ValidationError):
        DispatchIdentity.model_validate(_identity_values(mode="send_message"))


def test_static_template_cannot_select_reconciliation_job():
    with pytest.raises(ValidationError):
        DispatchTemplate.model_validate({**_template_values(), "mode": "peer_get_task"})
    with pytest.raises(ValidationError):
        DispatchTemplate.model_validate(
            {**_template_values(), "peer_reconciliation": {"operation_id": OPERATION_ID, "attempt": 1}}
        )


def _dispatch_runtime(tmp_path: Path) -> DockerDispatchRuntime:
    image_digest = "sha256:" + "a" * 64
    return DockerDispatchRuntime(
        DispatchServiceConfig(
            image=f"registry.invalid/dispatch@{image_digest}",
            image_digest=image_digest,
            service_network="scientist-b5-services-test",
            config_path="/run/scientist/dispatch/config.json",
            secrets_dir="/run/scientist/secrets",
            host_config_file=str(tmp_path / "dispatch-template.json"),
            host_secrets_dir=str(tmp_path / "secrets"),
            launcher_dir=str(tmp_path / "launches"),
        )
    )


def test_launch_materialization_derives_reconciliation_from_runtime_target(tmp_path):
    template_path = tmp_path / "dispatch-template.json"
    template_path.write_text(DispatchTemplate.model_validate(_template_values()).model_dump_json())
    template_path.chmod(0o444)
    runtime = _dispatch_runtime(tmp_path)
    target = PeerReconciliationTarget(operation_id=OPERATION_ID, attempt=ATTEMPT)
    launch_path = runtime._materialize_launch_config(
        uuid4(), 1, uuid4(), uuid4(), "engine-1", peer_reconciliation=target
    )
    launch = parse_dispatch_config(launch_path.read_bytes())
    assert launch.mode == "peer_get_task"
    assert launch.peer_reconciliation == target
    assert json.loads(launch_path.read_text())["peer_destinations"] == {str(PEER_ID): "https://peer.example"}


def test_find_qualifies_physical_dispatch_by_reconciliation_operation_and_attempt(tmp_path, monkeypatch):
    runtime = _dispatch_runtime(tmp_path)
    target = PeerReconciliationTarget(operation_id=OPERATION_ID, attempt=ATTEMPT)
    run_id, executor_id, incarnation = uuid4(), uuid4(), uuid4()
    container_id = "d" * 64
    image_digest = runtime.config.image_digest
    monkeypatch.setattr(runtime, "engine_id", lambda: "engine-1")
    monkeypatch.setattr(runtime, "_verified_image_id", lambda: image_digest)
    commands = []

    def docker(*args):
        commands.append(args)
        if args[0] == "ps":
            return container_id
        return "|".join(
            (
                str(run_id), "1", str(executor_id), str(incarnation), "dispatch",
                "peer_get_task", OPERATION_ID, str(ATTEMPT), container_id,
                image_digest, runtime.config.image,
            )
        )

    monkeypatch.setattr(runtime, "_docker", docker)
    ref = runtime.find(
        None, run_id, 1, executor_id, OPERATION_ID, incarnation,
        peer_reconciliation=target,
    )
    assert ref is not None and ref.operation_id == OPERATION_ID
    assert "scientist.platform/peer-reconciliation-attempt" in commands[-1][2]

    monkeypatch.setattr(runtime, "_docker", lambda *args: "|".join((
        str(run_id), "1", str(executor_id), str(incarnation), "dispatch",
        "peer_get_task", OPERATION_ID, "1", container_id, image_digest, runtime.config.image,
    )) if args[0] == "inspect" else container_id)
    with pytest.raises(RuntimeError):
        runtime.find(None, run_id, 1, executor_id, OPERATION_ID, incarnation, peer_reconciliation=target)


def test_start_accepts_existing_reconciliation_ref_only_for_same_operation(tmp_path, monkeypatch):
    runtime = _dispatch_runtime(tmp_path)
    target = PeerReconciliationTarget(operation_id=OPERATION_ID, attempt=ATTEMPT)
    run_id, executor_id, incarnation = uuid4(), uuid4(), uuid4()
    ref = runtime._ref(run_id, 1, executor_id, incarnation, "f" * 64, "engine-1", OPERATION_ID, target)
    calls = []
    monkeypatch.setattr(runtime, "engine_id", lambda: "engine-1")
    monkeypatch.setattr(runtime, "_verified_image_id", lambda: runtime.config.image_digest)
    monkeypatch.setattr(runtime, "_checked_egress_and_subnet", lambda: None)
    monkeypatch.setattr(runtime, "find", lambda *_args, **_kwargs: ref)
    monkeypatch.setattr(runtime, "_verify_container_image", lambda *_args: calls.append("verify"))
    monkeypatch.setattr(runtime, "_sync_egress_attachment", lambda *_args: calls.append("sync"))
    monkeypatch.setattr(runtime, "_docker", lambda *args: calls.append(args) or "")
    monkeypatch.setattr(runtime, "_ensure_run_network", lambda *_args: calls.append("network"))
    result = runtime.start(
        None, run_id, 1, "scientist-run-aaaaaaaaaaaa-g1", "172.29.42.2",
        executor_id, incarnation, before_mutation=lambda *_args: calls.append("proof"),
        peer_reconciliation=target,
    )
    assert result == ref
    assert calls == ["verify", "proof", "sync", ("start", ref.container_id), "network"]
    assert all(not (isinstance(call, tuple) and call[0] == "create") for call in calls)

    wrong_ref = ExecutorRef(executor_id, run_id, 1, "dispatch", "other-operation", incarnation, "engine-1", "f" * 64)
    monkeypatch.setattr(runtime, "find", lambda *_args, **_kwargs: wrong_ref)
    with pytest.raises(RuntimeError):
        runtime.start(
            None, run_id, 1, "scientist-run-aaaaaaaaaaaa-g1", "172.29.42.2",
            executor_id, incarnation, before_mutation=lambda *_args: calls.append("bad-proof"),
            peer_reconciliation=target,
        )
    assert "bad-proof" not in calls


@pytest.mark.parametrize("wrong_labels", [False, True])
def test_reconciliation_inactive_probe_requires_exact_physical_scope(tmp_path, monkeypatch, wrong_labels):
    runtime = _dispatch_runtime(tmp_path)
    target = PeerReconciliationTarget(operation_id=OPERATION_ID, attempt=ATTEMPT)
    run_id, executor_id, incarnation = uuid4(), uuid4(), uuid4()
    container_id = "c" * 64
    ref = runtime._ref(run_id, 1, executor_id, incarnation, container_id, "engine-1", OPERATION_ID, target)
    monkeypatch.setattr(runtime, "engine_id", lambda: "engine-1")
    wrong_operation = "wrong-operation" if wrong_labels else OPERATION_ID

    def docker(*args):
        if args[0] == "ps":
            return container_id
        return "|".join((
            str(incarnation), "false", container_id, "peer_get_task", wrong_operation,
            str(ATTEMPT + (1 if wrong_labels else 0)),
        ))

    monkeypatch.setattr(runtime, "_docker", docker)
    assert runtime._probe_inactive(incarnation, "engine-1", container_id, executor=ref) is (not wrong_labels)


@pytest.mark.parametrize("wrong_labels", [False, True])
def test_reconciliation_stop_never_stops_mismatched_physical_scope(tmp_path, monkeypatch, wrong_labels):
    runtime = _dispatch_runtime(tmp_path)
    target = PeerReconciliationTarget(operation_id=OPERATION_ID, attempt=ATTEMPT)
    run_id, executor_id, incarnation = uuid4(), uuid4(), uuid4()
    container_id = "b" * 64
    ref = runtime._ref(run_id, 1, executor_id, incarnation, container_id, "engine-1", OPERATION_ID, target)
    monkeypatch.setattr(runtime, "engine_id", lambda: "engine-1")
    stops = []
    wrong_operation = "wrong-operation" if wrong_labels else OPERATION_ID

    removed = False
    running = True

    def docker(*args):
        nonlocal removed, running
        if args[0] == "ps":
            return "" if removed else container_id
        if args[0] == "stop":
            stops.append(args)
            running = False
            return ""
        if args[0] == "rm":
            removed = True
            return ""
        return "|".join((
            str(executor_id), str(run_id), "1", "dispatch", str(incarnation),
            "peer_get_task", wrong_operation, str(ATTEMPT + (1 if wrong_labels else 0)),
            container_id, "true" if running else "false",
        ))

    monkeypatch.setattr(runtime, "_docker", docker)
    assert runtime.stop(None, ref, 0) is (not wrong_labels)
    assert bool(stops) is (not wrong_labels)


def test_start_labels_reconciliation_container_before_first_mutation(tmp_path, monkeypatch):
    runtime = _dispatch_runtime(tmp_path)
    target = PeerReconciliationTarget(operation_id=OPERATION_ID, attempt=ATTEMPT)
    run_id, executor_id, incarnation = uuid4(), uuid4(), uuid4()
    container_id = "e" * 64
    mutations = []
    docker_calls = []
    monkeypatch.setattr(runtime, "engine_id", lambda: "engine-1")
    monkeypatch.setattr(runtime, "_verified_image_id", lambda: runtime.config.image_digest)
    monkeypatch.setattr(runtime, "_checked_egress_and_subnet", lambda: None)
    monkeypatch.setattr(runtime, "find", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "_materialize_launch_config", lambda *args, **kwargs: "/tmp/launch.json")
    monkeypatch.setattr(runtime, "_secret_mounts", lambda: [])
    monkeypatch.setattr(runtime, "_ensure_run_network", lambda *_args: None)

    def docker(*args):
        docker_calls.append(args)
        if args[0] == "create":
            return container_id
        return "|".join((
            container_id, "true", str(run_id), "1", str(executor_id), "dispatch",
            str(incarnation), "peer_get_task", OPERATION_ID, str(ATTEMPT),
            runtime.config.image_digest, runtime.config.image,
        ))

    monkeypatch.setattr(runtime, "_docker", docker)
    ref = runtime.start(
        None, run_id, 1, "scientist-run-aaaaaaaaaaaa-g1", "172.29.42.2",
        executor_id, incarnation,
        before_mutation=lambda engine_id, existing: mutations.append((engine_id, existing)),
        peer_reconciliation=target,
    )
    create_args = docker_calls[0]
    assert f"scientist.platform/dispatch-mode=peer_get_task" in create_args
    assert f"scientist.platform/operation={OPERATION_ID}" in create_args
    assert f"scientist.platform/peer-reconciliation-attempt={ATTEMPT}" in create_args
    assert mutations == [("engine-1", None)]
    assert ref.operation_id == OPERATION_ID


def test_reconciliation_entrypoint_runs_only_trusted_recovery_callback(monkeypatch):
    target = PeerReconciliationTarget(operation_id=OPERATION_ID, attempt=ATTEMPT)
    identity = DispatchIdentity.model_validate(
        _identity_values(mode="peer_get_task", peer_reconciliation=target)
    )
    monkeypatch.setattr(private_dispatch_entrypoint, "_CONFIG", types.SimpleNamespace(read_bytes=identity.model_dump_json().encode))
    secret_values = {
        "database_url": "postgresql+psycopg://scientist@database/scientist",
        "broker_capability_key": "k" * 32,
        "master_key": "m" * 32,
        "s3_access_key": "a" * 32,
        "s3_secret_key": "s" * 32,
    }
    monkeypatch.setattr(private_dispatch_entrypoint, "_read_secret", lambda name: secret_values[name].encode())
    monkeypatch.setattr(private_dispatch_entrypoint, "_wait_for_active_executor", lambda config, container: events.append(("ready", config.executor_id, container)))
    monkeypatch.setattr(private_dispatch_entrypoint.database, "DATABASE_URL", "before")
    monkeypatch.setattr(private_dispatch_entrypoint.objects, "configure", lambda *_args, **_kwargs: events.append(("objects",)))
    monkeypatch.setattr(private_dispatch_entrypoint.checkpoints, "configure_trusted_pins", lambda **_kwargs: events.append(("pins",)))
    monkeypatch.setattr(private_dispatch_entrypoint.broker, "configure", lambda **kwargs: events.append(("broker", kwargs)))
    monkeypatch.setattr(private_dispatch_entrypoint.Path, "read_text", lambda *_args, **_kwargs: "abcdefabcdef")
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *_args, **_kwargs: object()))
    monkeypatch.setattr(private_dispatch_entrypoint.uvicorn, "run", lambda *_args, **_kwargs: pytest.fail("recovery mode must not start HTTP"))
    monkeypatch.setattr(private_dispatch_entrypoint, "WorkerController", lambda **_kwargs: pytest.fail("recovery mode must not build a worker controller"))
    monkeypatch.setattr(private_dispatch_entrypoint, "create_private_app", lambda *_args, **_kwargs: pytest.fail("recovery mode must not mount private APIs"))
    callbacks = []
    monkeypatch.setattr(private_dispatch_entrypoint.broker, "reconcile_peer_from_dispatch", lambda config: callbacks.append(config), raising=False)
    events = []
    private_dispatch_entrypoint.main()
    assert [item[0] for item in events] == ["objects", "ready", "pins", "broker"]
    assert callbacks == [identity]
    assert events[-1][1]["peer_destinations"] == {str(PEER_ID): "https://peer.example"}


@pytest.mark.parametrize(
    ("mode", "operation_id", "attempt", "started"),
    [
        ("effects", None, None, False),
        ("peer_get_task", OPERATION_ID, ATTEMPT, False),
        ("peer_get_task", OPERATION_ID, ATTEMPT, True),
    ],
)
def test_private_readiness_requires_exact_executor_reconciliation_scope(
    monkeypatch, mode, operation_id, attempt, started
):
    target = PeerReconciliationTarget(operation_id=operation_id, attempt=attempt) if operation_id else None
    identity = DispatchIdentity.model_validate(
        _identity_values(mode=mode, peer_reconciliation=target)
    )
    row = SimpleNamespace(
        state="active",
        container_id="a" * 64,
        engine_id="engine-1",
        kind="dispatch",
        process_incarnation=identity.process_incarnation,
        run_id=identity.run_id,
        generation=identity.generation,
        operation_id=operation_id,
        peer_reconciliation_attempt=attempt,
        peer_reconciliation_started=started,
    )

    class Result:
        def one_or_none(self):
            return row

    class DB:
        def execute(self, statement, params):
            sql = str(statement)
            assert "operation_id" in sql
            assert "peer_reconciliation_attempt" in sql
            assert "peer_reconciliation_started" in sql
            return Result()

    class Session:
        def __enter__(self):
            return DB()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(private_dispatch_entrypoint, "session", Session)
    private_dispatch_entrypoint._wait_for_active_executor(identity, row.container_id, timeout=0.1)


@pytest.mark.parametrize(
    ("mode", "operation_id", "attempt", "started"),
    [
        ("effects", OPERATION_ID, None, False),
        ("effects", None, ATTEMPT, False),
        ("effects", None, None, True),
        ("peer_get_task", "different-operation", ATTEMPT, False),
        ("peer_get_task", OPERATION_ID, ATTEMPT + 1, False),
        ("peer_get_task", OPERATION_ID, ATTEMPT, "true"),
        ("peer_get_task", OPERATION_ID, ATTEMPT, 1),
    ],
)
def test_private_readiness_rejects_mismatched_or_malformed_reconciliation_scope(
    monkeypatch, mode, operation_id, attempt, started
):
    target = PeerReconciliationTarget(operation_id=OPERATION_ID, attempt=ATTEMPT) if mode == "peer_get_task" else None
    identity = DispatchIdentity.model_validate(
        _identity_values(mode=mode, peer_reconciliation=target)
    )
    row = SimpleNamespace(
        state="active", container_id="a" * 64, engine_id="engine-1", kind="dispatch",
        process_incarnation=identity.process_incarnation, run_id=identity.run_id,
        generation=identity.generation, operation_id=operation_id,
        peer_reconciliation_attempt=attempt, peer_reconciliation_started=started,
    )

    class Result:
        def one_or_none(self):
            return row

    class DB:
        def execute(self, *_args, **_kwargs):
            return Result()

    class Session:
        def __enter__(self):
            return DB()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(private_dispatch_entrypoint, "session", Session)
    with pytest.raises(RuntimeError, match="trusted config"):
        private_dispatch_entrypoint._wait_for_active_executor(identity, row.container_id, timeout=0.1)


def test_main_builds_s3_client_with_secret_file_credentials_and_path_style(monkeypatch):
    identity = DispatchIdentity.model_validate(_identity_values())
    monkeypatch.setattr(private_dispatch_entrypoint, "_CONFIG", types.SimpleNamespace(read_bytes=lambda: identity.model_dump_json().encode()))
    secrets = {
        "database_url": "postgresql+psycopg://scientist@database/scientist",
        "broker_capability_key": "broker-secret-from-file",
        "master_key": "master-secret-from-file",
        "s3_access_key": "s3-access-from-file",
        "s3_secret_key": "s3-secret-from-file",
    }
    monkeypatch.setattr(private_dispatch_entrypoint, "_read_secret", lambda name: secrets[name].encode())
    monkeypatch.setattr(private_dispatch_entrypoint, "_wait_for_active_executor", lambda *_args: None)
    monkeypatch.setattr(private_dispatch_entrypoint, "_configure_dispatch_runtime", lambda _identity: object())
    monkeypatch.setattr(private_dispatch_entrypoint, "_build_effects_app", lambda *_args: object())
    monkeypatch.setattr(private_dispatch_entrypoint.Path, "read_text", lambda *_args, **_kwargs: "abcdefabcdef")
    captured = {}
    monkeypatch.setattr(private_dispatch_entrypoint.objects, "configure", lambda client, **kwargs: captured.update(client=client, **kwargs))
    monkeypatch.setattr(private_dispatch_entrypoint.uvicorn, "run", lambda *_args, **_kwargs: None)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    private_dispatch_entrypoint.main()

    client = captured["client"]
    assert client.meta.service_model.service_name == "s3"
    assert client.meta.endpoint_url == "http://scientist-minio:9000"
    assert client.meta.config.s3["addressing_style"] == "path"
    assert client._request_signer._credentials.access_key == secrets["s3_access_key"]
    assert client._request_signer._credentials.secret_key == secrets["s3_secret_key"]
    assert captured["bucket"] == "scientist-b5"


def test_normal_effects_inactive_check_uses_durable_operation_binding(tmp_path, monkeypatch):
    from scientist import dispatch_runtime
    runtime = _dispatch_runtime(tmp_path)
    run_id, executor_id, incarnation = uuid4(), uuid4(), uuid4()
    effects = ExecutorRef(executor_id, run_id, 1, "dispatch", None, incarnation, "engine-1", "e" * 64)
    checked = []
    def check(db, run, operation, generation, *, probe):
        checked.append((db, run, operation, generation, callable(probe)))
        return True
    monkeypatch.setattr(dispatch_runtime, "dispatch_is_inactive", check)
    assert runtime.inactive("fixture-db", effects, OPERATION_ID) is True
    assert checked == [("fixture-db", run_id, OPERATION_ID, 1, True)]
