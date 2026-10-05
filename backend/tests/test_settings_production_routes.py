from test_settings_admin import _owner_client


def test_owner_composition_mounts_admin_routes_without_test_registration(db):
    client = _owner_client()
    for path in ["/api/v1/peers", "/api/v1/access-tokens", "/api/v1/peer-delegations"]:
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers.get("cache-control") == "no-store"


def test_owner_guard_rejections_are_not_cached(db):
    client = _owner_client()
    client.headers.pop("x-csrf-token")
    response = client.post("/api/v1/access-tokens", json={})
    assert response.status_code == 403
    assert response.headers.get("cache-control") == "no-store"
