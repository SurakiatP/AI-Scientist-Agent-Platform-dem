from __future__ import annotations

import json
import time
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text

from scientist import broker, connection_test, settings
from scientist.app import create_app
from scientist.auth import Principal


@pytest.fixture(autouse=True)
def master_key(monkeypatch, tmp_path):
    key = tmp_path / "master"
    key.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("SCIENTIST_MASTER_KEY_FILE", str(key))


@pytest.fixture(autouse=True)
def clean_connections(db):
    db.execute(text("DELETE FROM credentials WHERE model IS NOT NULL"))
    db.commit()
    yield
    db.execute(text("DELETE FROM credentials WHERE model IS NOT NULL"))
    db.commit()


@pytest.fixture
def client(db):
    from secrets import token_urlsafe

    token = token_urlsafe(32)
    client = TestClient(create_app(bootstrap_token=token), client=("127.0.0.1", 12345))
    boot = client.post(
        "/api/v1/bootstrap",
        headers={"host": "localhost", "origin": "http://localhost"},
        json={"token": token},
    )
    assert boot.status_code == 200
    client.headers.update(
        {
            "host": "localhost",
            "origin": "http://localhost",
            "x-csrf-token": boot.json()["csrf_token"],
        }
    )
    return client


class FakeResponse:
    def __init__(self, status=200, body=b'{"data": []}', headers=None, length="auto"):
        self.status = status
        self._body = body
        self._headers = headers or {}
        self.length = len(body) if length == "auto" else length

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read(self, limit):
        return self._body[:limit]


class FakeConnection:
    made = []
    response = FakeResponse()

    def __init__(self, host, ip, port, timeout):
        self.host, self.ip, self.port, self.timeout = host, ip, port, timeout
        self.requests = []
        self.closed = False
        self.__class__.made.append(self)

    def request(self, method, path, *, headers):
        self.requests.append((method, path, headers))

    def getresponse(self):
        return self.__class__.response

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_real_provider_calls(monkeypatch):
    FakeConnection.made = []
    FakeConnection.response = FakeResponse()
    monkeypatch.setattr(broker, "_resolver", lambda _host, _port: ["8.8.8.8"])
    monkeypatch.setattr(broker, "_PinnedHTTPSConnection", FakeConnection)


def save_connection(client, monkeypatch, origin="https://openrouter.ai", model="x/model"):
    connection_id = uuid4()
    monkeypatch.setattr(settings, "provider_destinations", lambda: {str(connection_id): origin})
    response = client.post(
        "/api/v1/connections",
        json={"provider_id": str(connection_id), "label": "test", "model": model, "secret": "synthetic-key"},
    )
    assert response.status_code == 201
    return connection_id


def use_fake_transport(monkeypatch, *, addresses=("8.8.8.8",), response=None):
    FakeConnection.made = []
    FakeConnection.response = response or FakeResponse()
    monkeypatch.setattr(broker, "_resolver", lambda _host, _port: list(addresses))
    monkeypatch.setattr(broker, "_PinnedHTTPSConnection", FakeConnection)


def test_connection_test_owner_csrf_and_empty_body_only(client, monkeypatch):
    connection_id = save_connection(client, monkeypatch)
    use_fake_transport(monkeypatch)
    assert client.post(f"/api/v1/connections/{connection_id}/test", headers={"x-csrf-token": "bad"}).status_code == 403
    assert client.post(f"/api/v1/connections/{connection_id}/test", content=b'{"secret":"leak"}').status_code == 400
    assert FakeConnection.made == []
    assert client.post(f"/api/v1/connections/{connection_id}/test", headers={"cookie": ""}).status_code == 401


def test_openrouter_uses_one_fixed_authenticated_get_and_does_not_touch_ledger(client, db, monkeypatch, caplog):
    connection_id = save_connection(client, monkeypatch)
    use_fake_transport(monkeypatch, response=FakeResponse(body=b'{"data":{"label":"fixture"}}'))
    before = db.execute(text("SELECT COALESCE(sum(usage_tokens + reserved_tokens), 0) FROM runs")).scalar_one()
    operations = db.execute(text("SELECT count(*) FROM operations")).scalar_one()
    response = client.post(f"/api/v1/connections/{connection_id}/test")
    after = db.execute(text("SELECT COALESCE(sum(usage_tokens + reserved_tokens), 0) FROM runs")).scalar_one()
    assert db.execute(text("SELECT count(*) FROM operations")).scalar_one() == operations
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "connection_id": str(connection_id),
        "status": "reachable",
        "credential_status": "accepted",
        "model_status": "not_tested",
    }
    assert before == after
    assert len(FakeConnection.made) == 1
    connection = FakeConnection.made[0]
    assert connection.requests[0][0:2] == ("GET", "/api/v1/key")
    assert connection.requests[0][2]["Authorization"] == "Bearer synthetic-key"
    assert connection.closed
    assert "synthetic-key" not in response.text
    assert "synthetic-key" not in caplog.text


@pytest.mark.parametrize(
    ("status", "expected", "credential_status"),
    [
        (401, "credentials_rejected", "rejected"),
        (403, "denied", "unverified"),
        (429, "rate_limited", "unverified"),
        (500, "unavailable", "unverified"),
    ],
)
def test_http_status_mapping(client, monkeypatch, status, expected, credential_status):
    connection_id = save_connection(client, monkeypatch)
    use_fake_transport(monkeypatch, response=FakeResponse(status=status))
    response = client.post(f"/api/v1/connections/{connection_id}/test")
    assert response.status_code == 200
    assert response.json()["status"] == expected
    assert response.json()["credential_status"] == credential_status
    assert len(FakeConnection.made[0].requests) == 1
    assert FakeConnection.made[0].closed


