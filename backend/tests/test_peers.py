from __future__ import annotations

import json
import secrets
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from scientist.app import create_app
from scientist.db import create_project
from scientist.secrets import _fernet


PEER_SECRET = "synthetic-peer-bearer-value"


def _owner_client():
    token = secrets.token_urlsafe(32)
    client = TestClient(create_app(bootstrap_token=token), client=("127.0.0.1", 12345))
    response = client.post(
        "/api/v1/bootstrap",
        headers={"host": "localhost", "origin": "http://localhost"},
        json={"token": token},
    )
    assert response.status_code == 200
    client.headers.update({
        "host": "localhost",
        "origin": "http://localhost",
        "x-csrf-token": response.json()["csrf_token"],
    })
    from scientist import peers

    client.app.include_router(peers.router)
    return client


def test_project_delegation_uses_trusted_endpoint_and_encrypted_credential(db, monkeypatch, tmp_path, caplog):
    key_file = tmp_path / "master.key"
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    monkeypatch.setenv("SCIENTIST_MASTER_KEY_FILE", str(key_file))
    peer_id = uuid4()
    monkeypatch.setenv("SCIENTIST_PEER_DESTINATIONS", json.dumps({str(peer_id): "https://trusted.example"}))
    project_id = create_project(db, "delegated project")
    db.commit()
    client = _owner_client()
    csrf = client.headers.pop("x-csrf-token")
    rejected = client.post("/api/v1/peer-delegations", json={
        "project_id": str(project_id),
        "peer_id": str(peer_id),
        "credential_label": "Synthetic peer key",
        "credential": PEER_SECRET,
    })
    assert rejected.status_code == 403
    assert PEER_SECRET not in rejected.text
    client.headers["x-csrf-token"] = csrf

    created = client.post("/api/v1/peer-delegations", json={
        "project_id": str(project_id),
        "peer_id": str(peer_id),
        "credential_label": "Synthetic peer key",
        "credential": PEER_SECRET,
    })
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    assert created.json()["endpoint"] == "https://trusted.example"
    assert created.json()["actions"] == ["peer"]
    assert PEER_SECRET not in created.text and PEER_SECRET not in caplog.text

    ref = db.execute(text(
        "SELECT id, encrypted_value, provider FROM credentials WHERE project_id=:project AND provider=:provider"
    ), {"project": project_id, "provider": f"peer:{peer_id}"}).one()
    assert str(ref.id) == created.json()["credential_id"]
    assert bytes(ref.encrypted_value) != PEER_SECRET.encode()
    assert _fernet().decrypt(bytes(ref.encrypted_value)).decode() == PEER_SECRET
    assert ref.provider == f"peer:{peer_id}"

    replacement_secret = "replacement-peer-secret"
    updated = client.put(f"/api/v1/peer-delegations/{created.json()['id']}/credential", json={
        "credential_label": "Rotated key",
        "credential": replacement_secret,
    })
    assert updated.status_code == 200
    assert updated.headers["cache-control"] == "no-store"
    assert replacement_secret not in updated.text
    assert updated.json()["credential_id"] != created.json()["credential_id"]
    updated_ref = db.execute(text("SELECT encrypted_value, label FROM credentials WHERE id=:id"), {
        "id": updated.json()["credential_id"],
    }).one()
    assert _fernet().decrypt(bytes(updated_ref.encrypted_value)).decode() == replacement_secret
    assert updated_ref.label == "Rotated key"

    listed = client.get(f"/api/v1/peer-delegations?project_id={project_id}")
    assert listed.status_code == 200
    assert PEER_SECRET not in listed.text
    assert replacement_secret not in listed.text
    assert listed.json()[0]["credential_id"] == updated.json()["credential_id"]

    duplicate = client.post("/api/v1/peer-delegations", json={
        "project_id": str(project_id),
        "peer_id": str(peer_id),
        "credential_label": "Duplicate",
        "credential": PEER_SECRET,
    })
    assert duplicate.status_code == 409
    assert duplicate.headers["cache-control"] == "no-store"
    assert PEER_SECRET not in duplicate.text

    deleted = client.delete(f"/api/v1/peer-delegations/{created.json()['id']}")
    assert deleted.status_code == 204
    assert db.execute(text("SELECT count(*) FROM delegations WHERE id=:id AND revoked_at IS NOT NULL"), {
        "id": created.json()["id"],
    }).scalar_one() == 1
    assert db.execute(text("SELECT count(*) FROM credentials WHERE id=:id"), {
        "id": updated.json()["credential_id"],
    }).scalar_one() == 0


