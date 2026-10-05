from secrets import token_urlsafe
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text

from scientist import broker, secrets as secret_store, settings
from scientist.app import create_app
from scientist.auth import DomainError
from scientist.contracts import ConnectionView, OperationRequest, PlanSpec, Principal

SECRET = "sk-synthetic-connection-secret"


@pytest.fixture(autouse=True)
def master_key(monkeypatch, tmp_path):
    key = tmp_path / "master"
    key.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("SCIENTIST_MASTER_KEY_FILE", str(key))


@pytest.fixture
def client(db):
    token = token_urlsafe(32)
    c = TestClient(create_app(bootstrap_token=token), client=("127.0.0.1", 12345))
    resp = c.post("/api/v1/bootstrap", headers={"host": "localhost", "origin": "http://localhost"}, json={"token": token})
    assert resp.status_code == 200
    c.headers.update({"host": "localhost", "origin": "http://localhost", "x-csrf-token": resp.json()["csrf_token"]})
    return c


@pytest.fixture(autouse=True)
def clean(db):
    db.execute(text("DELETE FROM credentials WHERE model IS NOT NULL"))
    db.commit()


def body(pid=None, **over):
    return {"provider_id": str(pid or uuid4()), "label": "Lab key", "model": "m-1", "secret": SECRET, **over}


def test_create_list_revoke_round_trip_never_leaks_secret(client, caplog):
    pid = uuid4()
    r = client.post("/api/v1/connections", json=body(pid))
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    view = ConnectionView.model_validate(r.json())
    assert (str(view.id), view.model, view.state, view.has_secret) == (str(pid), "m-1", "ready", True)
    listed = client.get("/api/v1/connections")
    assert listed.headers["cache-control"] == "no-store"
    assert [ConnectionView.model_validate(x).id for x in listed.json()] == [pid]
    assert SECRET not in r.text + listed.text + caplog.text
    assert client.delete(f"/api/v1/connections/{pid}").status_code == 204
    assert client.get("/api/v1/connections").json() == []
    assert client.delete(f"/api/v1/connections/{pid}").status_code == 404


def test_validation_and_conflicts(client):
    assert client.post("/api/v1/connections", json=body(extra=1)).status_code == 422
    assert client.post("/api/v1/connections", json=body(secret="")).status_code == 422
    assert client.post("/api/v1/connections", json=body(model="")).status_code == 422
    assert client.post("/api/v1/connections", json=body(label="x" * 201)).status_code == 422
    pid = uuid4()
    assert client.post("/api/v1/connections", json=body(pid)).status_code == 201
    dup = client.post("/api/v1/connections", json=body(pid))
    assert (dup.status_code, dup.json()["code"]) == (409, "connection_exists")
    blank = client.post("/api/v1/connections", json=body(label="   "))
    assert (blank.status_code, blank.json()["code"]) == (422, "invalid_request")


def test_422_never_echoes_secret(client):
    missing = body(); del missing["model"]
    for payload in (missing, body(secret="s" * 16385), body(secret=[SECRET]), body(label=[SECRET])):
        r = client.post("/api/v1/connections", json=payload)
        assert r.status_code == 422 and r.json()["code"] == "invalid_request"
        assert set(r.json()) == {"code", "message", "request_id"}
        assert SECRET not in r.text and "s" * 100 not in r.text
        assert r.headers["cache-control"] == "no-store"
    other = client.post("/api/v1/projects", json={"nope": 1})
    assert other.status_code == 422 and other.json()["code"] == "invalid_request"


def test_unconfigured_destination_is_409_and_stores_nothing(client, db, monkeypatch):
    monkeypatch.setattr(settings, "provider_destinations", lambda: {})
    assert client.post("/api/v1/connections", json=body()).json()["code"] == "provider_not_configured"
    assert db.execute(text("SELECT count(*) FROM credentials WHERE model IS NOT NULL")).scalar_one() == 0


