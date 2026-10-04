import base64
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from scientist import broker, checkpoints, objects, supervisor
from scientist.auth import DomainError
from scientist.contracts import OperationRequest
from scientist.private_worker_api import RuntimePins, WorkerController
from scientist.runtime_contracts import (
    RUNTIME_COMMIT, RuntimeContextV1, operation_fingerprint,
)
from test_broker import broker_fixture
from test_runtime_recovery import _MemoryS3


@pytest.fixture
def crashed(broker_fixture, monkeypatch, tmp_path):
    """Generation 1 committed one operation and a checkpoint; generation 2 claimed."""
    db, _, run_id, _, _ = broker_fixture
    s3 = _MemoryS3()
    monkeypatch.setattr(objects, "_client", lambda: s3)
    pins = RuntimePins(image_digest="sha256:" + "b" * 64, skills_digest="c" * 64,
                       environment_digest="d" * 64)
    monkeypatch.setattr(checkpoints, "_trusted_pins", (
        pins.image_digest, pins.skills_digest, pins.environment_digest, RUNTIME_COMMIT))
    row = db.execute(text("SELECT * FROM runs WHERE id=:r"), {"r": run_id}).one()
    plan = broker._load_plan(db, run_id, row.revision)
    db.execute(text("INSERT INTO credentials (id, project_id, label, provider, encrypted_value) VALUES (:id, :project, 'fixture', 'fixture', :value)"),
               {"id": plan.provider_id, "project": row.project_id, "value": b"fixture-secret"})
    request = OperationRequest(
        run_id=run_id, generation=1, operation_id="operation-1", kind="llm", reserve_tokens=200,
        payload={"provider_id": str(plan.provider_id), "model": plan.model, "recipient": "https://research.example",
                 "credential_id": str(plan.provider_id), "prompt": "q", "max_output_tokens": 1})
    cap = broker.issue_capability(db, run_id, 1, 300)
    first = broker.execute(db, cap, request)
    assert first.state == "committed"
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "result.txt").write_bytes(b"durable \x00 result")
    data = (ws / "result.txt").read_bytes()
    turn_id = str(uuid4())
    request2 = request.model_copy(update={"operation_id": "operation-2"})
    context = RuntimeContextV1.model_validate(dict(
        schema_version=1, run_id=str(run_id), project_id=str(row.project_id), generation=1,
        revision=row.revision, input_snapshot_digest=plan.input_snapshot_digest,
        plan_digest=row.plan_digest.strip(), runtime_commit=RUNTIME_COMMIT,
        image_digest=pins.image_digest, skills_digest=pins.skills_digest,
        environment_digest=pins.environment_digest, provider_id=str(plan.provider_id),
        provider_endpoint="https://research.example", model=plan.model,
        plan=plan.model_dump(mode="json"), turn_id=turn_id, system_prompt="p",
        messages=[{"role": "user", "content": "q"}], todo={"todos": [], "revision": 0},
        compacted_context=None, boundary="before_model", pending_assistant=None,
        operation_mappings=[dict(operation_id=request.operation_id, turn_id=turn_id,
                                 purpose="compression", model_sequence=0, tool_call_id=None,
                                 request=request.model_dump(mode="json"),
                                 payload_hash=operation_fingerprint(request)),
                            dict(operation_id="operation-2", turn_id=turn_id,
                                 purpose="model", model_sequence=1, tool_call_id=None,
                                 request=request2.model_dump(mode="json"),
                                 payload_hash=operation_fingerprint(request2))],
        operation_sequence=2,
        workspace_manifest=[dict(path="result.txt", sha256=hashlib.sha256(data).hexdigest(), size=len(data))]))
    manifest = checkpoints.capture(db, run_id, 1, context.model_dump_json().encode(), ws)
    db.execute(text("UPDATE runs SET generation=2 WHERE id=:r"), {"r": run_id})
    db.commit()
    controller = WorkerController(pins=pins, provider_destinations={plan.provider_id: "https://research.example"})
    return SimpleNamespace(db=db, run_id=run_id, controller=controller, manifest=manifest, data=data, s3=s3,
                           context=context, ws=ws, request=request, pins=pins, plan=plan, first=first)


