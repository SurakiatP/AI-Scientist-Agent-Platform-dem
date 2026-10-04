from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from scientist import broker, limits, supervisor
from scientist.auth import DomainError
from scientist.contracts import ObjectRef, PlanSpec, Principal
from scientist.db import create_project, create_session, engine as base_engine, migrate
from scientist.domain import _event, approve_run, revise_plan, submit_run


class FixtureTransport:
    def __init__(self) -> None:
        self.calls = 0
        self.usage_tokens = 1

    def __call__(self, _request, _target):
        self.calls += 1
        return b'{"fixture":true}', self.usage_tokens


@dataclass
class RunLimitsContext:
    db: Session
    sql_engine: object
    run_id: UUID
    owner: Principal
    external: Principal
    transport: FixtureTransport
    capability: str
    token_limit: int
    elapsed_limit_ms: int


@pytest.fixture
def run_limits_context():
    schema = f"run_limits_{uuid4().hex}"
    parent_engine = base_engine()
    with parent_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated_engine = create_engine(
        parent_engine.url,
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    db = Session(isolated_engine, expire_on_commit=False)
    try:
        migrate(isolated_engine)
        project_id = create_project(db, "run limits actual PostgreSQL")
        session_id = create_session(db, project_id, "limits")
        owner = Principal(identity=uuid4(), kind="owner")
        external = Principal(identity=uuid4(), kind="external")
        provider_id = uuid4()
        token_limit, elapsed_limit_ms = 300, 10_000
        run = submit_run(
            db, owner, project_id, session_id, f"limits-{uuid4().hex}",
            "synthetic budget fixture", [], provider_id, "fixture-model",
        )
        snapshot = db.execute(
            text("SELECT digest FROM input_snapshots WHERE run_id=:run"),
            {"run": run.run_id},
        ).scalar_one().strip()
        plan = PlanSpec(
            input_snapshot_digest=snapshot,
            provider_id=provider_id,
            model="fixture-model",
            stages=["fixture"],
            allowed_ops=["llm"],
            data_recipients=["https://research.example"],
            packages=[],
            token_limit=token_limit,
            elapsed_limit_ms=elapsed_limit_ms,
        )
        run = revise_plan(db, owner, run.run_id, run.revision, plan)
        run = approve_run(db, owner, run.run_id, run.revision, run.plan_digest)
        db.execute(text("""
            UPDATE runs SET state='running', generation=1,
                lease_expires_at=clock_timestamp()+interval '1 hour'
            WHERE id=:run
        """), {"run": run.run_id})
        db.execute(text("""
            INSERT INTO credentials (id, project_id, label, provider, encrypted_value)
            VALUES (:id, :project, 'synthetic fixture', 'fixture', :value)
        """), {"id": provider_id, "project": project_id, "value": b"synthetic-only"})
        db.commit()

        transport = FixtureTransport()
        stored: dict[str, bytes] = {}

        def persist(_db, object_project: UUID, body: bytes, content_type: str) -> ObjectRef:
            digest = sha256(body).hexdigest()
            key = f"fixture/{object_project}/{digest}"
            stored[key] = body
            return ObjectRef(
                project_id=object_project, key=key, sha256=digest,
                size=len(body), content_type=content_type,
            )

        broker.configure(
            transport=transport,
            persist_result=persist,
            capability_key=b"l" * 32,
            resolver=lambda _host, _port: ["8.8.8.8"],
            provider_destinations={str(provider_id): "https://research.example"},
        )
        capability = broker.issue_capability(db, run.run_id, 1, 300)
        yield RunLimitsContext(
            db, isolated_engine, run.run_id, owner, external, transport,
            capability, token_limit, elapsed_limit_ms,
        )
    finally:
        db.rollback()
        db.close()
        broker.configure(transport=None, persist_result=None, capability_key=None)
        broker.configure_result_verifier(None)
        isolated_engine.dispose()
        with parent_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def _request(context: RunLimitsContext, *, reserve_tokens: int = 10,
             operation_id: str = "limit-op", generation: int = 1):
    row = context.db.execute(
        text("SELECT plan FROM plan_revisions WHERE run_id=:run AND revision=2"),
        {"run": context.run_id},
    ).mappings().one()
    provider_id = row["plan"]["provider_id"]
    return broker.OperationRequest(
        run_id=context.run_id,
        generation=generation,
        operation_id=operation_id,
        kind="llm",
        payload={
            "provider_id": provider_id,
            "model": "fixture-model",
            "recipient": "https://research.example",
            "credential_id": provider_id,
            "prompt": "synthetic only",
            "max_output_tokens": 3,
        },
        reserve_tokens=reserve_tokens,
    )


def _fresh_run(context: RunLimitsContext):
    with context.sql_engine.connect() as connection:
        return connection.execute(
            text("""
                SELECT state, waiting_reason, usage_tokens, reserved_tokens,
                       token_limit, elapsed_used_ms, elapsed_active_since,
                       budget_decision_id
                FROM runs WHERE id=:run
            """),
            {"run": context.run_id},
        ).mappings().one()


def _assert_durable_budget_wait(context: RunLimitsContext) -> UUID:
    row = _fresh_run(context)
    assert (row["state"], row["waiting_reason"]) == ("waiting_input", "budget_exhausted")
    assert row["budget_decision_id"] is not None
    with context.sql_engine.connect() as connection:
        events = connection.execute(text("""
            SELECT kind, payload FROM events WHERE run_id=:run
            ORDER BY sequence
        """), {"run": context.run_id}).mappings().all()
    assert any(event["kind"] == "run.state" and event["payload"].get("state") == "waiting_input"
               for event in events)
    assert any(event["kind"] == "decision.required"
               and event["payload"].get("reason") == "budget_exhausted"
               and event["payload"].get("decision_id") == str(row["budget_decision_id"])
               for event in events)
    return row["budget_decision_id"]


def test_elapsed_ceiling_commits_wait_before_refusing_llm(run_limits_context):
    context = run_limits_context
    context.db.execute(text("""
        UPDATE runs SET elapsed_used_ms=elapsed_limit_ms, elapsed_active_since=NULL
        WHERE id=:run
    """), {"run": context.run_id})
    context.db.commit()

    with pytest.raises(DomainError, match="budget_exhausted"):
        broker.execute(context.db, context.capability, _request(context, reserve_tokens=200))

    assert context.transport.calls == 0
    _assert_durable_budget_wait(context)


def test_token_reservation_shortfall_commits_wait_before_refusing_llm(run_limits_context):
    context = run_limits_context
    context.db.execute(text("""
        UPDATE runs SET usage_tokens=token_limit-1, elapsed_used_ms=0,
            elapsed_active_since=clock_timestamp()
        WHERE id=:run
    """), {"run": context.run_id})
    context.db.commit()

    with pytest.raises(DomainError, match="budget_exhausted"):
        broker.execute(context.db, context.capability, _request(context, reserve_tokens=200))

    assert context.transport.calls == 0
    _assert_durable_budget_wait(context)


def test_committed_result_replay_is_not_blocked_or_charged_at_deadline(run_limits_context):
    context = run_limits_context
    request = _request(context, reserve_tokens=200)
    result = broker.execute(context.db, context.capability, request)
    assert result.state == "committed"
    assert context.transport.calls == 1
    context.db.execute(text("""
        UPDATE runs SET elapsed_used_ms=elapsed_limit_ms, elapsed_active_since=NULL
        WHERE id=:run
    """), {"run": context.run_id})
    context.db.commit()
    before = _fresh_run(context)

    replay = broker.execute(context.db, context.capability, request)

    after = _fresh_run(context)
    assert replay.state == "committed"
    assert context.transport.calls == 1
    assert (after["usage_tokens"], after["reserved_tokens"]) == (
        before["usage_tokens"], before["reserved_tokens"],
    )


def test_known_usage_at_token_ceiling_is_committed_before_budget_wait(run_limits_context):
    context = run_limits_context
    context.db.execute(text("""
        UPDATE runs SET token_limit=200, usage_tokens=0, reserved_tokens=0,
            elapsed_active_since=clock_timestamp()
        WHERE id=:run
    """), {"run": context.run_id})
    context.db.commit()
    context.transport.usage_tokens = 200

    result = broker.execute(
        context.db, context.capability, _request(context, reserve_tokens=200),
    )

    assert result.state == "committed"
    assert context.transport.calls == 1
    row = _fresh_run(context)
    assert row["usage_tokens"] == 200
    assert row["state"] == "waiting_input"
    assert row["waiting_reason"] == "budget_exhausted"
    _assert_durable_budget_wait(context)


def test_elapsed_interval_uses_postgres_clock_and_settles_across_sessions(run_limits_context):
    context = run_limits_context
    context.db.execute(text("""
        UPDATE runs SET elapsed_used_ms=25,
            elapsed_active_since=clock_timestamp()-interval '2 seconds'
        WHERE id=:run
    """), {"run": context.run_id})
    context.db.commit()

    with Session(context.sql_engine) as accounting_session:
        observed = limits.effective_elapsed_ms(accounting_session, context.run_id)
        assert observed >= 1_900
        settled = limits.settle_active_interval(accounting_session, context.run_id)
        accounting_session.commit()

    with context.sql_engine.connect() as connection:
        row = connection.execute(text("""
            SELECT elapsed_used_ms, elapsed_active_since FROM runs WHERE id=:run
        """), {"run": context.run_id}).one()
    assert settled >= 1_900
    assert row.elapsed_used_ms >= 1_900
    assert row.elapsed_active_since is None


def test_claim_does_not_requeue_run_at_elapsed_ceiling(run_limits_context):
    context = run_limits_context
    context.db.execute(text("""
        UPDATE runs SET state='queued', elapsed_used_ms=elapsed_limit_ms,
            elapsed_active_since=NULL, lease_expires_at=NULL
        WHERE id=:run
    """), {"run": context.run_id})
    context.db.commit()

    claimed = supervisor.claim(context.db, max_active=3)

    assert claimed is None
    _assert_durable_budget_wait(context)


def test_claim_does_not_requeue_run_at_token_ceiling(run_limits_context):
    context = run_limits_context
    context.db.execute(text("""
        UPDATE runs SET state='queued', usage_tokens=token_limit,
            elapsed_used_ms=0, elapsed_active_since=NULL, lease_expires_at=NULL
        WHERE id=:run
    """), {"run": context.run_id})
    context.db.commit()

    claimed = supervisor.claim(context.db, max_active=3)

    assert claimed is None
    _assert_durable_budget_wait(context)


def test_claim_starts_postgres_active_interval(run_limits_context):
    context = run_limits_context
    context.db.execute(text("""
        UPDATE runs SET state='queued', elapsed_used_ms=0,
            elapsed_active_since=NULL, lease_expires_at=NULL
        WHERE id=:run
    """), {"run": context.run_id})
    context.db.commit()

    assert supervisor.claim(context.db, max_active=3) == (context.run_id, 2)
    row = _fresh_run(context)
    assert row["elapsed_active_since"] is not None


def test_recovery_requeue_barrier_pauses_after_proven_elapsed_exhaustion(run_limits_context):
    context = run_limits_context
    context.db.execute(text("""
        UPDATE runs SET state='running', generation=1,
            elapsed_used_ms=elapsed_limit_ms, elapsed_active_since=NULL
        WHERE id=:run
    """), {"run": context.run_id})
    context.db.commit()

    supervisor._queue_recovered_run(context.db, context.run_id, 1, 2)
    context.db.commit()

    assert _fresh_run(context)["state"] == "waiting_input"
    _assert_durable_budget_wait(context)


def test_extension_interface_is_owner_only_idempotent_and_monotonic(run_limits_context):
    context = run_limits_context
    context.db.execute(text("""
        UPDATE runs SET state='waiting_input', waiting_reason='budget_exhausted',
            elapsed_used_ms=elapsed_limit_ms, elapsed_active_since=NULL,
            budget_decision_id=:decision
        WHERE id=:run
    """), {"run": context.run_id, "decision": uuid4()})
    context.db.commit()
    decision_id = _fresh_run(context)["budget_decision_id"]
    extend = getattr(__import__("scientist.domain", fromlist=["extend_run_budget"]),
                     "extend_run_budget", None)
    assert callable(extend), "owner budget extension domain function is missing"

    with pytest.raises(DomainError, match="forbidden"):
        extend(context.db, context.external, context.run_id, 2, decision_id,
               "extension-1", 600, 20_000)
    first = extend(context.db, context.owner, context.run_id, 2, decision_id,
                   "extension-1", 600, 20_000)
    usage_before = _fresh_run(context)
    again = extend(context.db, context.owner, context.run_id, 2, decision_id,
                   "extension-1", 600, 20_000)
    usage_after = _fresh_run(context)

    assert first.token_limit == again.token_limit == 600
    assert usage_after["usage_tokens"] == usage_before["usage_tokens"]
    assert usage_after["reserved_tokens"] == usage_before["reserved_tokens"]
    assert usage_after["state"] == "waiting_input"
    assert context.db.execute(text("""
        SELECT count(*) FROM run_budget_extensions
        WHERE run_id=:run AND idempotency_key='extension-1'
    """), {"run": context.run_id}).scalar_one() == 1
    with pytest.raises(DomainError, match="idempotency_conflict"):
        extend(context.db, context.owner, context.run_id, 2, decision_id,
               "extension-1", 700, 20_000)
    with pytest.raises(DomainError, match="revision_conflict"):
        extend(context.db, context.owner, context.run_id, 1, decision_id,
               "extension-stale", 700, 30_000)


def test_budget_wait_does_not_replace_unknown_outcome_wait(run_limits_context):
    context = run_limits_context
    context.db.execute(text("""
        UPDATE runs SET state='waiting_input', waiting_reason='unknown_outcome',
            budget_decision_id=NULL, elapsed_used_ms=elapsed_limit_ms,
            elapsed_active_since=NULL
        WHERE id=:run
    """), {"run": context.run_id})
    context.db.commit()

    decision_id = limits.mark_budget_wait(
        context.db, context.run_id, 2, _event,
    )

    assert decision_id is None
    row = _fresh_run(context)
    assert row["state"] == "waiting_input"
    assert row["waiting_reason"] == "unknown_outcome"
    assert row["budget_decision_id"] is None


def test_budget_extension_consumes_old_decision_before_safe_resume(run_limits_context):
    context = run_limits_context
    context.db.execute(text("""
        UPDATE runs SET usage_tokens=token_limit, elapsed_used_ms=0,
            elapsed_active_since=NULL, budget_decision_id=NULL
        WHERE id=:run
    """), {"run": context.run_id})
    context.db.commit()
    first_decision = limits.mark_budget_wait(context.db, context.run_id, 2, _event)
    context.db.commit()
    assert first_decision is not None

    extend = getattr(__import__("scientist.domain", fromlist=["extend_run_budget"]),
                     "extend_run_budget")
    extended = extend(context.db, context.owner, context.run_id, 2, first_decision,
                      "cycle-extension", 600, 20_000)
    assert extended.state == "waiting_input"
    assert extended.token_limit == 600

    # The matching old key remains replayable even after its decision is consumed.
    replay = extend(context.db, context.owner, context.run_id, 2, first_decision,
                    "cycle-extension", 600, 20_000)
    assert replay.token_limit == 600
    with pytest.raises(DomainError, match="revision_conflict"):
        extend(context.db, context.owner, context.run_id, 2, first_decision,
               "stale-decision-new-key", 700, 30_000)

    supervisor._queue_recovered_run(context.db, context.run_id, 1, 2)
    context.db.commit()
    assert supervisor.claim(context.db, max_active=3) == (context.run_id, 2)
    context.db.execute(text("UPDATE runs SET usage_tokens=token_limit WHERE id=:run"),
                       {"run": context.run_id})
    context.db.commit()
    capability = broker.issue_capability(context.db, context.run_id, 2, 300)
    with pytest.raises(DomainError, match="budget_exhausted"):
        broker.execute(context.db, capability, _request(
            context, reserve_tokens=200, operation_id="second-budget-ceiling", generation=2,
        ))

    second_run = _fresh_run(context)
    assert second_run["state"] == "waiting_input"
    assert second_run["budget_decision_id"] != first_decision
    with context.sql_engine.connect() as connection:
        decisions = connection.execute(text("""
            SELECT payload->>'decision_id' FROM events
            WHERE run_id=:run AND kind='decision.required'
            ORDER BY sequence
        """), {"run": context.run_id}).scalars().all()
    assert len(decisions) == 2
    assert decisions[-1] == str(second_run["budget_decision_id"])
    # Replaying the first extension still does not consume usage or replace decision two.
    replay = extend(context.db, context.owner, context.run_id, 2, first_decision,
                    "cycle-extension", 600, 20_000)
    assert replay.token_limit == 600
    assert _fresh_run(context)["budget_decision_id"] == second_run["budget_decision_id"]


def test_partial_budget_extension_requests_a_fresh_decision_while_paused(run_limits_context):
    context = run_limits_context
    context.db.execute(text("""
        UPDATE runs SET usage_tokens=token_limit, elapsed_used_ms=elapsed_limit_ms,
            elapsed_active_since=NULL, budget_decision_id=NULL
        WHERE id=:run
    """), {"run": context.run_id})
    context.db.commit()
    first_decision = limits.mark_budget_wait(context.db, context.run_id, 2, _event)
    context.db.commit()
    assert first_decision is not None

    extend = getattr(__import__("scientist.domain", fromlist=["extend_run_budget"]),
                     "extend_run_budget")
    extended = extend(context.db, context.owner, context.run_id, 2, first_decision,
                      "partial-extension", 600, context.elapsed_limit_ms)

    assert extended.state == "waiting_input"
    second_run = _fresh_run(context)
    assert second_run["budget_decision_id"] not in {None, first_decision}
    with context.sql_engine.connect() as connection:
        assert connection.execute(text("""
            SELECT count(*) FROM events WHERE run_id=:run AND kind='decision.required'
        """), {"run": context.run_id}).scalar_one() == 2
    assert extend(context.db, context.owner, context.run_id, 2, first_decision,
                  "partial-extension", 600, context.elapsed_limit_ms).token_limit == 600
    with pytest.raises(DomainError, match="revision_conflict"):
        extend(context.db, context.owner, context.run_id, 2, first_decision,
               "stale-partial-decision", 700, 30_000)


def test_unknown_outcome_is_not_resumed_by_budget_extension(run_limits_context):
    context = run_limits_context
    operation_id = "unknown-after-budget-extension"
    context.db.execute(text("""
        UPDATE runs SET state='waiting_input', waiting_reason='unknown_outcome',
            usage_tokens=12, reserved_tokens=7, elapsed_active_since=NULL,
            budget_decision_id=NULL
        WHERE id=:run
    """), {"run": context.run_id})
    context.db.execute(text("""
        INSERT INTO operations
          (id, run_id, operation_id, generation, kind, payload_hash, state, reserve_tokens, result)
        VALUES (:id, :run, :operation, 1, 'llm', :hash, 'unknown', 7,
          CAST(:result AS jsonb))
    """), {
        "id": uuid4(), "run": context.run_id, "operation": operation_id,
        "hash": "a" * 64, "result": '{"request":{}}',
    })
    context.db.commit()
    run_before = _fresh_run(context)
    assert run_before["reserved_tokens"] == 7
    assert run_before["budget_decision_id"] is None

    # Unknown-outcome authority is intentionally separate from budget extension;
    # a stale/mismatched budget decision must never clear or resume it.
    extend = getattr(__import__("scientist.domain", fromlist=["extend_run_budget"]),
                     "extend_run_budget", None)
    assert callable(extend), "owner budget extension domain function is missing"
    with pytest.raises(DomainError):
        extend(context.db, context.owner, context.run_id, 2, uuid4(),
               "extension-unknown", 600, 20_000)
    after = _fresh_run(context)
    assert (after["state"], after["waiting_reason"], after["reserved_tokens"]) == (
        "waiting_input", "unknown_outcome", 7,
    )