def test_requires_csrf_and_owner(client):
    assert client.post("/api/v1/connections", json=body(), headers={"x-csrf-token": "wrong"}).status_code == 403
    pid = uuid4()
    client.post("/api/v1/connections", json=body(pid))
    assert client.delete(f"/api/v1/connections/{pid}", headers={"x-csrf-token": ""}).status_code == 403
    bare = TestClient(client.app, client=("127.0.0.1", 12345), headers={"host": "localhost", "origin": "http://localhost"})
    assert bare.get("/api/v1/connections").status_code == 401
    with pytest.raises(DomainError) as err:
        secret_store.list_connections(None, Principal(identity=uuid4(), kind="external"))
    assert err.value.status == 403
    ext = Principal(identity=uuid4(), kind="external")
    for call in (lambda: secret_store.create_connection(None, ext, uuid4(), "l", "m", "s"), lambda: secret_store.revoke_connection(None, ext, uuid4())):
        with pytest.raises(DomainError) as e:
            call()
        assert e.value.status == 403


def test_legacy_rows_hidden_and_states(client, db, monkeypatch):
    legacy = uuid4()
    db.execute(text("INSERT INTO credentials (id, label, provider, encrypted_value) VALUES (:id, 'old', 'manual', :v)"), {"id": legacy, "v": b"x"})
    ok, bad, gone = uuid4(), uuid4(), uuid4()
    for pid in (ok, bad, gone):
        assert client.post("/api/v1/connections", json=body(pid)).status_code == 201
    db.execute(text("UPDATE credentials SET encrypted_value = :v WHERE id = :id"), {"v": b"corrupt", "id": bad})
    db.commit()
    monkeypatch.setattr(settings, "provider_destinations", lambda: {str(ok): "https://research.example", str(bad): "https://research.example"})
    states = {x["id"]: x["state"] for x in client.get("/api/v1/connections").json()}
    assert states == {str(ok): "ready", str(bad): "invalid_credentials", str(gone): "unavailable_provider"}
    assert str(legacy) not in states
    monkeypatch.setattr(settings, "provider_destinations", lambda: {str(ok): "https://moved.example"})
    shown = {x["id"]: x["provider"] for x in client.get("/api/v1/connections").json()}
    assert shown[str(ok)] == "https://moved.example"  # current destination, not stored one
    assert client.delete(f"/api/v1/connections/{legacy}").status_code == 404  # legacy rows are not connections


def test_revoke_denies_llm_scope(client, db, monkeypatch):
    pid = uuid4()
    monkeypatch.setattr(broker, "_resolver", lambda host, port: ["8.8.8.8"])
    monkeypatch.setattr(broker, "_provider_destinations", {str(pid): "https://research.example"})
    plan = PlanSpec(input_snapshot_digest="a" * 64, provider_id=pid, model="m-1", stages=["s"], allowed_ops=["llm"],
                    data_recipients=["https://research.example"], packages=[], token_limit=100, elapsed_limit_ms=1000)
    req = OperationRequest(run_id=uuid4(), generation=1, operation_id="o", kind="llm", reserve_tokens=500, payload={
        "provider_id": str(pid), "model": "m-1", "recipient": "https://research.example", "credential_id": str(pid), "max_output_tokens": 5, "prompt": "q"})
    project = uuid4()
    with pytest.raises(DomainError) as before:
        broker._validate_scope(db, project, plan, req)
    assert before.value.status == 403  # no credential yet
    assert client.post("/api/v1/connections", json=body(pid)).status_code == 201
    assert broker._validate_scope(db, project, plan, req).kind == "llm"
    assert client.delete(f"/api/v1/connections/{pid}").status_code == 204
    with pytest.raises(DomainError) as after:
        broker._validate_scope(db, project, plan, req)
    assert after.value.status == 403


def test_migration_009_adds_nullable_model(db):
    row = db.execute(text("SELECT is_nullable FROM information_schema.columns WHERE table_name='credentials' AND column_name='model'")).scalar_one()
    assert row == "YES"


def test_mutations_without_origin_are_refused(client):
    no_origin = {k: v for k, v in client.headers.items() if k.lower() != "origin"}
    pid = uuid4()
    r = client.post("/api/v1/connections", json=body(pid), headers={**no_origin, "origin": ""})
    assert r.status_code == 403 and SECRET not in r.text
    assert client.get("/api/v1/connections").json() == []