def _accounting(db, run_id):
    return (
        db.execute(text("SELECT usage_tokens, reserved_tokens FROM runs WHERE id=:r"), {"r": run_id}).one(),
        db.execute(text("SELECT * FROM operations WHERE run_id=:r ORDER BY operation_id"), {"r": run_id}).all(),
    )


def test_continuation_bootstrap_restores_latest_checkpoint_for_new_generation(crashed):
    db, run_id, controller, manifest, data = (crashed.db, crashed.run_id, crashed.controller,
                                              crashed.manifest, crashed.data)
    before = _accounting(db, run_id)
    boot = supervisor.continuation_bootstrap(db, run_id, 2, controller)
    context = RuntimeContextV1.model_validate_json(boot.context)
    assert context.generation == 2
    assert context.revision == db.execute(text("SELECT revision FROM runs WHERE id=:r"), {"r": run_id}).scalar_one()
    assert [m.operation_id for m in context.operation_mappings] == ["operation-1", "operation-2"]
    assert context.operation_sequence == 2
    assert context.operation_mappings[0].request.generation == 1  # committed keeps original generation
    assert context.operation_mappings[1].request.generation == 2  # never sent: rebound
    assert boot.metadata.checkpoint_revision == manifest.revision == 1
    assert [f.decoded_data() for f in boot.workspace] == [data]
    assert [f.path for f in boot.workspace] == ["result.txt"]
    assert boot.workspace[0].data_base64 == base64.b64encode(data).decode()
    assert _accounting(db, run_id) == before


def test_continuation_bootstrap_without_checkpoint_raises(broker_fixture):
    db, _, run_id, _, _ = broker_fixture
    controller = WorkerController(pins=RuntimePins(image_digest="sha256:" + "b" * 64, skills_digest="c" * 64,
                                                   environment_digest="d" * 64), provider_destinations={})
    with pytest.raises(RuntimeError, match="no checkpoint"):
        supervisor.continuation_bootstrap(db, run_id, 1, controller)


def test_continuation_bootstrap_corrupt_object_raises_and_keeps_rows(crashed):
    db, run_id, controller, manifest, s3 = (crashed.db, crashed.run_id, crashed.controller,
                                            crashed.manifest, crashed.s3)
    s3.data[(objects.BUCKET, manifest.workspace[0].key)] = b"corrupt"
    before = _accounting(db, run_id)
    with pytest.raises(checkpoints.CheckpointIntegrityError):
        supervisor.continuation_bootstrap(db, run_id, 2, controller)
    assert _accounting(db, run_id) == before


def test_continuation_bootstrap_missing_object_raises(crashed):
    db, run_id, controller, manifest, s3 = (crashed.db, crashed.run_id, crashed.controller,
                                            crashed.manifest, crashed.s3)
    del s3.data[(objects.BUCKET, manifest.context.key)]
    with pytest.raises(checkpoints.CheckpointIntegrityError):
        supervisor.continuation_bootstrap(db, run_id, 2, controller)


@pytest.mark.parametrize("generation", [1, 3])
def test_continuation_bootstrap_wrong_generation_raises(crashed, generation):
    db, run_id, controller = crashed.db, crashed.run_id, crashed.controller
    before = _accounting(db, run_id)
    with pytest.raises(DomainError):
        supervisor.continuation_bootstrap(db, run_id, generation, controller)
    assert _accounting(db, run_id) == before


