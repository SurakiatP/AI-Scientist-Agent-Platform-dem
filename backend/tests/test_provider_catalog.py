from types import SimpleNamespace
from uuid import uuid4

import pytest

from scientist import broker, settings
from scientist.auth import DomainError
from scientist.broker import DispatchTarget
from scientist.provider_catalog import api_path, catalog
from test_rest_workflow import client, make_client, storage


def test_catalog_binds_only_known_origins_to_real_uuids():
    configured_id = str(uuid4())
    rows = catalog({
        configured_id: "https://api.openai.com",
        str(uuid4()): "https://custom.example",
        "stale-id": "https://openrouter.ai",
    })
    providers = {row["slug"]: row for row in rows}

    assert providers["openai"]["provider_id"] == configured_id
    assert providers["openai"]["available"] is True
    assert providers["openrouter"]["provider_id"] is None
    assert providers["openrouter"]["reason"] == "not_configured"
    assert "custom.example" not in {row["origin"] for row in rows}
    assert providers["anthropic"]["reason"] == "native_adapter_required"
    assert providers["google-gemini"]["reason"] == "native_adapter_required"
    assert providers["minimax"]["reason"] == "native_adapter_required"
    assert providers["lmstudio"]["reason"] == "local_isolation_required"
    assert providers["openai-codex"]["reason"] == "oauth_required"


def test_provider_route_is_owner_only_no_store_and_uses_configured_map(client, monkeypatch):
    provider_id = str(uuid4())
    monkeypatch.setattr(settings, "provider_destinations", lambda: {provider_id: "https://openrouter.ai"})

    response = client.get("/api/v1/providers")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    providers = {row["slug"]: row for row in response.json()}
    assert providers["openrouter"]["provider_id"] == provider_id
    assert providers["openrouter"]["available"] is True
    assert make_client(client.app).get("/api/v1/providers", headers={"host": "localhost"}).status_code == 401


@pytest.mark.parametrize(
    ("origin", "url_path", "expected_path"),
    [
        ("https://api.openai.com", "/", "/v1/chat/completions"),
        ("https://openrouter.ai", "/", "/api/v1/chat/completions"),
        ("https://custom.example", "/legacy/completions", "/legacy/completions"),
    ],
)
def test_provider_dispatch_uses_curated_or_approved_path_and_holds_auth(monkeypatch, db, origin, url_path, expected_path):
    validated = []
    sent = []

    def validate(url, recipients):
        validated.append((url, recipients))
        return origin.removeprefix("https://"), 443, url.removeprefix(origin) or "/", "203.0.113.10"

    class Response:
        status = 200

        def getheader(self, name):
            return None

        def read(self, size):
            return b'{"usage":{"total_tokens":3}}'

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, *, body, headers):
            sent.append((method, path, body, headers))

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(broker, "_validate_url", validate)
    monkeypatch.setattr(broker, "_PinnedHTTPSConnection", Connection)
    monkeypatch.setattr(broker, "read_secret", lambda session, credential_id: "synthetic-secret")
    token = broker._active_session.set(db)
    try:
        broker.http_transport(
            SimpleNamespace(payload={"messages": [{"role": "user", "content": "hello"}], "max_output_tokens": 5}),
            DispatchTarget("llm", origin + url_path, (origin,), uuid4(), model="fixture-model"),
        )
    finally:
        broker._active_session.reset(token)

    assert validated == [(origin + url_path, [origin])]
    assert len(sent) == 1
    method, path, _, headers = sent[0]
    assert method == "POST"
    assert path == expected_path
    assert headers["Authorization"] == "Bearer synthetic-secret"


def test_native_provider_fails_before_credential_lookup(monkeypatch, db):
    monkeypatch.setattr(
        broker,
        "_validate_url",
        lambda url, recipients: ("api.anthropic.com", 443, "/", "203.0.113.10"),
    )
    monkeypatch.setattr(broker, "read_secret", lambda *args: pytest.fail("credential must stay unread"))
    token = broker._active_session.set(db)
    try:
        with pytest.raises(DomainError) as exc:
            broker.http_transport(
                SimpleNamespace(payload={"messages": [{"role": "user", "content": "hello"}], "max_output_tokens": 5}),
                DispatchTarget("llm", "https://api.anthropic.com/", ("https://api.anthropic.com",), uuid4(), model="fixture"),
            )
    finally:
        broker._active_session.reset(token)
    assert exc.value.code == "provider_unavailable"


def test_provider_redirect_fails_without_resending(monkeypatch, db):
    requests = []

    class Response:
        status = 307

        def getheader(self, name):
            return "https://other.example/collect" if name == "Location" else None

        def read(self, size):
            return b"{}"

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            requests.append(args)

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(broker, "_validate_url", lambda url, recipients: ("api.openai.com", 443, "/", "203.0.113.10"))
    monkeypatch.setattr(broker, "_PinnedHTTPSConnection", Connection)
    monkeypatch.setattr(broker, "read_secret", lambda session, credential_id: "synthetic-secret")
    token = broker._active_session.set(db)
    try:
        with pytest.raises(DomainError) as exc:
            broker.http_transport(
                SimpleNamespace(payload={"messages": [{"role": "user", "content": "hello"}], "max_output_tokens": 5}),
                DispatchTarget("llm", "https://api.openai.com/", ("https://api.openai.com",), uuid4(), model="fixture"),
            )
    finally:
        broker._active_session.reset(token)
    assert exc.value.code == "provider_unavailable"
    assert len(requests) == 1


def test_custom_and_unknown_paths_keep_existing_behavior():
    assert api_path("https://custom.example") is None
    assert api_path("https://api.openai.com") == "/v1/chat/completions"
