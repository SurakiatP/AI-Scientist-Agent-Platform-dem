from __future__ import annotations

import json
import subprocess
import sys
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import text

from scientist.auth import DomainError
from scientist.contracts import PeerDataRef, PeerReleaseSpec, PlanSpec, Principal, canonical_peer_parameters_bytes
from scientist.db import create_project, create_session
from scientist.domain import _plan_digest, approve_run, get_plan, peer_data_version_digest, revise_plan, save_finding, submit_run
from scientist.runtime_contracts import RUNTIME_COMMIT, RuntimeContextV1
from scientist import supervisor
from test_broker import broker_fixture
from test_continuation_bootstrap import crashed


OWNER = Principal(identity=uuid4(), kind="owner")
PROVIDER = uuid4()


def _release(snapshot_digest: str, **changes) -> PeerReleaseSpec:
    message_id = changes.get("message_id", "peer-message-1")
    approved_parameters = changes.pop("approved_parameters", {
        "message": {
            "messageId": message_id,
            "role": "ROLE_USER",
            "parts": [{"text": "Please review this selected claim."}],
        },
    })
    values = {
        "release_id": uuid4(),
        "peer_id": uuid4(),
        "endpoint_fingerprint": "a" * 64,
        "purpose": "Have the configured peer check a selected citation claim.",
        "input_snapshot_digest": snapshot_digest,
        "data_refs": [],
        "approved_parameters": approved_parameters,
        "parameters_sha256": sha256(canonical_peer_parameters_bytes(approved_parameters)).hexdigest(),
        "message_id": message_id,
        "method": "SendMessage",
        "allow_get_task": True,
        "request_bytes_limit": 4096,
        "timeout_ms": 5000,
        "reserved_tokens": 100,
        "reconciliation_limit": 3,
    }
    values.update(changes)
    return PeerReleaseSpec(**values)


def _plan(snapshot_digest: str, releases: list[PeerReleaseSpec]) -> PlanSpec:
    return PlanSpec(
        input_snapshot_digest=snapshot_digest,
        provider_id=PROVIDER,
        model="fixture-model",
        stages=["peer review"],
        allowed_ops=["peer"],
        data_recipients=[],
        packages=[],
        token_limit=1000,
        elapsed_limit_ms=30000,
        peer_releases=releases,
    )


def _run(db, project_session):
    project_id, session_id = project_session
    run = submit_run(db, OWNER, project_id, session_id, uuid4().hex, "Check this claim", [], PROVIDER, "fixture-model")
    snapshot_digest = db.execute(text("SELECT digest FROM input_snapshots WHERE run_id=:run"), {"run": run.run_id}).scalar_one().strip()
    return run, snapshot_digest


def test_plan_without_peer_releases_remains_backward_compatible():
    plan = PlanSpec(
        input_snapshot_digest="a" * 64,
        provider_id=PROVIDER,
        model="fixture-model",
        stages=["search"],
        allowed_ops=["search"],
        data_recipients=[],
        packages=[],
        token_limit=1000,
        elapsed_limit_ms=30000,
    )
    assert plan.peer_releases == []


def test_approved_sdk_parameters_have_deterministic_canonical_bytes():
    params = {"z": [1, "ไทย"], "a": {"messageId": "message-1"}}
    assert canonical_peer_parameters_bytes(params) == '{"a":{"messageId":"message-1"},"z":[1,"ไทย"]}'.encode()


def test_contract_module_does_not_import_a2a_sdk():
    code = "import sys; import scientist.contracts; assert not any(name == 'a2a' or name.startswith('a2a.') for name in sys.modules)"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_peer_release_rejects_hash_mismatch_and_parameters_over_limit():
    params = {"message": {"messageId": "peer-message-1", "role": "ROLE_USER", "parts": [{"text": "selected claim"}]}}
    with pytest.raises(ValueError, match="sha256"):
        _release("c" * 64, approved_parameters=params, parameters_sha256="f" * 64)
    with pytest.raises(ValueError, match="request_bytes_limit"):
        _release("c" * 64, approved_parameters=params, request_bytes_limit=8)


