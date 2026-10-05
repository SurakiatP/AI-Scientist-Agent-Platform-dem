"""The external protocol listener never inherits local owner authority."""
from fastapi.testclient import TestClient


def test_external_listener_has_no_owner_routes_and_rejects_wrong_host():
    from scientist.protocol_app import create_protocol_app
    app = create_protocol_app(public_base_url="http://127.0.0.1")
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as client:
        for path in ("/api/v1/projects", "/api/v1/connections", "/api/v1/bootstrap", "/docs", "/openapi.json"):
            assert client.get(path).status_code == 404
        assert client.get("/.well-known/agent-card.json").status_code == 200
        assert client.get("/.well-known/agent-card.json", headers={"Host": "unconfigured.example"}).status_code == 403
        assert client.post("/mcp", json={}, headers={"Origin": "https://unconfigured.example"}).status_code == 403
        assert client.post("/mcp", json={}).status_code == 401
        assert client.post("/mcp", json={}, cookies={"scientist_owner": "owner-cookie-is-not-a-bearer"}).status_code == 401


def test_nonlocal_plain_http_profile_is_rejected_before_listener_creation():
    import pytest
    from scientist.protocol_app import create_protocol_app
    with pytest.raises(ValueError, match="https_required"):
        create_protocol_app(public_base_url="http://scientist.example")
