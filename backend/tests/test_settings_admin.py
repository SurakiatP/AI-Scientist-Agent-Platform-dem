from __future__ import annotations

import json
from hashlib import sha256
from datetime import datetime, timedelta, timezone
import secrets
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text

from scientist.app import create_app
from scientist.db import create_project
from scientist.secrets import _fernet


PEER_SECRET = "synthetic-peer-bearer-never-return-this"


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
    return client


def test_peer_settings_show_only_trusted_masked_metadata(db, monkeypatch, tmp_path):
    key_file = tmp_path / "master.key"
    key_file.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("SCIENTIST_MASTER_KEY_FILE", str(key_file))
    peer_id = uuid4()
    monkeypatch.setenv("SCIENTIST_PEER_DESTINATIONS", json.dumps({str(peer_id): "https://peer.example"}))
    project_id = create_project(db, "peer settings")
    encrypted = _fernet().encrypt(PEER_SECRET.encode())
    db.execute(text("INSERT INTO credentials (id, project_id, label, provider, encrypted_value) VALUES (:id, :project, :label, :provider, :value)"), {
        "id": uuid4(), "project": project_id, "label": "Lab peer", "provider": f"peer:{peer_id}", "value": encrypted,
    })
    db.commit()

    from scientist import owner_settings

    client = _owner_client()
    client.app.include_router(owner_settings.router)
    response = client.get("/api/v1/peers")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()[0]["peer_id"] == str(peer_id)
    assert response.json()[0]["endpoint"] == "https://peer.example"
    assert response.json()[0]["credential_configured"] is True
    assert len(response.json()[0]["endpoint_fingerprint"]) == 64
    assert response.json()[0]["network_check"] == "not_run"
    assert response.json()[0]["configured"] is True
    assert PEER_SECRET not in response.text
    assert PEER_SECRET not in response.headers.values()


def test_peer_configuration_rejects_user_supplied_endpoint_authority(monkeypatch):
    peer_id = uuid4()
    monkeypatch.setenv("SCIENTIST_PEER_DESTINATIONS", json.dumps({str(peer_id): "https://trusted.example"}))
    from scientist import owner_settings

    configured = owner_settings.configured_peers()
    assert configured[str(peer_id)] == "https://trusted.example"
    monkeypatch.setenv("SCIENTIST_PEER_DESTINATIONS", json.dumps({"attacker": "https://evil.example"}))
    assert owner_settings.configured_peers() == {}
    monkeypatch.setenv("SCIENTIST_PEER_DESTINATIONS", f'{{"{peer_id}":"https://one.example","{peer_id}":"https://two.example"}}')
    assert owner_settings.configured_peers() == {}
    monkeypatch.setenv("SCIENTIST_PEER_DESTINATIONS", json.dumps({str(peer_id).upper(): "https://trusted.example"}))
    assert owner_settings.configured_peers() == {}


def test_access_tokens_are_listed_as_metadata_and_secret_is_returned_once(db, project_session):
    project_id, _ = project_session
    db.commit()
    from scientist import owner_settings

    client = _owner_client()
    client.app.include_router(owner_settings.router)
    created = client.post("/api/v1/access-tokens", json={
        "grants": [{"project_id": str(project_id), "actions": ["project:read", "result:read"]}],
    })
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    once = created.json()["token"]
    assert once and len(once) >= 40
    expires_at = datetime.fromisoformat(created.json()["expires_at"])
    assert timedelta(days=29, hours=23) < expires_at - datetime.now(timezone.utc) < timedelta(days=30, minutes=1)

    listed = client.get("/api/v1/access-tokens")
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    assert once not in listed.text
    assert "token_hash" not in listed.text and "hash" not in listed.text
    assert {"id", "expires_at", "revoked_at", "grants"} <= set(listed.json()[0])
    assert created.json()["token"] == once

    revoked = client.delete(f"/api/v1/access-tokens/{listed.json()[0]['id']}")
    assert revoked.status_code == 204
    assert client.get("/api/v1/access-tokens").json()[0]["revoked_at"] is not None
    stored_hash = db.execute(text("SELECT token_hash FROM access_tokens WHERE id=:id"), {
        "id": listed.json()[0]["id"],
    }).scalar_one()
    assert stored_hash != once and len(stored_hash.strip()) == 64
    assert stored_hash.strip() == sha256(once.encode()).hexdigest()
    missing = client.delete(f"/api/v1/access-tokens/{uuid4()}")
    assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store"
    assert once not in missing.text and stored_hash not in missing.text
    invalid_project = client.post("/api/v1/access-tokens", json={
        "grants": [{"project_id": str(uuid4()), "actions": ["project:read"]}],
    })
    assert invalid_project.status_code == 404
    assert invalid_project.headers["cache-control"] == "no-store"


def test_access_token_errors_never_echo_submitted_secret(db):
    from scientist import owner_settings

    client = _owner_client()
    client.app.include_router(owner_settings.router)
    response = client.post("/api/v1/access-tokens", json={"grants": [], "token": PEER_SECRET})
    assert response.status_code == 422
    assert PEER_SECRET not in response.text
    assert response.headers["cache-control"] == "no-store"