def test_peer_release_api_does_not_accept_endpoint_or_arbitrary_actions(db, monkeypatch, project_session):
    from scientist import peers

    project_id, _ = project_session
    peer_id = uuid4()
    monkeypatch.setenv("SCIENTIST_PEER_DESTINATIONS", json.dumps({str(peer_id): "https://trusted.example"}))
    db.commit()
    client = _owner_client()
    payload = {
        "project_id": str(project_id),
        "peer_id": str(peer_id),
        "endpoint": "https://evil.example",
        "credential_label": "key",
        "credential": PEER_SECRET,
    }
    rejected = client.post("/api/v1/peer-delegations", json=payload)
    assert rejected.status_code == 422
    assert PEER_SECRET not in rejected.text
    assert "evil.example" not in rejected.text

    payload.pop("endpoint")
    payload["actions"] = ["project:read", "peer"]
    rejected = client.post("/api/v1/peer-delegations", json=payload)
    assert rejected.status_code == 422
    assert PEER_SECRET not in rejected.text
    assert db.execute(text("SELECT count(*) FROM delegations WHERE project_id=:project"), {
        "project": project_id,
    }).scalar_one() == 0


def test_peer_delegation_requires_csrf_and_owner_session(db):
    from scientist import peers

    token = secrets.token_urlsafe(32)
    bare = TestClient(create_app(bootstrap_token=token), client=("127.0.0.1", 12345))
    bare.app.include_router(peers.router)
    response = bare.post(
        "/api/v1/peer-delegations",
        headers={"host": "localhost", "origin": "http://localhost"},
        json={"project_id": str(uuid4()), "peer_id": str(uuid4()), "credential_label": "key", "credential": PEER_SECRET},
    )
    assert response.status_code == 401
    assert PEER_SECRET not in response.text


def test_peer_revocation_database_failure_is_sanitized_and_rolls_back(db, monkeypatch, tmp_path, caplog):
    from scientist import peers

    key_file = tmp_path / "master.key"
    key_file.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("SCIENTIST_MASTER_KEY_FILE", str(key_file))
    peer_id = uuid4()
    monkeypatch.setenv("SCIENTIST_PEER_DESTINATIONS", json.dumps({str(peer_id): "https://trusted.example"}))
    project_id = create_project(db, "revocation failure")
    db.commit()
    client = _owner_client()
    created = client.post("/api/v1/peer-delegations", json={
        "project_id": str(project_id),
        "peer_id": str(peer_id),
        "credential_label": "Synthetic peer key",
        "credential": PEER_SECRET,
    })
    assert created.status_code == 201

    original_execute = Session.execute

    def fail_secret_delete(self, statement, *args, **kwargs):
        if "DELETE FROM credentials" in str(statement):
            raise RuntimeError(PEER_SECRET)
        return original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", fail_secret_delete)
    failed = client.delete(f"/api/v1/peer-delegations/{created.json()['id']}")
    assert failed.status_code == 503
    assert failed.headers["cache-control"] == "no-store"
    assert PEER_SECRET not in failed.text and PEER_SECRET not in caplog.text
    assert db.execute(text("SELECT revoked_at FROM delegations WHERE id=:id"), {
        "id": created.json()["id"],
    }).scalar_one() is None
    assert db.execute(text("SELECT count(*) FROM credentials WHERE id=:id"), {
        "id": created.json()["credential_id"],
    }).scalar_one() == 1
