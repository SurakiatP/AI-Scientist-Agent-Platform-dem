import base64
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from scientist import broker, checkpoints, limits, objects, supervisor
from scientist.contracts import ObjectRef, PlanSpec, Principal
from scientist.db import create_project, create_session
from scientist.domain import _event, approve_run, extend_run_budget, revise_plan, submit_run
from scientist.model_payload import llm_input_reserve
from scientist.runtime_adapter import BudgetExhausted, RuntimeAdapter
from scientist.auth import DomainError
from scientist.contracts import OperationRequest
from scientist.private_worker_api import RuntimePins, WorkerController
from scientist.runtime_contracts import (
    BootstrapMetadata, RUNTIME_COMMIT, RuntimeContextV1, operation_fingerprint,
)
from test_broker import broker_fixture
from test_runtime_recovery import _MemoryS3
from test_runtime_adapter import _checkpoint_ack, runtime_entrypoint


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


def _start(db, run_id, generation, pins, factory, monkeypatch):
    """Run the real supervisor.start with fake engines; return what it installed."""
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
        bootstrap_factory=factory, capability_factory=lambda *_: "cap", dispatch=Dispatch(), engine=Engine())
    assert supervisor.start(db, run_id, generation) == "a" * 64
    return installed


def test_real_supervisor_start_accepts_continuation_bootstrap(crashed, monkeypatch):
    installed = _start(crashed.db, crashed.run_id, 2, crashed.pins,
                       lambda d, r, g: supervisor.continuation_bootstrap(d, r, g, crashed.controller), monkeypatch)
    workspace = json.loads(installed["files"]["workspace.json"])
    assert [base64.b64decode(item["data_base64"]) for item in workspace] == [crashed.data]
    assert json.loads(installed["files"]["metadata.json"])["checkpoint_revision"] == 1


# ADR-012: trusted per-generation budget snapshot.

def _ledger_remaining(db, run_id):
    row = db.execute(text("SELECT token_limit, usage_tokens, reserved_tokens FROM runs WHERE id=:r"), {"r": run_id}).one()
    return row.token_limit - row.usage_tokens - row.reserved_tokens


def _recapture(crashed, body):
    """Store `body` (raw JSON dict) as the newest checkpoint context of generation 1."""
    db, run_id = crashed.db, crashed.run_id
    db.execute(text("UPDATE runs SET generation=1 WHERE id=:r"), {"r": run_id})
    checkpoints.capture(db, run_id, 1, json.dumps(body).encode(), crashed.ws)
    db.execute(text("UPDATE runs SET generation=2 WHERE id=:r"), {"r": run_id})
    db.commit()


@pytest.mark.parametrize("saved", ["absent", None, 0, 10**12])
def test_continuation_rebinds_budget_snapshot_from_ledger_usage_and_held_reservations(crashed, saved):
    """Pre-ADR checkpoints (no key) and any saved/forged value are rebound server-side."""
    db, run_id = crashed.db, crashed.run_id
    body = crashed.context.model_dump(mode="json")
    if saved == "absent":
        body.pop("budget_remaining_tokens")
    else:
        body["budget_remaining_tokens"] = saved
    _recapture(crashed, body)
    db.execute(text("UPDATE runs SET reserved_tokens=reserved_tokens+50 WHERE id=:r"), {"r": run_id})  # held unknown op
    db.commit()
    usage, reserved = _accounting(db, run_id)[0]
    assert (usage, reserved) == (3, 50)
    plan_before = db.execute(text("SELECT r.plan_digest, p.plan FROM runs r JOIN plan_revisions p "
                                  "ON p.run_id=r.id AND p.revision=r.revision WHERE r.id=:r"), {"r": run_id}).one()
    context = RuntimeContextV1.model_validate_json(
        supervisor.continuation_bootstrap(db, run_id, 2, crashed.controller).context)
    assert context.budget_remaining_tokens == 2000 - 3 - 50 == _ledger_remaining(db, run_id)
    assert context.plan == crashed.plan and context.plan.token_limit == 2000
    assert db.execute(text("SELECT r.plan_digest, p.plan FROM runs r JOIN plan_revisions p "
                           "ON p.run_id=r.id AND p.revision=r.revision WHERE r.id=:r"), {"r": run_id}).one() == plan_before


