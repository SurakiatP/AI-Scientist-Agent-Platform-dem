import json
from uuid import uuid4

import pytest

from scientist import settings
from scientist.auth import DomainError
from scientist.contracts import PlanSpec, Principal
from scientist.domain import get_plan, revise_plan, submit_run
from scientist.research import build_plan

_REAL = settings.provider_destinations  # captured before conftest's autouse stub
A, B = uuid4(), uuid4()


@pytest.fixture(autouse=True)
def _real_settings(monkeypatch):
    monkeypatch.setattr(settings, "provider_destinations", _REAL)
    monkeypatch.setenv("SCIENTIST_SCHOLARLY_ENDPOINTS", "https://api.scholar.example")
    monkeypatch.delenv("SCIENTIST_PROVIDER_ENDPOINT", raising=False)


def _map(monkeypatch, value):
    monkeypatch.setenv("SCIENTIST_PROVIDER_DESTINATIONS", value if isinstance(value, str) else json.dumps(value))


def test_valid_map_and_key_case_normalized(monkeypatch):
    _map(monkeypatch, {str(A).upper(): "https://a.example", str(B): "https://b.example"})
    assert settings.provider_destinations() == {str(A): "https://a.example", str(B): "https://b.example"}
    assert settings.provider_endpoint(A) == "https://a.example"
    assert settings.provider_endpoint(uuid4()) is None


@pytest.mark.parametrize("raw", [
    "{not json", "[]", '"x"', json.dumps({"not-a-uuid": "https://a.example"}),
    json.dumps({str(A): "http://a.example"}), json.dumps({str(A): "https://a.example:8443"}),
    json.dumps({str(A): "https://u:p@a.example"}), json.dumps({str(A): "https://localhost"}),
    json.dumps({str(A): "https://2130706433"}), json.dumps({str(A): "https://A.example/path"}),
    json.dumps({str(A): 5}),
    json.dumps({str(B): "https://b.example", str(A): "http://a.example"}),  # one bad entry voids the whole map
    json.dumps({str(A): "https://a.example", str(A).upper(): "https://b.example"}),  # duplicate after normalization
    json.dumps({str(A): "https://a.example"}) + " " * 17000,  # valid content, only the size bound rejects it
    '{"%s": "https://a.example", "%s": "https://b.example"}' % (A, A),  # exact duplicate key
    json.dumps({A.hex: "https://a.example"}), json.dumps({f"urn:uuid:{A}": "https://a.example"}),  # non-canonical key spellings
])
def test_invalid_map_fails_closed_whole(monkeypatch, raw):
    _map(monkeypatch, raw)
    assert settings.provider_destinations() == {}


def test_old_global_endpoint_configures_nothing(monkeypatch):
    monkeypatch.delenv("SCIENTIST_PROVIDER_DESTINATIONS", raising=False)
    monkeypatch.setenv("SCIENTIST_PROVIDER_ENDPOINT", "https://llm.example")
    assert settings.provider_destinations() == {} and settings.provider_endpoint(A) is None


def _run(db, project_session, provider):
    project_id, session_id = project_session
    owner = Principal(identity=uuid4(), kind="owner")
    return owner, submit_run(db, owner, project_id, session_id, f"k-{uuid4()}", "question", [], provider, "fixture")


def test_build_plan_uses_the_runs_provider_endpoint(db, project_session, monkeypatch):
    _map(monkeypatch, {str(A): "https://a.example", str(B): "https://b.example"})
    owner, run = _run(db, project_session, B)
    assert build_plan(db, owner, run.run_id, ["x"]).data_recipients == ["https://api.scholar.example", "https://b.example"]


def test_unmapped_provider_is_not_ready_even_if_another_is_mapped(db, project_session, monkeypatch):
    _map(monkeypatch, {str(A): "https://a.example"})
    owner, run = _run(db, project_session, B)
    with pytest.raises(DomainError) as error:
        build_plan(db, owner, run.run_id, ["x"])
    assert (error.value.code, error.value.status) == ("data_destinations_not_configured", 409)


def test_revise_plan_binds_recipients_to_the_plans_provider(db, project_session, monkeypatch):
    _map(monkeypatch, {str(A): "https://a.example", str(B): "https://b.example"})
    owner, run = _run(db, project_session, A)
    base = get_plan(db, owner, run.run_id).plan.model_dump(mode="json")

    def plan(recipients):
        return PlanSpec.model_validate({**base, "data_recipients": recipients})

    with pytest.raises(DomainError) as error:
        revise_plan(db, owner, run.run_id, run.revision, plan(["https://b.example"]))
    assert (error.value.code, error.value.status) == ("data_destinations_not_configured", 409)
    revise_plan(db, owner, run.run_id, run.revision, plan(["https://a.example", "https://api.scholar.example"]))
