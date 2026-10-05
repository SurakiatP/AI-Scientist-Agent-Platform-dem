from __future__ import annotations

from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
from hashlib import sha256
from socket import AF_INET, SOCK_STREAM, socket
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
import http.client
from sqlalchemy import text

from scientist import broker
from scientist.auth import DomainError
from scientist.contracts import ObjectRef, OperationRequest, PackageSpec, PlanSpec, Principal
from scientist.db import create_project, create_session, session as database_session
from scientist.domain import approve_run, revise_plan, submit_run

PEER_ID = uuid4()
PACKAGE_SOURCE = "https://packages.example/fixture.whl"
ALTERNATE_RECIPIENT = "https://alternate.example"


@dataclass
class DeterministicTransport:
    calls: int = 0
    lose_response: bool = False
    usage_tokens: int = 3

    targets: list[broker.DispatchTarget] = field(default_factory=list)
    def __call__(self, request: OperationRequest, target: broker.DispatchTarget) -> tuple[bytes, int]:
        self.calls += 1
        self.targets.append(target)
        if self.lose_response:
            raise TimeoutError("fixture response was lost")
        return b'{"ok":true}', self.usage_tokens


@pytest.fixture
def broker_fixture(db):
    project_id = create_project(db, "broker test")
    session_id = create_session(db, project_id, "session")
    owner = Principal(identity=uuid4(), kind="owner")
    provider_id = uuid4()
    run = submit_run(db, owner, project_id, session_id, "broker-run", "question", [], provider_id, "fixture-model")
    plan = PlanSpec(
        input_snapshot_digest=db.execute(text("SELECT digest FROM input_snapshots WHERE run_id = :run"), {"run": run.run_id}).scalar_one().strip(),
        provider_id=provider_id,
        model="fixture-model",
        stages=["search"],
        allowed_ops=["search", "llm", "peer", "package"],
        data_recipients=["https://research.example", "https://alternate.example", "https://packages.example", f"peer:{PEER_ID}"],
        packages=[PackageSpec(name="fixture", version="1.0", source=PACKAGE_SOURCE, sha256="a" * 64)],
        token_limit=2000,
        elapsed_limit_ms=10000,
    )
    run = revise_plan(db, owner, run.run_id, run.revision, plan)
    approve_run(db, owner, run.run_id, run.revision, run.plan_digest)
    db.execute(text("INSERT INTO delegations (id, project_id, owner_identity, peer_id, actions) VALUES (:id, :project, :owner, :peer, ARRAY['peer'])"),
               {"id": uuid4(), "project": project_id, "owner": owner.identity, "peer": PEER_ID})
    db.execute(
        text("UPDATE runs SET state = 'running', generation = 1, lease_expires_at = now() + interval '1 hour' WHERE id = :run"),
        {"run": run.run_id},
    )
    db.commit()
    transport = DeterministicTransport()
    stored: dict[str, bytes] = {}

    def persist(_db, project_id: UUID, data: bytes, content_type: str) -> ObjectRef:
        digest = sha256(data).hexdigest()
        key = f"test/{project_id}/{digest}"
        stored[key] = data
        return ObjectRef(project_id=project_id, key=key, sha256=digest, size=len(data), content_type=content_type)

    broker.configure(transport=transport, persist_result=persist, capability_key=b"b" * 32,
                     resolver=lambda host, port: ["8.8.8.8"], peer_destinations={str(PEER_ID): "https://peer.example/a2a"},
                     provider_destinations={str(provider_id): "https://research.example"})
    yield db, owner, run.run_id, transport, stored
    broker.configure(transport=None, persist_result=None, capability_key=None)
    broker.configure_result_verifier(None)


def request(run_id: UUID, operation_id: str = "operation-1", reserve_tokens: int = 5) -> OperationRequest:
    return OperationRequest(
        run_id=run_id,
        generation=1,
        operation_id=operation_id,
        kind="search",
        payload={"url": "https://research.example/search?q=fixture"},
        reserve_tokens=reserve_tokens,
    )