def test_supervisor_start_overrides_factory_budget_snapshot(crashed, monkeypatch):
    def forged(d, r, g):
        boot = supervisor.continuation_bootstrap(d, r, g, crashed.controller)
        body = json.loads(boot.context)
        body["budget_remaining_tokens"] = 10**12
        return supervisor.WorkerBootstrap(context=json.dumps(body).encode(), workspace=boot.workspace,
                                          metadata=boot.metadata)

    installed = _start(crashed.db, crashed.run_id, 2, crashed.pins, forged, monkeypatch)
    context = RuntimeContextV1.model_validate_json(installed["files"]["context.json"])
    assert context.budget_remaining_tokens == _ledger_remaining(crashed.db, crashed.run_id) == 2000 - 3


def test_stale_generation_context_still_rejected_with_budget_snapshot(crashed):
    db, run_id = crashed.db, crashed.run_id
    body = crashed.context.model_dump(mode="json")
    body["budget_remaining_tokens"] = 10**12
    _recapture(crashed, body)
    for generation in (1, 3):
        with pytest.raises(DomainError):
            crashed.controller.bootstrap_context(db, json.dumps(body).encode(), run_id, generation)


_PROVIDER_REPLY = json.dumps({
    "id": "chatcmpl-r8", "object": "chat.completion", "created": 1, "model": "fixture-model",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "extended answer"}, "finish_reason": "stop"}],
}).encode()


@pytest.fixture
def zero_plan(db):
    """A run whose immutable approved plan has token_limit 0, with a counting provider transport."""
    owner = Principal(identity=uuid4(), kind="owner")
    project_id = create_project(db, "zero budget")
    session_id = create_session(db, project_id, "session")
    provider_id = uuid4()
    run = submit_run(db, owner, project_id, session_id, f"zero-{uuid4().hex}", "question", [], provider_id, "fixture-model")
    snapshot = db.execute(text("SELECT digest FROM input_snapshots WHERE run_id=:r"), {"r": run.run_id}).scalar_one().strip()
    plan = PlanSpec(input_snapshot_digest=snapshot, provider_id=provider_id, model="fixture-model", stages=["search"],
                    allowed_ops=["llm"], data_recipients=["https://research.example"], packages=[],
                    token_limit=0, elapsed_limit_ms=600_000)
    run = revise_plan(db, owner, run.run_id, run.revision, plan)
    run = approve_run(db, owner, run.run_id, run.revision, run.plan_digest)
    db.execute(text("INSERT INTO credentials (id, project_id, label, provider, encrypted_value) "
                    "VALUES (:id, :project, 'fixture', 'fixture', :value)"),
               {"id": provider_id, "project": project_id, "value": b"fixture-secret"})
    db.commit()
    provider_calls, stored = [], {}

    def provider(request, target):
        provider_calls.append(request)
        return _PROVIDER_REPLY, 7

    def persist(_db, project, data, content_type):
        digest = hashlib.sha256(data).hexdigest()
        stored[f"r8/{digest}"] = data
        return ObjectRef(project_id=project, key=f"r8/{digest}", sha256=digest, size=len(data), content_type=content_type)

    broker.configure(transport=provider, persist_result=persist, capability_key=b"r" * 32,
                     resolver=lambda host, port: ["8.8.8.8"],
                     provider_destinations={str(provider_id): "https://research.example"})
    pins = RuntimePins(image_digest="sha256:" + "b" * 64, skills_digest="c" * 64, environment_digest="d" * 64)
    yield SimpleNamespace(db=db, owner=owner, run_id=run.run_id, plan=plan, project_id=project_id,
                          provider_calls=provider_calls, stored=stored, pins=pins)
    broker.configure(transport=None, persist_result=None, capability_key=None)