@pytest.mark.parametrize(
    "params",
    [
        {"unexpected": "unsupported"},
        {"message": {"messageId": "different-id", "role": "ROLE_USER", "parts": [{"text": "claim"}]}},
        {"message": {"messageId": "peer-message-1", "role": "ROLE_AGENT", "parts": [{"text": "claim"}]}},
        {"message": {"messageId": "peer-message-1", "role": "ROLE_USER", "parts": [{"data": "raw"}]}},
    ],
)
def test_revise_plan_rejects_unsupported_sdk_parameters_or_identity_mismatch(db, project_session, params):
    run, snapshot_digest = _run(db, project_session)
    release = _release(snapshot_digest, approved_parameters=params)

    with pytest.raises(DomainError) as error:
        revise_plan(db, OWNER, run.run_id, run.revision, _plan(snapshot_digest, [release]))

    assert error.value.code == "peer_release_parameters_invalid"


def test_plan_view_returns_the_exact_approved_peer_parameters(db, project_session):
    run, snapshot_digest = _run(db, project_session)
    release = _release(snapshot_digest)
    revised = revise_plan(db, OWNER, run.run_id, run.revision, _plan(snapshot_digest, [release]))
    approve_run(db, OWNER, run.run_id, revised.revision, revised.plan_digest)

    view = get_plan(db, OWNER, run.run_id)

    assert view.plan.peer_releases[0].approved_parameters == release.approved_parameters