def test_generation_two_replay_returns_stored_result_without_new_reservation(crashed):
    db, run_id = crashed.db, crashed.run_id
    boot = supervisor.continuation_bootstrap(db, run_id, 2, crashed.controller)
    mapping = RuntimeContextV1.model_validate_json(boot.context).operation_mappings[0]
    before = _accounting(db, run_id)
    cap = broker.issue_capability(db, run_id, 2, 300)
    replay = broker.execute(db, cap, mapping.request.model_copy(update={"generation": 2}))
    db.commit()
    assert replay == crashed.first and replay.state == "committed"
    assert _accounting(db, run_id) == before


def test_latest_of_two_checkpoints_is_used(crashed):
    db, run_id, ws = crashed.db, crashed.run_id, crashed.ws
    new = b"second checkpoint"
    (ws / "result.txt").write_bytes(new)
    body = crashed.context.model_dump(mode="json")
    body["workspace_manifest"] = [dict(path="result.txt", sha256=hashlib.sha256(new).hexdigest(), size=len(new))]
    db.execute(text("UPDATE runs SET generation=1 WHERE id=:r"), {"r": run_id})
    checkpoints.capture(db, run_id, 1, RuntimeContextV1.model_validate(body).model_dump_json().encode(), ws)
    db.execute(text("UPDATE runs SET generation=2 WHERE id=:r"), {"r": run_id})
    db.commit()
    boot = supervisor.continuation_bootstrap(db, run_id, 2, crashed.controller)
    assert boot.metadata.checkpoint_revision == 2
    assert [f.decoded_data() for f in boot.workspace] == [new]


def test_controller_pins_mismatch_raises(crashed):
    bad = WorkerController(pins=RuntimePins(image_digest="sha256:" + "e" * 64, skills_digest="c" * 64,
                                            environment_digest="d" * 64),
                           provider_destinations={crashed.plan.provider_id: "https://research.example"})
    before = _accounting(crashed.db, crashed.run_id)
    with pytest.raises(DomainError):
        supervisor.continuation_bootstrap(crashed.db, crashed.run_id, 2, bad)
    assert _accounting(crashed.db, crashed.run_id) == before


def test_real_supervisor_start_accepts_continuation_bootstrap(crashed, monkeypatch):
    db, run_id, pins = crashed.db, crashed.run_id, crashed.pins
    installed = {}

    class Engine:
        def engine_id(self): return "r2-engine"
        def create_run_network(self, *a): return "r2-net", "172.29.42.2"
        def create_worker(self, *a): return "a" * 64, "r2-engine"
        def start_worker(self, cid): pass
        def install_network_policy(self, cid, ip, port): return {"ok": True}
        def install_bootstrap(self, cid, bootstrap, capability):
            installed["files"], _ = supervisor._bootstrap_files(bootstrap, capability)
            installed["bootstrap"] = bootstrap
        def release_worker(self, cid): pass

    class Dispatch:
        def start(self, db, run_id, generation, network, broker_ip, executor_id, incarnation, *, before_mutation):
            before_mutation("r2-engine", None)
            return supervisor.ExecutorRef(executor_id, run_id, generation, "dispatch", None,
                                          incarnation, "r2-engine", "b" * 64)
        def ready(self, *a): return True

    monkeypatch.setattr(supervisor, "_config", None)
    supervisor.configure(
        image="registry.invalid/w@" + pins.image_digest, image_digest=pins.image_digest,
        broker_url="http://172.29.42.1:8080", broker_ip="172.29.42.1", broker_port=8080,
        runtime_commit=RUNTIME_COMMIT, skills_digest=pins.skills_digest,
        environment_digest=pins.environment_digest,
        bootstrap_factory=lambda d, r, g: supervisor.continuation_bootstrap(d, r, g, crashed.controller),
        capability_factory=lambda *_: "cap", dispatch=Dispatch(), engine=Engine())
    assert supervisor.start(db, run_id, 2) == "a" * 64
    workspace = json.loads(installed["files"]["workspace.json"])
    assert [base64.b64decode(item["data_base64"]) for item in workspace] == [crashed.data]
    assert json.loads(installed["files"]["metadata.json"])["checkpoint_revision"] == 1