def _extend_zero_plan(z, token_limit):
    """Owner lifecycle: claim pauses the 0-budget run, the owner extends, recovery requeues it."""
    db, run_id = z.db, z.run_id
    row = db.execute(text("SELECT * FROM runs WHERE id=:r"), {"r": run_id}).mappings().one()
    if row["budget_decision_id"] is None:
        assert limits.budget_exhausted(db, row)
        limits.mark_budget_wait(db, run_id, row["revision"], _event)
        db.commit()
        row = db.execute(text("SELECT * FROM runs WHERE id=:r"), {"r": run_id}).mappings().one()
    extend_run_budget(db, z.owner, run_id, row["revision"], row["budget_decision_id"], f"ext-{token_limit}",
                      token_limit, row["elapsed_limit_ms"])
    supervisor._queue_recovered_run(db, run_id, row["generation"], row["revision"])
    db.commit()
    assert db.execute(text("SELECT state FROM runs WHERE id=:r"), {"r": run_id}).scalar_one() == "queued"
    assert broker._load_plan(db, run_id, row["revision"]).token_limit == 0  # approved plan is immutable


def _claim(z, generation):
    z.db.execute(text("UPDATE runs SET state='running', generation=:g, lease_expires_at=now()+interval '1 hour' "
                      "WHERE id=:r AND state='queued'"), {"r": z.run_id, "g": generation})
    z.db.commit()


def _fresh_bootstrap(z, system_prompt="p"):
    """Fresh-context factory as an operator harness builds it: no budget field at all."""
    def factory(db, run_id, generation):
        row = db.execute(text("SELECT * FROM runs WHERE id=:r"), {"r": run_id}).one()
        context = RuntimeContextV1.model_validate(dict(
            schema_version=1, run_id=str(run_id), project_id=str(z.project_id), generation=generation,
            revision=row.revision, input_snapshot_digest=z.plan.input_snapshot_digest,
            plan_digest=row.plan_digest.strip(), runtime_commit=RUNTIME_COMMIT, image_digest=z.pins.image_digest,
            skills_digest=z.pins.skills_digest, environment_digest=z.pins.environment_digest,
            provider_id=str(z.plan.provider_id), provider_endpoint="https://research.example", model=z.plan.model,
            plan=z.plan.model_dump(mode="json"), turn_id=str(uuid4()), system_prompt=system_prompt,
            messages=[{"role": "user", "content": "q"}], current_turn_user_index=0, todo={"todos": [], "revision": 0},
            compacted_context=None, boundary="before_model", pending_assistant=None, operation_mappings=[],
            operation_sequence=0, workspace_manifest=[]))
        body = context.model_dump(mode="json")
        del body["budget_remaining_tokens"]
        return supervisor.WorkerBootstrap(context=json.dumps(body, ensure_ascii=False).encode(), workspace=[],
                                          metadata=BootstrapMetadata(schema_version=1, checkpoint_revision=0))
    return factory


def _adapter(z, context, generation, tmp_path):
    """Worker adapter whose private broker routes go to the real broker (boundary acked in memory)."""
    capability = broker.issue_capability(z.db, z.run_id, generation, 300)
    refs = {}

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control/boundary":
            return httpx.Response(200, json=_checkpoint_ack(json.loads(request.content)))
        if request.url.path == "/effects":
            try:
                result = broker.execute(z.db, capability, OperationRequest.model_validate_json(request.content))
            except DomainError as exc:
                z.db.rollback()
                return httpx.Response(exc.status, json={"detail": {"code": exc.code}})
            if result.result is not None:
                refs[result.operation_id] = result.result.key
            return httpx.Response(200, content=result.model_dump_json())
        if request.url.path.endswith("/result"):
            return httpx.Response(200, content=z.stored[refs[request.url.path.split("/")[2]]])
        raise AssertionError(request.url.path)

    return RuntimeAdapter(context, broker_url="http://172.30.0.2:8000", capability=capability, workspace_dir=tmp_path,
                          broker_client=httpx.Client(transport=httpx.MockTransport(route), trust_env=False))


