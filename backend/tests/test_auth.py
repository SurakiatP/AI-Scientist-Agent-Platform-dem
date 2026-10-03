from datetime import datetime, timedelta, timezone
from hashlib import sha256
from secrets import token_urlsafe
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from cryptography.fernet import Fernet

from scientist.app import create_app
from scientist.auth import DomainError, authenticate_bearer, authorize, create_token, revoke_token
from scientist.contracts import Principal
from scientist.secrets import read_secret, save_secret


def test_owner_bootstrap_is_loopback_same_origin_single_use_and_csrf_protected(db):
    bootstrap = token_urlsafe(32)
    client = TestClient(create_app(bootstrap_token=bootstrap), client=("127.0.0.1", 12345))
    assert client.post("/api/v1/bootstrap", headers={"host": "evil.example", "origin": "http://evil.example"}, json={"token": bootstrap}).status_code == 403
    assert client.post("/api/v1/bootstrap", headers={"host": "localhost", "origin": "http://localhost"}, json={"token": "wrong"}).status_code == 401
    assert client.post("/api/v1/bootstrap", headers={"host": "localhost", "origin": "http://evil.example"}, json={"token": bootstrap}).status_code == 403
    response = client.post("/api/v1/bootstrap", headers={"host": "localhost", "origin": "http://localhost"}, json={"token": bootstrap})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=lax" in response.headers["set-cookie"]
    assert "; Path=/api/v1;" in response.headers["set-cookie"]
    assert response.headers["cache-control"] == "no-store"
    bootstrap_csrf = response.json()["csrf_token"]
    refreshed = client.get("/api/v1/owner/session", headers={"host": "localhost"})
    assert refreshed.json()["kind"] == "owner"
    assert refreshed.headers["cache-control"] == "no-store"
    csrf = refreshed.json()["csrf_token"]
    assert client.get("/api/v1/runs", headers={"host": "localhost"}).status_code == 404
    assert client.post("/api/v1/runs", headers={"host": "localhost", "origin": "http://localhost"}).status_code == 403
    assert client.post("/api/v1/owner/check", headers={"host": "localhost", "origin": "http://localhost"}).status_code == 403
    assert client.post("/api/v1/owner/check", headers={"host": "localhost", "origin": "http://localhost", "x-csrf-token": bootstrap_csrf}).status_code == 403
    assert client.post("/api/v1/owner/check", headers={"host": "localhost", "origin": "http://localhost", "x-csrf-token": csrf}).status_code == 404
    assert client.post("/api/v1/bootstrap", headers={"host": "localhost", "origin": "http://localhost"}, json={"token": bootstrap}).status_code == 401
    restarted = TestClient(create_app(bootstrap_token=bootstrap), client=("127.0.0.1", 12345))
    assert restarted.post("/api/v1/bootstrap", headers={"host": "localhost", "origin": "http://localhost"}, json={"token": bootstrap}).status_code == 401


def test_expired_owner_session_is_rejected(db):
    bootstrap = token_urlsafe(32)
    app = create_app(bootstrap_token=bootstrap)
    client = TestClient(app, client=("127.0.0.1", 12345))
    bootstrap_owner(client, bootstrap)
    session_id = client.cookies.get("owner_session")
    db.execute(text("UPDATE owner_sessions SET expires_at = :expired WHERE token_hash = :hash"), {
        "expired": datetime.now(timezone.utc) - timedelta(seconds=1),
        "hash": sha256(session_id.encode()).hexdigest(),
    })
    db.commit()
    assert client.get("/api/v1/owner/session", headers={"host": "localhost"}).status_code == 401


def test_bearer_tokens_are_scoped_expiring_and_revocable(db, project_session):
    project_id, _ = project_session
    owner = Principal(identity=uuid4(), kind="owner")
    token = create_token(db, owner, {project_id: ["project:read", "work:submit"]})
    token_id = db.execute(text("SELECT id FROM access_tokens WHERE owner_identity = :owner"), {"owner": owner.identity}).scalar_one()
    db.commit()
    external = authenticate_bearer(db, token)
    assert external.kind == "external"
    authorize(db, external, "work:submit", project_id)
    with pytest.raises(DomainError, match="forbidden"):
        authorize(db, external, "plan:approve", project_id)
    with pytest.raises(DomainError, match="forbidden"):
        authorize(db, external, "work:submit", uuid4())
    revoke_token(db, owner, token_id)
    db.commit()
    with pytest.raises(DomainError, match="forbidden"):
        authenticate_bearer(db, token)
    with pytest.raises(DomainError, match="forbidden"):
        authorize(db, external, "work:submit", project_id)


def test_expired_bearer_is_rejected_and_bearer_cannot_open_owner_session(db, project_session):
    project_id, _ = project_session
    owner = Principal(identity=uuid4(), kind="owner")
    token = create_token(db, owner, {project_id: ["project:read"]})
    token_id = db.execute(text("SELECT id FROM access_tokens WHERE owner_identity = :owner"), {"owner": owner.identity}).scalar_one()
    db.commit()
    client = TestClient(create_app(bootstrap_token="u" * 40), client=("127.0.0.1", 12345))
    assert client.get("/api/v1/owner/session", headers={"host": "localhost", "authorization": f"Bearer {token}"}).status_code == 401
    db.execute(text("UPDATE access_tokens SET expires_at = :expiry WHERE id = :id"), {
        "expiry": datetime.now(timezone.utc) - timedelta(seconds=1), "id": token_id,
    })
    db.commit()
    with pytest.raises(DomainError, match="forbidden"):
        authenticate_bearer(db, token)


def test_encrypted_secret_fails_closed_without_master_key_and_round_trips(monkeypatch, db, tmp_path):
    owner = Principal(identity=uuid4(), kind="owner")
    monkeypatch.delenv("SCIENTIST_MASTER_KEY_FILE", raising=False)
    with pytest.raises(DomainError, match="storage_unavailable"):
        save_secret(db, owner, "fixture", "synthetic-only")
    key_file = tmp_path / "test-master-key"
    key_file.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("SCIENTIST_MASTER_KEY_FILE", str(key_file))
    credential_id = save_secret(db, owner, "fixture", "synthetic-only")
    assert read_secret(db, credential_id) == "synthetic-only"
    stored = db.execute(text("SELECT encrypted_value FROM credentials WHERE id = :id"), {"id": credential_id}).scalar_one()
    assert b"synthetic-only" not in stored


def test_expired_bootstrap_is_rejected():
    with pytest.raises(ValueError, match="expiry"):
        create_app(bootstrap_token="e" * 40, bootstrap_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))


def test_bootstrap_requires_high_entropy_token():
    with pytest.raises(ValueError, match="32 characters"):
        create_app(bootstrap_token="")


def bootstrap_owner(client, token):
    response = client.post("/api/v1/bootstrap", headers={"host": "localhost", "origin": "http://localhost"}, json={"token": token})
    assert response.status_code == 200