def test_historical_plan_and_runtime_context_keep_the_legacy_digest():
    provider_id = uuid4()
    legacy_plan = {
        "input_snapshot_digest": "a" * 64,
        "provider_id": str(provider_id),
        "model": "fixture-model",
        "stages": ["search"],
        "allowed_ops": ["llm", "search"],
        "data_recipients": ["https://research.example"],
        "packages": [],
        "token_limit": 1000,
        "elapsed_limit_ms": 30000,
    }
    legacy_digest = sha256(json.dumps(legacy_plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    context = RuntimeContextV1.model_validate({
        "schema_version": 1,
        "run_id": str(uuid4()),
        "project_id": str(uuid4()),
        "generation": 1,
        "revision": 1,
        "input_snapshot_digest": "a" * 64,
        "plan_digest": legacy_digest,
        "runtime_commit": RUNTIME_COMMIT,
        "image_digest": "sha256:" + "b" * 64,
        "skills_digest": "c" * 64,
        "environment_digest": "d" * 64,
        "provider_id": str(provider_id),
        "provider_endpoint": "https://research.example",
        "model": "fixture-model",
        "plan": legacy_plan,
        "turn_id": str(uuid4()),
        "system_prompt": "Fixture system prompt",
        "messages": [{"role": "user", "content": "Find fixture papers"}],
        "todo": {"todos": [], "revision": 0},
        "compacted_context": None,
        "boundary": "before_model",
        "pending_assistant": None,
        "operation_mappings": [],
        "operation_sequence": 0,
        "workspace_manifest": [],
        "budget_remaining_tokens": 1000,
    })

    assert "peer_releases" not in context.plan.model_dump(mode="json")
    assert context.plan_digest == legacy_digest


def test_legacy_checkpoint_recovers_without_rewriting_approved_plan(crashed):
    db, run_id, controller = crashed.db, crashed.run_id, crashed.controller
    before = db.execute(text("""
        SELECT r.plan_digest, p.digest, p.plan
        FROM runs r JOIN plan_revisions p ON p.run_id=r.id AND p.revision=r.revision
        WHERE r.id=:run
    """), {"run": run_id}).one()
    assert "peer_releases" not in before.plan
    assert before.plan_digest.strip() == before.digest.strip()

    boot = supervisor.continuation_bootstrap(db, run_id, 2, controller)

    context = RuntimeContextV1.model_validate_json(boot.context)
    after = db.execute(text("""
        SELECT r.plan_digest, p.digest, p.plan
        FROM runs r JOIN plan_revisions p ON p.run_id=r.id AND p.revision=r.revision
        WHERE r.id=:run
    """), {"run": run_id}).one()
    assert context.plan.model_dump(mode="json") == before.plan
    assert "peer_releases" not in context.plan.model_dump(mode="json")
    assert (after.plan_digest, after.digest, after.plan) == (before.plan_digest, before.digest, before.plan)


@pytest.mark.parametrize(
    "override",
    [
        {"endpoint_fingerprint": "not-a-fingerprint"},
        {"parameters_sha256": "not-a-digest"},
        {"purpose": "   "},
        {"request_bytes_limit": 0},
        {"timeout_ms": 0},
        {"reconciliation_limit": 1000000},
        {"method": "DeleteTask"},
    ],
)
def test_peer_release_contract_rejects_unbounded_or_unapproved_values(override):
    with pytest.raises(ValueError):
        _release("c" * 64, **override)


def test_approved_plan_digest_binds_peer_release_and_message(db, project_session):
    project_id, session_id = project_session
    finding = save_finding(db, OWNER, project_id, session_id, "Captured evidence", None, [])
    run, snapshot_digest = _run(db, project_session)
    captured = db.execute(text("SELECT manifest FROM input_snapshots WHERE run_id=:run"), {"run": run.run_id}).scalar_one()
    release = _release(snapshot_digest, data_refs=[PeerDataRef(kind="finding", record_id=finding.id, version_digest=peer_data_version_digest("finding", captured["findings"][0]))])
    plan = _plan(snapshot_digest, [release])
    revised = revise_plan(db, OWNER, run.run_id, run.revision, plan)

    approved = approve_run(db, OWNER, run.run_id, revised.revision, revised.plan_digest)

    stored = db.execute(text("SELECT plan, digest FROM plan_revisions WHERE run_id=:run AND revision=:revision"), {"run": run.run_id, "revision": revised.revision}).one()
    assert stored.plan["peer_releases"][0]["release_id"] == str(release.release_id)
    assert stored.plan["peer_releases"][0]["message_id"] == release.message_id
    assert stored.digest.strip() == _plan_digest(plan)
    assert stored.digest.strip() != _plan_digest(_plan(snapshot_digest, []))
    assert approved.state == "queued"


def test_peer_release_cannot_bind_a_different_or_stale_input_snapshot(db, project_session):
    run, snapshot_digest = _run(db, project_session)
    stale = _release("d" * 64)

    with pytest.raises(DomainError) as error:
        revise_plan(db, OWNER, run.run_id, run.revision, _plan(snapshot_digest, [stale]))

    assert error.value.code == "revision_conflict"


def test_peer_release_rejects_a_data_reference_outside_the_immutable_snapshot(db, project_session):
    project_id, session_id = project_session
    finding = save_finding(db, OWNER, project_id, session_id, "Captured evidence", None, [])
    run, snapshot_digest = _run(db, project_session)
    release = _release(snapshot_digest, data_refs=[PeerDataRef(kind="finding", record_id=finding.id, version_digest="e" * 64)])

    with pytest.raises(DomainError) as error:
        revise_plan(db, OWNER, run.run_id, run.revision, _plan(snapshot_digest, [release]))

    assert error.value.code == "peer_release_data_unavailable"


def test_peer_message_identity_is_unique_per_peer_in_an_approved_plan(db, project_session):
    run, snapshot_digest = _run(db, project_session)
    first = _release(snapshot_digest)
    duplicate = _release(snapshot_digest, peer_id=first.peer_id, message_id=first.message_id)

    with pytest.raises(DomainError) as error:
        revise_plan(db, OWNER, run.run_id, run.revision, _plan(snapshot_digest, [first, duplicate]))

    assert error.value.code == "peer_release_invalid"


def test_sdk_numeric_metadata_roundtrip_cannot_change_approved_bytes():
    from scientist.domain import _validate_peer_sdk_parameters

    params = {
        "message": {
            "messageId": "peer-message-1",
            "role": "ROLE_USER",
            "parts": [{"text": "selected claim"}],
            "metadata": {"count": 1},
        }
    }
    release = _release("c" * 64, approved_parameters=params)
    with pytest.raises(DomainError) as error:
        _validate_peer_sdk_parameters(release)
    assert error.value.code == "peer_release_parameters_invalid"

    params["message"]["metadata"]["count"] = 1.0
    normalized_release = _release("c" * 64, approved_parameters=params)
    _validate_peer_sdk_parameters(normalized_release)