def test_zero_approved_plan_extension_fresh_generation_two_sends_allowance(zero_plan, monkeypatch, tmp_path):
    z = zero_plan
    _extend_zero_plan(z, 600)
    _claim(z, 2)
    installed = _start(z.db, z.run_id, 2, z.pins, _fresh_bootstrap(z), monkeypatch)
    context = RuntimeContextV1.model_validate_json(installed["files"]["context.json"])
    assert context.plan.token_limit == 0 and context.budget_remaining_tokens == 600
    messages = [{"role": "user", "content": "Summarize the extended evidence."}]
    adapter = _adapter(z, context, 2, tmp_path)
    adapter.dispatch_chat_completion({"model": "fixture-model", "messages": messages})
    allowance = 600 - llm_input_reserve(messages)
    assert 1 <= allowance <= 2048
    assert [call.payload["max_output_tokens"] for call in z.provider_calls] == [allowance]
    operation = z.db.execute(text("SELECT state, reserve_tokens FROM operations WHERE run_id=:r"), {"r": z.run_id}).one()
    assert tuple(operation) == ("committed", 600)  # the broker reserved exactly input + overhead + allowance
    assert _accounting(z.db, z.run_id)[0] == (7, 0)


def test_insufficient_allowance_makes_no_provider_call_and_recover_keeps_owner_wait(zero_plan, monkeypatch, tmp_path):
    z = zero_plan
    _extend_zero_plan(z, 50)  # >= 1 token remains, but less than any request's input reserve
    _claim(z, 2)
    db, run_id = z.db, z.run_id
    row = db.execute(text("SELECT * FROM runs WHERE id=:r"), {"r": run_id}).mappings().one()
    assert not limits.budget_exhausted(db, row), "a reserve-free ledger check alone would requeue forever"
    context = RuntimeContextV1.model_validate_json(_fresh_bootstrap(z)(db, run_id, 2).context)
    context = context.model_copy(update={"budget_remaining_tokens": limits.remaining_tokens(row)})
    adapter = _adapter(z, context, 2, tmp_path)
    with pytest.raises(BudgetExhausted):
        adapter.dispatch_chat_completion({"model": "fixture-model", "messages": [{"role": "user", "content": "q"}]})
    assert z.provider_calls == []
    assert db.execute(text("SELECT count(*) FROM operations WHERE run_id=:r"), {"r": run_id}).scalar_one() == 0
    waiting = db.execute(text("SELECT state, waiting_reason, budget_decision_id FROM runs WHERE id=:r"), {"r": run_id}).one()
    assert waiting[:2] == ("waiting_input", "budget_exhausted") and waiting.budget_decision_id is not None

    monkeypatch.setattr(supervisor, "_config", SimpleNamespace(engine=object(), dispatch=object()))
    recovered = supervisor.recover(db, run_id)
    assert (recovered.state, recovered.waiting_reason) == ("waiting_input", "budget_exhausted")
    assert db.execute(text("SELECT count(*) FROM events WHERE run_id=:r AND kind='decision.required' "
                           "AND payload->>'reason'='budget_exhausted' AND payload->>'decision_id'=:d"),
                      {"r": run_id, "d": str(waiting.budget_decision_id)}).scalar_one() == 1
    assert z.provider_calls == [] and _accounting(db, run_id)[0] == (0, 0)


def test_supervisor_start_keeps_utf8_context_within_worker_cap(zero_plan, monkeypatch, tmp_path):
    """Rebinding the snapshot must not \\u-escape non-ASCII text past the worker's 1 MiB read cap."""
    z = zero_plan
    _extend_zero_plan(z, 600)
    _claim(z, 2)
    prompt = "การวิจัย" * 30_000  # ~700 KB of Thai: ~1.4 MB if escaped as \\uXXXX
    assert 650_000 < len(prompt.encode()) < 1024 * 1024 < len(json.dumps(prompt))
    installed = _start(z.db, z.run_id, 2, z.pins, _fresh_bootstrap(z, prompt), monkeypatch)
    raw = installed["files"]["context.json"]
    assert len(raw) <= 1024 * 1024
    for name, data in installed["files"].items():
        (tmp_path / name).write_bytes(data)
    context, _, _ = runtime_entrypoint.load_bootstrap(tmp_path)
    assert context.system_prompt == prompt and context.budget_remaining_tokens == 600