def test_openai_probe_is_fixed_and_unsupported_catalog_entries_do_not_call(client, monkeypatch):
    openai_id = save_connection(client, monkeypatch, "https://api.openai.com")
    use_fake_transport(monkeypatch, response=FakeResponse(body=b'{"object":"list","data":[]}'))
    response = client.post(f"/api/v1/connections/{openai_id}/test")
    assert response.json()["status"] == "reachable"
    assert FakeConnection.made[0].requests[0][0:2] == ("GET", "/v1/models")

    unsupported_id = save_connection(client, monkeypatch, "https://api.deepseek.com")
    use_fake_transport(monkeypatch)
    unsupported = client.post(f"/api/v1/connections/{unsupported_id}/test")
    assert unsupported.json()["status"] == "unsupported"
    assert unsupported.json()["credential_status"] == "unverified"
    assert FakeConnection.made == []


def test_public_ipv6_destination_is_pinned(client, monkeypatch):
    connection_id = save_connection(client, monkeypatch)
    address = "2001:4860:4860::8888"
    use_fake_transport(monkeypatch, addresses=(address,), response=FakeResponse(body=b'{"data":{}}'))
    assert client.post(f"/api/v1/connections/{connection_id}/test").json()["status"] == "reachable"
    assert FakeConnection.made[0].ip == address


def test_revoked_changed_origin_and_decrypt_failure_fail_closed(client, db, monkeypatch):
    revoked = save_connection(client, monkeypatch)
    use_fake_transport(monkeypatch)
    assert client.delete(f"/api/v1/connections/{revoked}").status_code == 204
    missing = client.post(f"/api/v1/connections/{revoked}/test")
    assert missing.status_code == 404, missing.text

    changed = save_connection(client, monkeypatch)
    use_fake_transport(monkeypatch)
    monkeypatch.setattr(settings, "provider_destinations", lambda: {str(changed): "https://api.openai.com"})
    changed_result = client.post(f"/api/v1/connections/{changed}/test").json()
    assert changed_result["status"] == "unavailable"
    assert FakeConnection.made == []

    broken = save_connection(client, monkeypatch)
    db.execute(text("UPDATE credentials SET encrypted_value = :bad WHERE id = :id"), {"bad": b"bad", "id": broken})
    db.commit()
    monkeypatch.setattr(settings, "provider_destinations", lambda: {str(broken): "https://openrouter.ai"})
    broken_result = client.post(f"/api/v1/connections/{broken}/test").json()
    assert broken_result["status"] == "unavailable"
    assert FakeConnection.made == []


@pytest.mark.parametrize(
    "body",
    [b"not json", b'{"data":{},"data":{}}', b'{"data":NaN}', b'{"data":[]}', b" " * (64 * 1024 + 1)],
)
def test_malformed_or_oversized_success_response_is_not_accepted(client, monkeypatch, body):
    connection_id = save_connection(client, monkeypatch)
    use_fake_transport(monkeypatch, response=FakeResponse(body=body, length=None))
    result = client.post(f"/api/v1/connections/{connection_id}/test").json()
    assert result["status"] == "unavailable"
    assert result["credential_status"] == "unverified"


def test_redirect_is_not_followed(client, monkeypatch):
    connection_id = save_connection(client, monkeypatch)
    use_fake_transport(monkeypatch, response=FakeResponse(302, headers={"Location": "https://attacker.example/"}))
    assert client.post(f"/api/v1/connections/{connection_id}/test").json()["status"] == "unavailable"
    assert len(FakeConnection.made) == 1
    assert FakeConnection.made[0].requests[0][0] == "GET"


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "fe80::1"])
def test_private_and_scoped_ipv6_dns_results_are_blocked(client, monkeypatch, address):
    connection_id = save_connection(client, monkeypatch)
    use_fake_transport(monkeypatch, addresses=(address,))
    assert client.post(f"/api/v1/connections/{connection_id}/test").json()["status"] == "unavailable"
    assert FakeConnection.made == []


def test_dns_deadline_includes_resolution(client, monkeypatch):
    connection_id = save_connection(client, monkeypatch)
    monkeypatch.setattr(connection_test, "_MAX_TOTAL_SECONDS", 0.03)
    monkeypatch.setattr(broker, "_resolver", lambda *_: time.sleep(0.2) or ["8.8.8.8"])
    monkeypatch.setattr(broker, "_PinnedHTTPSConnection", FakeConnection)
    started = time.monotonic()
    result = client.post(f"/api/v1/connections/{connection_id}/test").json()
    assert result["status"] == "unavailable"
    assert time.monotonic() - started < 0.15
    assert FakeConnection.made == []


def test_invalid_saved_secret_control_characters_are_never_sent(client, db, monkeypatch):
    connection_id = save_connection(client, monkeypatch)
    from scientist import secrets

    monkeypatch.setattr(secrets, "read_secret", lambda *_: "key\r\nInjected: true")
    use_fake_transport(monkeypatch)
    assert client.post(f"/api/v1/connections/{connection_id}/test").json()["status"] == "unavailable"
    assert FakeConnection.made == []