def test_committed_operation_is_not_sent_twice(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    first = broker.execute(db, capability, request(run_id))
    second = broker.execute(db, capability, request(run_id))
    assert first == second
    assert first.state == "committed"
    assert transport.calls == 1


def test_unknown_response_keeps_reservation_and_reconcile_does_not_resend(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    transport.lose_response = True
    result = broker.execute(db, capability, request(run_id))
    assert result.state == "unknown"
    assert broker.reconcile(db, run_id, "operation-1") == result
    assert transport.calls == 1
    assert db.execute(text("SELECT reserved_tokens FROM runs WHERE id = :run"), {"run": run_id}).scalar_one() == 5


def test_operation_identity_cannot_be_reused_with_changed_payload(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    broker.execute(db, capability, request(run_id))
    changed = request(run_id).model_copy(update={"payload": {"url": "https://research.example/other"}})
    with pytest.raises(DomainError, match="idempotency_conflict"):
        broker.execute(db, capability, changed)
    assert transport.calls == 1


def test_committed_operation_replays_after_fenced_generation_change(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    old_capability = broker.issue_capability(db, run_id, 1, 300)
    original = request(run_id)
    first = broker.execute(db, old_capability, original)
    before = db.execute(
        text("SELECT usage_tokens, reserved_tokens FROM runs WHERE id = :run"),
        {"run": run_id},
    ).one()

    db.execute(
        text("UPDATE runs SET generation = 2, state = 'running', lease_expires_at = now() + interval '1 hour' WHERE id = :run"),
        {"run": run_id},
    )
    db.commit()
    new_capability = broker.issue_capability(db, run_id, 2, 300)
    replay = original.model_copy(update={"generation": 2})
    cached = broker.execute(db, new_capability, replay)

    assert cached == first
    assert transport.calls == 1
    after = db.execute(
        text("SELECT usage_tokens, reserved_tokens FROM runs WHERE id = :run"),
        {"run": run_id},
    ).one()
    assert (after.usage_tokens, after.reserved_tokens) == (before.usage_tokens, before.reserved_tokens)
    with pytest.raises(DomainError, match="forbidden"):
        broker.execute(db, old_capability, original)
    changed = replay.model_copy(update={"payload": {"url": "https://research.example/changed"}})
    with pytest.raises(DomainError, match="idempotency_conflict"):
        broker.execute(db, new_capability, changed)


def test_last_budget_reservation_is_serialized_in_postgres(broker_fixture):
    db, _, run_id, _, _ = broker_fixture
    with pytest.raises(DomainError, match="budget_exhausted"):
        broker.execute(db, broker.issue_capability(db, run_id, 1, 300), request(run_id, reserve_tokens=2001))
    assert db.execute(text("SELECT reserved_tokens FROM runs WHERE id = :run"), {"run": run_id}).scalar_one() == 0


def test_stale_generation_capability_is_rejected(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    db.execute(text("UPDATE runs SET generation = 2 WHERE id = :run"), {"run": run_id})
    db.commit()
    with pytest.raises(DomainError, match="forbidden"):
        broker.execute(db, capability, request(run_id))
    assert transport.calls == 0


def test_expired_capability_is_rejected(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    expired = broker._sign_capability({"run_id": str(run_id), "generation": 1, "revision": 2,
                                       "plan_digest": db.execute(text("SELECT plan_digest FROM runs WHERE id = :run"), {"run": run_id}).scalar_one().strip(),
                                       "exp": 1})
    with pytest.raises(DomainError, match="forbidden"):
        broker.execute(db, expired, request(run_id))
    assert transport.calls == 0


def test_unapproved_recipient_is_denied_before_dispatch(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    changed = request(run_id).model_copy(update={"payload": {"url": "https://unapproved.example/search"}})
    with pytest.raises(DomainError, match="forbidden"):
        broker.execute(db, capability, changed)
    assert transport.calls == 0


@pytest.mark.parametrize("routing_key", ["url", "source", "endpoint"])
def test_llm_routing_cannot_override_approved_recipient(broker_fixture, routing_key):
    db, _, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    plan = broker._load_plan(db, run_id, 2)
    db.execute(text("INSERT INTO credentials (id, project_id, label, provider, encrypted_value) VALUES (:id, :project, 'fixture', 'fixture', :value)"),
               {"id": plan.provider_id, "project": db.execute(text("SELECT project_id FROM runs WHERE id = :run"), {"run": run_id}).scalar_one(), "value": b"fixture-secret"})
    payload = {"provider_id": str(plan.provider_id), "model": plan.model, "recipient": "https://research.example",
               "credential_id": str(plan.provider_id), "max_output_tokens": 3, routing_key: "https://attacker.example/collect"}
    malicious = OperationRequest(run_id=run_id, generation=1, operation_id=f"llm-{routing_key}", kind="llm", payload=payload, reserve_tokens=5)
    with pytest.raises(DomainError, match="forbidden"):
        broker.execute(db, capability, malicious)
    assert transport.calls == 0


def test_llm_secret_cannot_be_sent_to_other_approved_data_recipient(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    plan = broker._load_plan(db, run_id, 2)
    db.execute(text("INSERT INTO credentials (id, project_id, label, provider, encrypted_value) VALUES (:id, (SELECT project_id FROM runs WHERE id = :run), 'fixture', 'fixture', :value)"),
               {"id": plan.provider_id, "run": run_id, "value": b"fixture-secret"})
    malicious = OperationRequest(run_id=run_id, generation=1, operation_id="llm-alternate", kind="llm",
                                 payload={"provider_id": str(plan.provider_id), "model": plan.model,
                                          "recipient": ALTERNATE_RECIPIENT, "credential_id": str(plan.provider_id),
                                          "prompt": "question", "max_output_tokens": 1}, reserve_tokens=200)
    with pytest.raises(DomainError, match="forbidden"):
        broker.execute(db, capability, malicious)
    assert transport.calls == 0


@pytest.mark.parametrize("kind,payload", [
    ("search", {"url": "https://research.example/search", "credential_id": str(uuid4())}),
    ("package", {"name": "fixture", "version": "1.0", "source": PACKAGE_SOURCE, "sha256": "a" * 64, "credential_id": str(uuid4())}),
    ("peer", {"peer_id": str(PEER_ID), "message": "fixture", "credential_id": str(uuid4())}),
])
def test_non_provider_effects_cannot_select_credentials(broker_fixture, kind, payload, monkeypatch):
    db, _, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    decrypted = []
    monkeypatch.setattr(broker, "read_secret", lambda *args: decrypted.append(args))
    malicious = OperationRequest(run_id=run_id, generation=1, operation_id=f"secret-{kind}", kind=kind, payload=payload, reserve_tokens=5)
    with pytest.raises(DomainError, match="forbidden"):
        broker.execute(db, capability, malicious)
    assert transport.calls == 0
    assert decrypted == []


@pytest.mark.parametrize("routing", [
    {"url": "https://attacker.example/file.whl"},
    {"source": "https://attacker.example/file.whl"},
    {"endpoint": "https://attacker.example/file.whl"},
])
def test_package_routing_cannot_override_approved_source(broker_fixture, routing):
    db, _, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    payload = {"name": "fixture", "version": "1.0", "source": PACKAGE_SOURCE, "sha256": "a" * 64, **routing}
    malicious = OperationRequest(run_id=run_id, generation=1, operation_id=f"package-{next(iter(routing))}", kind="package", payload=payload, reserve_tokens=5)
    with pytest.raises(DomainError, match="forbidden"):
        broker.execute(db, capability, malicious)
    assert transport.calls == 0


def test_peer_request_cannot_override_configured_endpoint_or_body_route(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    payload = {"peer_id": str(PEER_ID), "message": "fixture", "endpoint": "https://attacker.example/a2a",
               "destination": "https://attacker.example/other"}
    malicious = OperationRequest(run_id=run_id, generation=1, operation_id="peer-route", kind="peer", payload=payload, reserve_tokens=5)
    with pytest.raises(DomainError, match="forbidden"):
        broker.execute(db, capability, malicious)
    assert transport.calls == 0


def test_valid_peer_effect_uses_only_configured_destination(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    peer_request = OperationRequest(run_id=run_id, generation=1, operation_id="peer-valid", kind="peer",
                                    payload={"peer_id": str(PEER_ID), "message": "fixture"}, reserve_tokens=5)
    broker.execute(db, capability, peer_request)
    assert transport.targets[-1].url == "https://peer.example/a2a"
    assert transport.targets[-1].credential_id is None


def test_reserved_effect_reconciles_to_owner_resolvable_unknown(broker_fixture):
    db, owner, run_id, _, _ = broker_fixture

    def crash_after_reservation(request, target):
        raise SystemExit("simulated worker crash after durable reservation")

    broker.configure(transport=crash_after_reservation, capability_key=b"b" * 32,
                     resolver=lambda host, port: ["8.8.8.8"], peer_destinations={str(PEER_ID): "https://peer.example/a2a"})
    capability = broker.issue_capability(db, run_id, 1, 300)
    with pytest.raises(SystemExit):
        broker.execute(db, capability, request(run_id))
    with pytest.raises(DomainError, match="revision_conflict"):
        broker.reconcile(db, run_id, "operation-1")
    db.execute(text("UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE id = :run"), {"run": run_id})
    db.commit()
    broker.configure(dispatch_is_inactive=lambda run, operation, generation:
                     run == run_id and operation == "operation-1" and generation == 1)
    reconciled = broker.reconcile(db, run_id, "operation-1")
    assert reconciled.state == "unknown"
    run = db.execute(text("SELECT state, waiting_reason, reserved_tokens FROM runs WHERE id = :run"), {"run": run_id}).one()
    assert (run.state, run.waiting_reason, run.reserved_tokens) == ("waiting_input", "unknown_outcome", 5)
    stopped = broker.resolve_unknown(db, owner, run_id, "operation-1", "stop", None)
    assert stopped.state == "stopping"


def test_llm_prompt_input_is_covered_by_reservation(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    plan = broker._load_plan(db, run_id, 2)
    project_id = db.execute(text("SELECT project_id FROM runs WHERE id = :run"), {"run": run_id}).scalar_one()
    db.execute(text("INSERT INTO credentials (id, project_id, label, provider, encrypted_value) VALUES (:id, :project, 'fixture', 'fixture', :value)"),
               {"id": plan.provider_id, "project": project_id, "value": b"fixture-secret"})
    underreserved = OperationRequest(run_id=run_id, generation=1, operation_id="underreserved-llm", kind="llm",
                                     payload={"provider_id": str(plan.provider_id), "model": plan.model,
                                              "recipient": "https://research.example", "credential_id": str(plan.provider_id),
                                              "prompt": "x" * 1000, "max_output_tokens": 1}, reserve_tokens=1)
    with pytest.raises(DomainError, match="budget_exhausted"):
        broker.execute(db, capability, underreserved)
    assert transport.calls == 0


def test_valid_llm_effect_uses_plan_recipient_and_scoped_credential(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    plan = broker._load_plan(db, run_id, 2)
    project_id = db.execute(text("SELECT project_id FROM runs WHERE id = :run"), {"run": run_id}).scalar_one()
    db.execute(text("INSERT INTO credentials (id, project_id, label, provider, encrypted_value) VALUES (:id, :project, 'fixture', 'fixture', :value)"),
               {"id": plan.provider_id, "project": project_id, "value": b"fixture-secret"})
    valid = OperationRequest(run_id=run_id, generation=1, operation_id="valid-llm", kind="llm",
        payload={"provider_id": str(plan.provider_id), "model": plan.model,
                 "recipient": "https://research.example", "credential_id": str(plan.provider_id),
                 "prompt": "hello", "max_output_tokens": 3}, reserve_tokens=200)
    result = broker.execute(db, capability, valid)
    assert result.state == "committed"
    assert transport.targets[-1].url == "https://research.example"
    assert transport.targets[-1].credential_id == plan.provider_id


def test_llm_tool_history_is_accepted_and_forwarded_without_rewriting(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    plan = broker._load_plan(db, run_id, 2)
    project_id = db.execute(text("SELECT project_id FROM runs WHERE id = :run"), {"run": run_id}).scalar_one()
    db.execute(
        text("INSERT INTO credentials (id, project_id, label, provider, encrypted_value) VALUES (:id, :project, 'fixture', 'fixture', :value)"),
        {"id": plan.provider_id, "project": project_id, "value": b"fixture-secret"},
    )
    messages = [
        {"role": "user", "content": "Compute 1 + 1"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_ab12", "type": "function", "function": {"name": "add", "arguments": '{"left":1, "right":1}'}}
        ]},
        {"role": "tool", "tool_call_id": "call_ab12", "content": "2"},
    ]
    tool = {"type": "function", "function": {"name": "add", "parameters": {"type": "object", "properties": {}}}}
    request_with_tools = OperationRequest(
        run_id=run_id,
        generation=1,
        operation_id="llm-tool-history",
        kind="llm",
        payload={
            "provider_id": str(plan.provider_id),
            "model": plan.model,
            "recipient": "https://research.example",
            "credential_id": str(plan.provider_id),
            "messages": messages,
            "tools": [tool],
            "tool_choice": {"type": "function", "function": {"name": "add"}},
            "max_output_tokens": 3,
        },
        reserve_tokens=1000,
    )

    result = broker.execute(db, capability, request_with_tools)

    assert result.state == "committed"
    assert transport.calls == 1
    assert transport.targets[-1].kind == "llm"
    committed = db.execute(
        text("SELECT result FROM operations WHERE run_id = :run AND operation_id = :operation"),
        {"run": run_id, "operation": "llm-tool-history"},
    ).scalar_one()
    assert committed["request"]["payload"]["messages"] == messages
    assert committed["request"]["payload"]["tools"] == [tool]


def test_llm_oversized_tool_schema_is_rejected_before_dispatch(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    plan = broker._load_plan(db, run_id, 2)
    project_id = db.execute(text("SELECT project_id FROM runs WHERE id = :run"), {"run": run_id}).scalar_one()
    db.execute(
        text("INSERT INTO credentials (id, project_id, label, provider, encrypted_value) VALUES (:id, :project, 'fixture', 'fixture', :value)"),
        {"id": plan.provider_id, "project": project_id, "value": b"fixture-secret"},
    )
    too_large = OperationRequest(
        run_id=run_id,
        generation=1,
        operation_id="llm-oversized-tool",
        kind="llm",
        payload={
            "provider_id": str(plan.provider_id),
            "model": plan.model,
            "recipient": "https://research.example",
            "credential_id": str(plan.provider_id),
            "messages": [{"role": "user", "content": "question"}],
            "tools": [{"type": "function", "function": {"name": "large", "parameters": {"type": "object", "description": "x" * 40_000}}}],
            "max_output_tokens": 3,
        },
        reserve_tokens=100_000,
    )

    with pytest.raises(DomainError, match="forbidden"):
        broker.execute(db, capability, too_large)
    assert transport.calls == 0


def test_llm_tool_schema_and_choice_are_included_in_reservation(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    plan = broker._load_plan(db, run_id, 2)
    project_id = db.execute(text("SELECT project_id FROM runs WHERE id = :run"), {"run": run_id}).scalar_one()
    db.execute(
        text("INSERT INTO credentials (id, project_id, label, provider, encrypted_value) VALUES (:id, :project, 'fixture', 'fixture', :value)"),
        {"id": plan.provider_id, "project": project_id, "value": b"fixture-secret"},
    )
    underreserved = OperationRequest(
        run_id=run_id,
        generation=1,
        operation_id="llm-schema-budget",
        kind="llm",
        payload={
            "provider_id": str(plan.provider_id),
            "model": plan.model,
            "recipient": "https://research.example",
            "credential_id": str(plan.provider_id),
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"type": "function", "function": {"name": "lookup", "description": "x" * 900, "parameters": {"type": "object", "properties": {}}}}],
            "tool_choice": {"type": "function", "function": {"name": "lookup"}},
            "max_output_tokens": 3,
        },
        reserve_tokens=100,
    )

    with pytest.raises(DomainError, match="budget_exhausted"):
        broker.execute(db, capability, underreserved)
    assert transport.calls == 0


@pytest.mark.parametrize("extra", [{"headers": {"Authorization": "Bearer attacker"}}, {"url": "https://attacker.example"}])
def test_llm_rejects_untrusted_wire_and_routing_fields_before_dispatch(broker_fixture, extra):
    db, _, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    plan = broker._load_plan(db, run_id, 2)
    project_id = db.execute(text("SELECT project_id FROM runs WHERE id = :run"), {"run": run_id}).scalar_one()
    db.execute(
        text("INSERT INTO credentials (id, project_id, label, provider, encrypted_value) VALUES (:id, :project, 'fixture', 'fixture', :value)"),
        {"id": plan.provider_id, "project": project_id, "value": b"fixture-secret"},
    )
    payload = {
        "provider_id": str(plan.provider_id),
        "model": plan.model,
        "recipient": "https://research.example",
        "credential_id": str(plan.provider_id),
        "messages": [{"role": "user", "content": "question"}],
        "max_output_tokens": 3,
        **extra,
    }
    invalid = OperationRequest(run_id=run_id, generation=1, operation_id="llm-invalid-wire", kind="llm", payload=payload, reserve_tokens=1000)

    with pytest.raises(DomainError, match="forbidden"):
        broker.execute(db, capability, invalid)
    assert transport.calls == 0


def test_outbound_llm_client_releases_secret_only_to_approved_target(broker_fixture, monkeypatch):
    db, _, run_id, _, stored = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    plan = broker._load_plan(db, run_id, 2)
    project_id = db.execute(text("SELECT project_id FROM runs WHERE id = :run"), {"run": run_id}).scalar_one()
    db.execute(text("INSERT INTO credentials (id, project_id, label, provider, encrypted_value) VALUES (:id, :project, 'fixture', 'fixture', :value)"),
               {"id": plan.provider_id, "project": project_id, "value": b"fixture-secret"})
    captured = {}

    class Response:
        status = 200
        def getheader(self, name):
            return None
        def read(self, limit):
            return b'{"usage_tokens":3}'

    class Connection:
        def __init__(self, host, ip, port, timeout):
            captured["target"] = (host, port, ip)
        def request(self, method, path, body, headers):
            captured.update(method=method, path=path, body=body, headers=headers)
        def getresponse(self):
            return Response()
        def close(self):
            pass

    monkeypatch.setattr(broker, "_PinnedHTTPSConnection", Connection)
    monkeypatch.setattr(broker, "read_secret", lambda secret_db, credential_id: "fixture-secret")
    broker.configure(persist_result=lambda _db, project, data, content_type: ObjectRef(
        project_id=project, key=f"test/{sha256(data).hexdigest()}", sha256=sha256(data).hexdigest(),
        size=len(data), content_type=content_type
    ), capability_key=b"b" * 32, resolver=lambda host, port: ["8.8.8.8"],
        provider_destinations={str(plan.provider_id): "https://research.example"})
    messages = [
        {"role": "user", "content": "Compute 1 + 1"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_ab12", "type": "function", "function": {"name": "add", "arguments": '{"left":1, "right":1}'}}
        ]},
        {"role": "tool", "tool_call_id": "call_ab12", "content": "2"},
    ]
    tools = [{"type": "function", "function": {"name": "add", "parameters": {"type": "object", "properties": {}}}}]
    valid = OperationRequest(run_id=run_id, generation=1, operation_id="outbound-llm", kind="llm",
                             payload={"provider_id": str(plan.provider_id), "model": plan.model,
                                      "recipient": "https://research.example", "credential_id": str(plan.provider_id),
                                      "messages": messages, "tools": tools,
                                      "tool_choice": {"type": "function", "function": {"name": "add"}},
                                      "temperature": 0.2, "max_output_tokens": 3}, reserve_tokens=1000)
    broker.execute(db, capability, valid)
    import json
    outbound = json.loads(captured["body"])
    assert captured["target"] == ("research.example", 443, "8.8.8.8")
    assert captured["headers"]["Authorization"] == "Bearer fixture-secret"
    assert outbound["model"] == plan.model
    assert outbound["max_tokens"] == 3
    assert outbound["messages"] == messages
    assert outbound["tools"] == tools
    assert outbound["tool_choice"] == {"type": "function", "function": {"name": "add"}}
    assert outbound["temperature"] == 0.2
    assert "max_output_tokens" not in outbound
    assert "credential_id" not in outbound
    assert "fixture-secret" not in captured["body"].decode()


def test_verified_result_evidence_cannot_be_replayed_for_another_operation(broker_fixture):
    db, owner, run_id, transport, _ = broker_fixture
    project_id = db.execute(text("SELECT project_id FROM runs WHERE id = :run"), {"run": run_id}).scalar_one()
    ref = ObjectRef(project_id=project_id, key="evidence/one", sha256="b" * 64, size=1, content_type="application/json")
    broker.configure_result_verifier(lambda context, evidence: True)
    capability = broker.issue_capability(db, run_id, 1, 300)
    transport.lose_response = True
    broker.execute(db, capability, request(run_id, "evidence-one"))
    broker.resolve_unknown(db, owner, run_id, "evidence-one", "verified_result", ref)
    transport.lose_response = True
    broker.execute(db, capability, request(run_id, "evidence-two"))
    with pytest.raises(DomainError, match="forbidden"):
        broker.resolve_unknown(db, owner, run_id, "evidence-two", "verified_result", ref)


def test_verifier_rejects_evidence_bound_to_a_different_operation(broker_fixture):
    db, owner, run_id, transport, _ = broker_fixture
    project_id = db.execute(text("SELECT project_id FROM runs WHERE id = :run"), {"run": run_id}).scalar_one()
    ref = ObjectRef(project_id=project_id, key="evidence/wrong-operation", sha256="c" * 64, size=1, content_type="application/json")
    capability = broker.issue_capability(db, run_id, 1, 300)
    transport.lose_response = True
    broker.execute(db, capability, request(run_id))
    broker.configure_result_verifier(lambda context, evidence: context.operation_id == "some-other-operation")
    with pytest.raises(DomainError, match="forbidden"):
        broker.resolve_unknown(db, owner, run_id, "operation-1", "verified_result", ref)


def test_package_bytes_must_match_approved_hash(broker_fixture):
    db, _, run_id, transport, stored = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    package = OperationRequest(run_id=run_id, generation=1, operation_id="package-hash", kind="package",
                               payload={"name": "fixture", "version": "1.0", "source": PACKAGE_SOURCE, "sha256": "a" * 64},
                               reserve_tokens=5)
    result = broker.execute(db, capability, package)
    assert result.state == "unknown"
    assert result.result is None
    assert result.usage_tokens == 3
    assert transport.calls == 1
    assert stored == {}
    run = db.execute(text("SELECT usage_tokens, reserved_tokens FROM runs WHERE id = :run"), {"run": run_id}).one()
    assert (run.usage_tokens, run.reserved_tokens) == (3, 0)


def test_known_usage_survives_result_storage_failure(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    transport.usage_tokens = 9

    def unavailable(storage_db, project, data, content_type):
        storage_db.execute(text("SELECT 1 / 0"))
        raise OSError("fixture object storage outage")

    broker.configure(transport=transport, persist_result=unavailable, capability_key=b"b" * 32,
                     resolver=lambda host, port: ["8.8.8.8"], peer_destinations={str(PEER_ID): "https://peer.example/a2a"})
    result = broker.execute(db, broker.issue_capability(db, run_id, 1, 300), request(run_id))
    assert result.state == "unknown"
    assert result.result is None
    assert result.usage_tokens == 9
    run = db.execute(text("SELECT usage_tokens, reserved_tokens, waiting_reason FROM runs WHERE id = :run"), {"run": run_id}).one()
    assert (run.usage_tokens, run.reserved_tokens, run.waiting_reason) == (9, 0, "unknown_outcome")


def test_verified_external_result_preserves_known_usage(broker_fixture):
    db, owner, run_id, transport, _ = broker_fixture
    transport.usage_tokens = 7

    def unavailable(_db, _project, _data, _content_type):
        raise OSError("fixture storage unavailable")

    broker.configure(
        transport=transport,
        persist_result=unavailable,
        capability_key=b"b" * 32,
        resolver=lambda host, port: ["8.8.8.8"],
        provider_destinations={
            str(broker._load_plan(db, run_id, 1).provider_id): "https://research.example"
        },
    )
    assert broker.execute(db, broker.issue_capability(db, run_id, 1, 300), request(run_id)).state == "unknown"
    project_id = db.execute(text("SELECT project_id FROM runs WHERE id = :run"), {"run": run_id}).scalar_one()
    data = b"verified outside result"
    evidence = ObjectRef(
        project_id=project_id,
        key=f"external/{project_id}/{sha256(data).hexdigest()}",
        sha256=sha256(data).hexdigest(),
        size=len(data),
        content_type="application/octet-stream",
    )
    broker.configure_result_verifier(lambda _context, _ref: True)
    broker.resolve_unknown(db, owner, run_id, "operation-1", "verified_result", evidence)

    operation = db.execute(
        text("SELECT result FROM operations WHERE run_id = :run AND operation_id = :operation"),
        {"run": run_id, "operation": "operation-1"},
    ).scalar_one()
    run = db.execute(
        text("SELECT usage_tokens, reserved_tokens FROM runs WHERE id = :run"),
        {"run": run_id},
    ).one()
    assert operation["usage_known"] is True
    assert operation["usage_tokens"] == 7
    assert (run.usage_tokens, run.reserved_tokens) == (7, 0)


def test_final_ledger_commit_failure_leaves_staged_truth_for_reconcile(broker_fixture, monkeypatch):
    db, _, run_id, _, stored = broker_fixture
    real_commit = db.commit
    commits = 0

    def fail_final_commit():
        nonlocal commits
        commits += 1
        if commits == 3:
            raise OSError("fixture ledger unavailable")
        real_commit()

    monkeypatch.setattr(db, "commit", fail_final_commit)
    with pytest.raises(OSError, match="fixture ledger unavailable"):
        broker.execute(db, broker.issue_capability(db, run_id, 1, 300), request(run_id))
    monkeypatch.setattr(db, "commit", real_commit)
    db.rollback()
    op = db.execute(text("SELECT state, usage_tokens, result FROM operations WHERE run_id = :run AND operation_id = 'operation-1'"),
                    {"run": run_id}).one()
    assert op.state == "unknown"
    assert op.usage_tokens == 3
    assert op.result["ref"]["key"] in stored
    db.execute(text("UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE id = :run"), {"run": run_id})
    db.commit()
    result = broker.reconcile(db, run_id, "operation-1")
    assert result.state == "committed"
    assert result.usage_tokens == 3
    verified = db.execute(
        text("SELECT state, result FROM operations WHERE run_id = :run AND operation_id = :operation"),
        {"run": run_id, "operation": "operation-1"},
    ).one()
    run_usage = db.execute(
        text("SELECT usage_tokens, reserved_tokens FROM runs WHERE id = :run"),
        {"run": run_id},
    ).one()
    assert verified.state == "committed"
    assert verified.result["usage_known"] is True
    assert verified.result["usage_tokens"] == 3
    assert (run_usage.usage_tokens, run_usage.reserved_tokens) == (3, 0)


def test_verified_unknown_result_verifier_receives_operation_binding(broker_fixture):
    db, owner, run_id, transport, stored = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    transport.lose_response = True
    broker.execute(db, capability, request(run_id))
    project_id = db.execute(text("SELECT project_id FROM runs WHERE id = :run"), {"run": run_id}).scalar_one()
    data = b"provider result"
    digest = sha256(data).hexdigest()
    key = f"verified/{project_id}/{digest}"
    stored[key] = data
    evidence = ObjectRef(project_id=project_id, key=key, sha256=digest, size=len(data), content_type="application/json")
    seen = []

    def verify(context, ref):
        seen.append((context.operation_id, context.payload_hash, ref.key))
        return context.operation_id == "operation-1" and ref.key == key

    broker.configure_result_verifier(verify)
    resolved = broker.resolve_unknown(db, owner, run_id, "operation-1", "verified_result", evidence)
    assert resolved.state == "running"
    assert seen[0][0] == "operation-1"
    assert seen[0][2] == key
    assert db.execute(text("SELECT reserved_tokens FROM runs WHERE id = :run"), {"run": run_id}).scalar_one() == 5


def test_provider_overage_is_recorded_truthfully(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    transport.usage_tokens = 8
    result = broker.execute(db, broker.issue_capability(db, run_id, 1, 300), request(run_id, reserve_tokens=5))
    assert result.usage_tokens == 8
    run = db.execute(text("SELECT usage_tokens, reserved_tokens FROM runs WHERE id = :run"), {"run": run_id}).one()
    assert (run.usage_tokens, run.reserved_tokens) == (8, 0)


def test_concurrent_reservations_cannot_overbook_last_budget(broker_fixture):
    db, _, run_id, _, _ = broker_fixture
    entered_dispatch, release_dispatch = Event(), Event()

    def slow_transport(_: OperationRequest, __: broker.DispatchTarget) -> tuple[bytes, int]:
        entered_dispatch.set()
        assert release_dispatch.wait(5)
        return b'{"ok":true}', 1

    broker.configure(transport=slow_transport, persist_result=lambda _db, project, data, content_type: ObjectRef(
        project_id=project, key="fixture", sha256=sha256(data).hexdigest(), size=len(data), content_type=content_type
    ), capability_key=b"b" * 32, resolver=lambda host, port: ["8.8.8.8"])
    capability = broker.issue_capability(db, run_id, 1, 300)

    def first_effect():
        with database_session() as worker_db:
            return broker.execute(worker_db, capability, request(run_id, "first", 1001))

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(first_effect)
            assert entered_dispatch.wait(5)
            with database_session() as second_db:
                with pytest.raises(DomainError, match="budget_exhausted"):
                    broker.execute(second_db, capability, request(run_id, "second", 1000))
            release_dispatch.set()
            assert future.result(timeout=5).state == "committed"
    finally:
        release_dispatch.set()
    run = db.execute(text("SELECT usage_tokens, reserved_tokens FROM runs WHERE id = :run"), {"run": run_id}).one()
    assert (run.usage_tokens, run.reserved_tokens) == (1, 0)


def test_staged_success_duplicate_does_not_pause_run(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    staged = Event()
    finish_finalization = Event()

    def first_effect():
        with database_session() as worker_db:
            commit = worker_db.commit
            commits = 0

            def commit_after_staging():
                nonlocal commits
                commits += 1
                commit()
                if commits == 2:
                    staged.set()
                    assert finish_finalization.wait(5)

            worker_db.commit = commit_after_staging
            return broker.execute(
                worker_db,
                broker.issue_capability(worker_db, run_id, 1, 300),
                request(run_id),
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first_effect)
        try:
            assert staged.wait(5)
            duplicate = broker.execute(
                db, broker.issue_capability(db, run_id, 1, 300), request(run_id)
            )
            finish_finalization.set()
            original = future.result(timeout=5)
        finally:
            finish_finalization.set()

    run = db.execute(
        text("SELECT state, waiting_reason FROM runs WHERE id = :run"), {"run": run_id}
    ).one()
    assert duplicate.state == "committed"
    assert original.state == "committed"
    assert (run.state, run.waiting_reason) == ("running", None)


def test_reconcile_finalizes_staged_success_without_pausing_run(broker_fixture):
    db, _, run_id, _, _ = broker_fixture
    staged = Event()
    finish_finalization = Event()

    def first_effect():
        with database_session() as worker_db:
            commit = worker_db.commit
            commits = 0

            def commit_after_staging():
                nonlocal commits
                commits += 1
                commit()
                if commits == 2:
                    staged.set()
                    assert finish_finalization.wait(5)

            worker_db.commit = commit_after_staging
            return broker.execute(
                worker_db,
                broker.issue_capability(worker_db, run_id, 1, 300),
                request(run_id),
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first_effect)
        try:
            assert staged.wait(5)
            reconciled = broker.reconcile(db, run_id, "operation-1")
            finish_finalization.set()
            original = future.result(timeout=5)
        finally:
            finish_finalization.set()

    run = db.execute(
        text("SELECT state, waiting_reason, usage_tokens, reserved_tokens FROM runs WHERE id = :run"),
        {"run": run_id},
    ).one()
    assert reconciled.state == "committed"
    assert original.state == "committed"
    assert (run.state, run.waiting_reason, run.usage_tokens, run.reserved_tokens) == ("running", None, 3, 0)


def test_reconcile_cannot_modify_operation_while_result_persistence_is_live(broker_fixture):
    db, _, run_id, _, _ = broker_fixture
    entered_persistence = Event()
    release_persistence = Event()
    plan = broker._load_plan(db, run_id, 1)

    def persist(_storage_db, project_id, data, _content_type):
        entered_persistence.set()
        assert release_persistence.wait(5)
        digest = sha256(data).hexdigest()
        return ObjectRef(
            project_id=project_id,
            key=f"test/{project_id}/{digest}",
            sha256=digest,
            size=len(data),
            content_type="application/octet-stream",
        )

    broker.configure(
        transport=DeterministicTransport(),
        persist_result=persist,
        capability_key=b"b" * 32,
        resolver=lambda host, port: ["8.8.8.8"],
        provider_destinations={str(plan.provider_id): "https://research.example"},
        dispatch_is_inactive=lambda _run, _operation, _generation: True,
    )

    def effect():
        with database_session() as worker_db:
            return broker.execute(
                worker_db,
                broker.issue_capability(worker_db, run_id, 1, 300),
                request(run_id),
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(effect)
        try:
            assert entered_persistence.wait(5)
            db.execute(
                text("UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE id = :run"),
                {"run": run_id},
            )
            db.commit()
            with pytest.raises(DomainError, match="revision_conflict"):
                broker.reconcile(db, run_id, "operation-1")
            active_operation = db.execute(
                text("SELECT state FROM operations WHERE run_id = :run AND operation_id = 'operation-1'"),
                {"run": run_id},
            ).scalar_one()
            active_run = db.execute(
                text("SELECT state, waiting_reason, usage_tokens, reserved_tokens FROM runs WHERE id = :run"),
                {"run": run_id},
            ).one()
            assert active_operation == "reserved"
            assert (active_run.state, active_run.waiting_reason, active_run.usage_tokens,
                    active_run.reserved_tokens) == ("running", None, 0, 5)
        finally:
            db.rollback()
            release_persistence.set()
        result = future.result(timeout=5)

    run = db.execute(
        text("SELECT state, waiting_reason, usage_tokens, reserved_tokens FROM runs WHERE id = :run"),
        {"run": run_id},
    ).one()
    operation = db.execute(
        text("SELECT state, usage_tokens, result FROM operations WHERE run_id = :run AND operation_id = 'operation-1'"),
        {"run": run_id},
    ).one()
    assert result.state == "committed"
    assert operation.state == "committed"
    assert operation.usage_tokens == 3
    assert operation.result["ref"]["sha256"] == sha256(b'{"ok":true}').hexdigest()
    assert (run.state, run.waiting_reason, run.usage_tokens, run.reserved_tokens) == ("running", None, 3, 0)


def test_native_http_total_deadline_interrupts_slow_response_headers(broker_fixture, monkeypatch):
    db, _, run_id, _, _ = broker_fixture
    plan = broker._load_plan(db, run_id, 1)
    listener = socket(AF_INET, SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def trickle_headers():
        try:
            conn, _ = listener.accept()
            with conn:
                received = b""
                while b"\r\n\r\n" not in received:
                    received += conn.recv(1024)
                headers = b"HTTP/1.1 200 OK\r\nX-Wait: " + (b"x" * 32) + b"\r\nContent-Length: 2\r\n\r\n{}"
                for byte in headers:
                    try:
                        conn.sendall(bytes((byte,)))
                    except OSError:
                        return
                    sleep(0.02)
        finally:
            listener.close()

    server = Thread(target=trickle_headers, daemon=True)
    server.start()

    class LocalHTTPConnection:
        def __init__(self, _host, _ip, _https_port, timeout):
            from http.client import HTTPConnection

            self.connection = HTTPConnection("127.0.0.1", port, timeout=timeout)

        @property
        def sock(self):
            return self.connection.sock

        def request(self, *args, **kwargs):
            self.connection.request(*args, **kwargs)

        def getresponse(self):
            return self.connection.getresponse()

        def close(self):
            self.connection.close()

    monkeypatch.setattr(broker, "_PinnedHTTPSConnection", LocalHTTPConnection)
    monkeypatch.setattr(broker, "_MAX_HTTP_TOTAL_SECONDS", 0.2, raising=False)
    broker.configure(
        persist_result=lambda _db, project, data, content_type: ObjectRef(
            project_id=project,
            key=f"test/{project}/{sha256(data).hexdigest()}",
            sha256=sha256(data).hexdigest(),
            size=len(data),
            content_type=content_type,
        ),
        capability_key=b"b" * 32,
        resolver=lambda host, port: ["8.8.8.8"],
        provider_destinations={str(plan.provider_id): "https://research.example"},
    )
    timed_request = request(run_id).model_copy(
        update={"payload": {"url": "https://research.example/search?q=fixture", "timeout_seconds": 10}}
    )
    start = monotonic()
    result = broker.execute(db, broker.issue_capability(db, run_id, 1, 300), timed_request)
    elapsed = monotonic() - start
    server.join(timeout=1)

    run = db.execute(
        text("SELECT usage_tokens, reserved_tokens FROM runs WHERE id = :run"),
        {"run": run_id},
    ).one()
    assert result.state == "unknown"
    assert elapsed < 0.6
    assert (run.usage_tokens, run.reserved_tokens) == (0, 5)


@pytest.mark.parametrize(
    ("status_line", "connection_header"),
    [(b"HTTP/1.0 200 OK\r\n", b""), (b"HTTP/1.1 200 OK\r\n", b"Connection: close\r\n")],
)
def test_native_http_total_deadline_interrupts_body_after_connection_close(
    broker_fixture, monkeypatch, status_line, connection_header
):
    db, _, run_id, _, _ = broker_fixture
    plan = broker._load_plan(db, run_id, 1)
    listener = socket(AF_INET, SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def trickle_body():
        try:
            conn, _ = listener.accept()
            with conn:
                received = b""
                while b"\r\n\r\n" not in received:
                    received += conn.recv(1024)
                conn.sendall(status_line + connection_header + b"Content-Length: 32\r\n\r\n")
                for _ in range(32):
                    try:
                        conn.sendall(b"x")
                    except OSError:
                        return
                    sleep(0.02)
        finally:
            listener.close()

    server = Thread(target=trickle_body, daemon=True)
    server.start()

    class LocalHTTPConnection(http.client.HTTPConnection):
        def __init__(self, _host, _ip, _https_port, timeout):
            super().__init__("127.0.0.1", port, timeout=timeout)
            self._deadline_sock = None

        def connect(self):
            super().connect()
            self._deadline_sock = self.sock

    monkeypatch.setattr(broker, "_PinnedHTTPSConnection", LocalHTTPConnection)
    monkeypatch.setattr(broker, "_MAX_HTTP_TOTAL_SECONDS", 0.2)
    broker.configure(
        persist_result=lambda _db, project, data, content_type: ObjectRef(
            project_id=project,
            key=f"test/{project}/{sha256(data).hexdigest()}",
            sha256=sha256(data).hexdigest(),
            size=len(data),
            content_type=content_type,
        ),
        capability_key=b"b" * 32,
        resolver=lambda host, port: ["8.8.8.8"],
        provider_destinations={str(plan.provider_id): "https://research.example"},
    )
    timed_request = request(run_id).model_copy(
        update={"payload": {"url": "https://research.example/search?q=fixture", "timeout_seconds": 10}}
    )
    started = monotonic()
    result = broker.execute(db, broker.issue_capability(db, run_id, 1, 300), timed_request)
    elapsed = monotonic() - started
    server.join(timeout=1)

    run = db.execute(
        text("SELECT usage_tokens, reserved_tokens FROM runs WHERE id = :run"),
        {"run": run_id},
    ).one()
    assert result.state == "unknown"
    assert elapsed < 0.6
    assert (run.usage_tokens, run.reserved_tokens) == (0, 5)


def test_expired_lease_does_not_prove_dispatch_inactive(broker_fixture):
    db, owner, run_id, _, _ = broker_fixture
    entered_dispatch = Event()
    release_dispatch = Event()
    inactive = False

    def slow_transport(_: OperationRequest, __: broker.DispatchTarget):
        entered_dispatch.set()
        assert release_dispatch.wait(5)
        raise TimeoutError("fixture response lost")

    broker.configure(
        transport=slow_transport,
        capability_key=b"b" * 32,
        resolver=lambda host, port: ["8.8.8.8"],
        dispatch_is_inactive=lambda run, operation, generation: inactive,
    )

    def effect():
        with database_session() as worker_db:
            return broker.execute(
                worker_db,
                broker.issue_capability(worker_db, run_id, 1, 300),
                request(run_id),
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(effect)
        try:
            assert entered_dispatch.wait(5)
            db.execute(
                text("UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE id = :run"),
                {"run": run_id},
            )
            db.execute(
                text("UPDATE operations SET state = 'unknown' WHERE run_id = :run AND operation_id = 'operation-1'"),
                {"run": run_id},
            )
            db.execute(
                text("UPDATE runs SET state = 'waiting_input', waiting_reason = 'unknown_outcome' WHERE id = :run"),
                {"run": run_id},
            )
            db.commit()
            with pytest.raises(DomainError, match="revision_conflict"):
                broker.resolve_unknown(db, owner, run_id, "operation-1", "retry", None)
            db.rollback()
            with pytest.raises(DomainError, match="revision_conflict"):
                broker.reconcile(db, run_id, "operation-1")
            db.rollback()
            release_dispatch.set()
            inactive = True
            assert future.result(timeout=5).state == "unknown"
            assert broker.reconcile(db, run_id, "operation-1").state == "unknown"
        finally:
            release_dispatch.set()


def test_persist_result_uses_db_session_and_authoritative_object_mime(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    seen_sessions = []
    plan = broker._load_plan(db, run_id, 1)

    def persist(session, project_id, data, _content_type):
        seen_sessions.append(session)
        digest = sha256(data).hexdigest()
        return ObjectRef(
            project_id=project_id,
            key=f"test/{project_id}/{digest}",
            sha256=digest,
            size=len(data),
            content_type="application/octet-stream",
        )

    broker.configure(
        transport=transport,
        persist_result=persist,
        capability_key=b"b" * 32,
        resolver=lambda host, port: ["8.8.8.8"],
        peer_destinations={str(PEER_ID): "https://peer.example/a2a"},
        provider_destinations={str(plan.provider_id): "https://research.example"},
    )
    result = broker.execute(db, broker.issue_capability(db, run_id, 1, 300), request(run_id))
    assert result.state == "committed"
    assert seen_sessions == [db]


def test_private_ip_is_rejected_even_when_named_as_recipient(broker_fixture):
    with pytest.raises(DomainError, match="forbidden"):
        broker._validate_url("https://127.0.0.1/search", ["https://127.0.0.1"])
    with pytest.raises(DomainError, match="forbidden"):
        broker._validate_url("https://metadata.google.internal/latest", ["https://metadata.google.internal"])
    with pytest.raises(DomainError, match="forbidden"):
        broker._validate_url("https://[::1]/a2a", ["https://[::1]"], allow_lan=True)


def test_dns_rebinding_to_private_address_is_rejected(broker_fixture):
    answers = iter([["8.8.8.8"], ["10.0.0.8"]])
    broker.configure(capability_key=b"b" * 32, resolver=lambda host, port: next(answers))
    assert broker._validate_url("https://research.example/search", ["https://research.example"])[3] == "8.8.8.8"
    with pytest.raises(DomainError, match="forbidden"):
        broker._validate_url("https://research.example/search", ["https://research.example"])


def test_provider_redirect_is_not_followed(broker_fixture, monkeypatch):
    class Redirect:
        status = 302
        def getheader(self, name):
            return "https://127.0.0.1/private" if name == "Location" else None

    class Connection:
        def __init__(self, *args):
            pass
        def request(self, *args, **kwargs):
            pass
        def getresponse(self):
            return Redirect()
        def close(self):
            pass

    monkeypatch.setattr(broker, "_PinnedHTTPSConnection", Connection)
    broker.configure(capability_key=b"b" * 32, resolver=lambda host, port: ["8.8.8.8"])
    with pytest.raises(DomainError, match="provider_unavailable"):
        broker._dispatch(request(broker_fixture[2]), broker.DispatchTarget("search", "https://research.example/search", ("https://research.example",)))


def test_unapproved_model_is_denied_before_dispatch(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    plan = broker._load_plan(db, run_id, 2)
    bad = OperationRequest(
        run_id=run_id,
        generation=1,
        operation_id="wrong-model",
        kind="llm",
        payload={"provider_id": str(plan.provider_id), "model": "unapproved", "recipient": "https://research.example",
                 "max_output_tokens": 3},
        reserve_tokens=5,
    )
    with pytest.raises(DomainError, match="forbidden"):
        broker.execute(db, capability, bad)
    assert transport.calls == 0


def test_unknown_retry_keeps_original_reservation_and_links_new_identity(broker_fixture):
    db, owner, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    transport.lose_response = True
    original = broker.execute(db, capability, request(run_id))
    transport.lose_response = False
    resolved = broker.resolve_unknown(db, owner, run_id, "operation-1", "retry", None, queued_only=False)
    rows = db.execute(text("SELECT operation_id, state, reserve_tokens, result FROM operations WHERE run_id = :run ORDER BY created_at, operation_id"),
                      {"run": run_id}).all()
    assert resolved.state == "running"
    assert len(rows) == 2
    retry = next(row for row in rows if row.operation_id != original.operation_id)
    assert retry.state == "committed"
    assert retry.reserve_tokens == 5
    assert retry.result["retry_of"] == "operation-1"
    run = db.execute(text("SELECT usage_tokens, reserved_tokens FROM runs WHERE id = :run"), {"run": run_id}).one()
    assert (run.usage_tokens, run.reserved_tokens) == (3, 5)


def test_expired_unknown_retry_is_linked_for_the_next_worker_generation(broker_fixture):
    db, owner, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    transport.lose_response = True
    broker.execute(db, capability, request(run_id))
    db.execute(text("UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE id = :run"), {"run": run_id})
    db.commit()
    broker.reconcile(db, run_id, "operation-1")
    queued = broker.resolve_unknown(db, owner, run_id, "operation-1", "retry", None)
    assert queued.state == "queued"
    old = db.execute(text("SELECT result, reserve_tokens FROM operations WHERE run_id = :run AND operation_id = 'operation-1'"),
                     {"run": run_id}).one()
    retry = OperationRequest.model_validate(old.result["retry_request"])
    assert retry.operation_id == old.result["retry_identity"]
    assert retry.generation == 2
    assert old.reserve_tokens == 5
    db.execute(text("UPDATE runs SET generation = 2, state = 'running', lease_expires_at = now() + interval '1 hour' WHERE id = :run"),
               {"run": run_id})
    db.commit()
    transport.lose_response = False
    result = broker.execute(db, broker.issue_capability(db, run_id, 2, 300), retry)
    assert result.state == "committed"
    assert transport.calls == 2


def test_arbitrary_object_reference_cannot_resolve_unknown_outcome(broker_fixture):
    db, owner, run_id, transport, _ = broker_fixture
    capability = broker.issue_capability(db, run_id, 1, 300)
    transport.lose_response = True
    broker.execute(db, capability, request(run_id))
    project_id = db.execute(text("SELECT project_id FROM runs WHERE id = :run"), {"run": run_id}).scalar_one()
    arbitrary = ObjectRef(project_id=project_id, key="asserted", sha256="a" * 64, size=1, content_type="application/json")
    with pytest.raises(DomainError, match="forbidden"):
        broker.resolve_unknown(db, owner, run_id, "operation-1", "verified_result", arbitrary)


def _decisions(db, run_id) -> int:
    return db.execute(text("SELECT count(*) FROM events WHERE run_id=:r AND kind='decision.required'"),
                      {"r": run_id}).scalar_one()


def _lost_reserved_op(broker_fixture):
    """An operation left in 'reserved' with the run reset to running, quiescence proven."""
    db, _, run_id, transport, _ = broker_fixture
    transport.lose_response = True
    broker.execute(db, broker.issue_capability(db, run_id, 1, 300), request(run_id))
    db.execute(text("UPDATE operations SET state='reserved' WHERE run_id=:r"), {"r": run_id})
    db.execute(text("UPDATE runs SET state='running', waiting_reason=NULL WHERE id=:r"), {"r": run_id})
    db.commit()
    return db, run_id


@pytest.mark.parametrize("terminal", ["canceled", "completed"])
def test_reconcile_keeps_terminal_run_terminal(broker_fixture, terminal):
    db, run_id = _lost_reserved_op(broker_fixture)
    db.execute(text("UPDATE runs SET state=:s WHERE id=:r"), {"s": terminal, "r": run_id})
    db.commit()
    before = _decisions(db, run_id)
    result = broker.reconcile(db, run_id, "operation-1")
    assert result.state == "unknown"
    assert db.execute(text("SELECT state FROM runs WHERE id=:r"), {"r": run_id}).scalar_one() == terminal
    assert _decisions(db, run_id) == before


def test_reconcile_does_not_preempt_stopping(broker_fixture):
    db, run_id = _lost_reserved_op(broker_fixture)
    db.execute(text("UPDATE runs SET state='stopping', cancel_requested=true WHERE id=:r"), {"r": run_id})
    db.commit()
    before = _decisions(db, run_id)
    assert broker.reconcile(db, run_id, "operation-1").state == "unknown"
    assert db.execute(text("SELECT state FROM runs WHERE id=:r"), {"r": run_id}).scalar_one() == "stopping"
    assert _decisions(db, run_id) == before


@pytest.mark.parametrize("target", ["canceled", "stopping"])
def test_record_unknown_respects_stop(broker_fixture, target):
    db, _, run_id, transport, _ = broker_fixture

    def cancel_then_fail(req, tgt):
        with database_session() as other:
            other.execute(text("UPDATE runs SET state=:s, cancel_requested=true WHERE id=:r"),
                          {"s": target, "r": run_id})
            other.commit()
        raise TimeoutError("lost")

    broker.configure(transport=cancel_then_fail, persist_result=broker._persist_result, capability_key=b"b" * 32,
                     resolver=lambda host, port: ["8.8.8.8"],
                     provider_destinations=dict(broker._provider_destinations))
    result = broker.execute(db, broker.issue_capability(db, run_id, 1, 300), request(run_id))
    assert result.state == "unknown"
    run = db.execute(text("SELECT state, reserved_tokens FROM runs WHERE id=:r"), {"r": run_id}).one()
    assert (run.state, run.reserved_tokens) == (target, 5)
    assert _decisions(db, run_id) == 0


def test_stop_queued_with_reserved_op_fences(broker_fixture, monkeypatch):
    from scientist import supervisor
    db, run_id = _lost_reserved_op(broker_fixture)
    db.execute(text("UPDATE runs SET state='queued', lease_expires_at=NULL WHERE id=:r"), {"r": run_id})
    db.commit()
    calls = []
    monkeypatch.setattr(supervisor, "_fence_generation", lambda *a: calls.append(a) or True)
    view = supervisor.stop(db, run_id, 1)
    assert len(calls) == 1
    assert view.state == "canceled"
    assert db.execute(text("SELECT state FROM operations WHERE run_id=:r"), {"r": run_id}).scalar_one() == "unknown"


def test_stop_queued_with_reserved_op_unproven_fence_waits(broker_fixture, monkeypatch):
    from scientist import supervisor
    db, run_id = _lost_reserved_op(broker_fixture)
    db.execute(text("UPDATE runs SET state='queued', lease_expires_at=NULL WHERE id=:r"), {"r": run_id})
    db.commit()
    monkeypatch.setattr(supervisor, "_fence_generation", lambda *a: False)
    supervisor.stop(db, run_id, 1)
    run = db.execute(text("SELECT state, waiting_reason FROM runs WHERE id=:r"), {"r": run_id}).one()
    assert (run.state, run.waiting_reason) == ("waiting_input", "executor_quiescence_unproven")


def _search_connection(monkeypatch, body=b'[{"title":"x"}]', constructed=None):
    captured = {}

    class Response:
        status = 200
        def getheader(self, name):
            return None
        def read(self, limit):
            return body

    class Connection:
        def __init__(self, host, ip, port, timeout):
            if constructed is not None:
                constructed.append(1)
            captured["target"] = (host, port, ip)
        def request(self, method, path, body=None, headers=None):
            captured.update(method=method, path=path, body=body, headers=headers)
        def getresponse(self):
            return Response()
        def close(self):
            pass

    monkeypatch.setattr(broker, "_PinnedHTTPSConnection", Connection)
    return captured


def test_search_uses_get_without_body_and_preserves_query(broker_fixture, monkeypatch):
    db, _, run_id, _, stored = broker_fixture
    monkeypatch.setattr(broker, "_transport", None)
    captured = _search_connection(monkeypatch, body=b'[{"title":"x"}]')
    url = "https://research.example/works?query=a%20b&rows=2&filter=from-pub-date:2020"
    req = request(run_id, "get-search").model_copy(update={"payload": {"url": url}})
    result = broker.execute(db, broker.issue_capability(db, run_id, 1, 300), req)
    assert result.state == "committed"
    assert captured["method"] == "GET"
    assert captured["body"] is None
    assert captured["path"] == "/works?query=a%20b&rows=2&filter=from-pub-date:2020"
    assert list(stored.values()) == [b'[{"title":"x"}]']
    assert db.execute(text("SELECT usage_tokens FROM runs WHERE id = :run"), {"run": run_id}).scalar_one() == 0


def test_search_non_json_response_is_stored(broker_fixture, monkeypatch):
    db, _, run_id, _, stored = broker_fixture
    monkeypatch.setattr(broker, "_transport", None)
    _search_connection(monkeypatch, body=b"<xml>not json</xml>")
    broker.execute(db, broker.issue_capability(db, run_id, 1, 300), request(run_id, "xml-search"))
    assert list(stored.values()) == [b"<xml>not json</xml>"]


def test_search_oversized_response_is_refused(broker_fixture, monkeypatch):
    db, _, run_id, _, stored = broker_fixture
    monkeypatch.setattr(broker, "_transport", None)
    _search_connection(monkeypatch, body=b"x" * (2 * 1024 * 1024 + 1))
    result = broker.execute(db, broker.issue_capability(db, run_id, 1, 300), request(run_id, "big-search"))
    assert result.state != "committed"
    assert not stored


@pytest.mark.parametrize("url,resolver_calls", [
    ("https://unconfigured.example/works", 0),
    ("https://research.example:8443/works", 0),
    ("http://research.example/works", 0),
    ("https://user:pw@research.example/works", 0),
    ("https://169.254.169.254/latest", 0),
    ("https://research.example/works", 1),  # resolves to a private address
])
def test_search_refused_before_dns_or_connection(broker_fixture, monkeypatch, url, resolver_calls):
    db, _, run_id, _, stored = broker_fixture
    monkeypatch.setattr(broker, "_transport", None)
    constructed, calls = [], []
    _search_connection(monkeypatch, constructed=constructed)
    private = resolver_calls == 1
    monkeypatch.setattr(broker, "_resolver", lambda host, port: calls.append(host) or ["10.0.0.5" if private else "8.8.8.8"])
    req = request(run_id, "refused").model_copy(update={"payload": {"url": url}})
    with pytest.raises(DomainError) as err:
        broker.execute(db, broker.issue_capability(db, run_id, 1, 300), req)
    assert err.value.args[0] == "forbidden"
    assert len(calls) == resolver_calls
    assert constructed == []
    assert db.execute(text("SELECT count(*) FROM operations WHERE run_id = :run"), {"run": run_id}).scalar_one() == 0
    assert db.execute(text("SELECT reserved_tokens FROM runs WHERE id = :run"), {"run": run_id}).scalar_one() == 0


def test_search_rejects_query_payload_field(broker_fixture):
    db, _, run_id, transport, _ = broker_fixture
    req = request(run_id, "q-field").model_copy(update={"payload": {"url": "https://research.example/works", "query": "x"}})
    with pytest.raises(DomainError) as err:
        broker.execute(db, broker.issue_capability(db, run_id, 1, 300), req)
    assert err.value.args[0] == "forbidden"
    assert transport.calls == 0


@pytest.mark.parametrize("suffix", ["?query=a b", "?query=é", "?query=a\tb", "?query=a\r\nb", "?query=a\x00b", "?q=\x7f"])
def test_search_url_with_control_space_or_non_ascii_is_refused_before_reservation(broker_fixture, monkeypatch, suffix):
    db, _, run_id, _, _ = broker_fixture
    monkeypatch.setattr(broker, "_transport", None)
    constructed = []
    _search_connection(monkeypatch, constructed=constructed)
    req = request(run_id, "bad-url").model_copy(update={"payload": {"url": "https://research.example/works" + suffix}})
    with pytest.raises(DomainError) as err:
        broker.execute(db, broker.issue_capability(db, run_id, 1, 300), req)
    assert (err.value.code, err.value.status) == ("forbidden", 403)
    assert constructed == []
    assert db.execute(text("SELECT state FROM runs WHERE id = :run"), {"run": run_id}).scalar_one() == "running"
    assert db.execute(text("SELECT count(*) FROM operations WHERE run_id = :run"), {"run": run_id}).scalar_one() == 0
    assert db.execute(text("SELECT reserved_tokens FROM runs WHERE id = :run"), {"run": run_id}).scalar_one() == 0


def test_search_truncated_body_is_not_committed(broker_fixture, monkeypatch):
    db, _, run_id, _, stored = broker_fixture
    monkeypatch.setattr(broker, "_transport", None)

    class Response:
        status = 200
        length = 10  # bytes the server promised but never delivered
        def getheader(self, name):
            return None
        def read(self, limit):
            return b"partial"

    class Connection:
        def __init__(self, *args):
            pass
        def request(self, *args, **kwargs):
            pass
        def getresponse(self):
            return Response()
        def close(self):
            pass

    monkeypatch.setattr(broker, "_PinnedHTTPSConnection", Connection)
    result = broker.execute(db, broker.issue_capability(db, run_id, 1, 300), request(run_id, "truncated"))
    assert result.state != "committed"
    assert not stored
