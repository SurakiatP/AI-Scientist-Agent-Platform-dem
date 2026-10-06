from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scientist import host
from scientist.app import create_app
from scientist.web_assets import create_web_router


def test_web_distribution_is_optional_for_existing_host_config():
    assert host.HostConfig.model_fields["web_dist_dir"].default is None


def test_local_host_serves_spa_assets_and_keeps_api_and_protocol_routes_isolated(tmp_path: Path):
    (tmp_path / "index.html").write_text("<main>built app</main>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log('built')", encoding="utf-8")
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "leak.txt").write_text("private", encoding="utf-8")
    (tmp_path / "control").write_text("private", encoding="utf-8")
    (tmp_path / "mcp").write_text("private", encoding="utf-8")
    (tmp_path / "a2a").write_text("private", encoding="utf-8")
    (tmp_path / ".env").write_text("private", encoding="utf-8")
    app: FastAPI = create_app(bootstrap_token="b" * 32)
    app.include_router(create_web_router(tmp_path))
    client = TestClient(app, client=("127.0.0.1", 12345))

    assert client.get("/", headers={"accept": "text/html"}).text == "<main>built app</main>"
    assert client.get("/projects/123/sessions/456", headers={"accept": "text/html"}).text == "<main>built app</main>"
    assert client.get("/assets/app.js").text == "console.log('built')"
    assert client.get("/assets/missing.js").status_code == 404
    assert client.get("/not-a-route", headers={"accept": "text/html"}).status_code == 404
    for private_path in ("/api/leak.txt", "/control", "/mcp", "/a2a", "/.env"):
        response = client.get(private_path, headers={"accept": "text/html"})
        assert response.status_code == 404
        assert response.text != "private"
    assert client.get("/api/v1/missing", headers={"host": "127.0.0.1"}).status_code == 401
    assert client.get("/mcp", headers={"accept": "text/html"}).status_code == 404
    assert client.get("/..%2Fsecret", headers={"accept": "text/html"}).status_code == 404


def test_web_distribution_rejects_symlinked_index(tmp_path: Path):
    outside = tmp_path / "outside.html"
    outside.write_text("external", encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").symlink_to(outside)
    with pytest.raises(ValueError, match="regular index.html"):
        create_web_router(dist)


def test_spa_fallback_rejects_index_replaced_with_external_symlink(tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    index = dist / "index.html"
    index.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside.html"
    outside.write_text("outside secret", encoding="utf-8")
    app = FastAPI()
    app.include_router(create_web_router(dist))
    client = TestClient(app)

    assert client.get("/projects", headers={"accept": "text/html"}).text == "inside"
    index.unlink()
    index.symlink_to(outside)

    response = client.get("/projects", headers={"accept": "text/html"})
    assert response.status_code == 404
    assert "outside secret" not in response.text


def test_static_assets_reject_hidden_and_reserved_resolved_aliases(tmp_path: Path):
    (tmp_path / "index.html").write_text("inside", encoding="utf-8")
    (tmp_path / ".env").write_text("hidden secret", encoding="utf-8")
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "leak.txt").write_text("private api", encoding="utf-8")
    (tmp_path / "visible-env.txt").symlink_to(tmp_path / ".env")
    (tmp_path / "visible-api.txt").symlink_to(tmp_path / "api" / "leak.txt")
    app = FastAPI()
    app.include_router(create_web_router(tmp_path))
    client = TestClient(app)

    for path in ("/visible-env.txt", "/visible-api.txt", "/API/leak.txt"):
        response = client.get(path)
        assert response.status_code == 404
