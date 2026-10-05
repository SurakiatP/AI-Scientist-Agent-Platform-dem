from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import socket
import threading
import time
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.types import BlobResourceContents, TextResourceContents
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from sqlalchemy import text
from botocore.exceptions import ClientError

from scientist.auth import create_owner_session, create_token, revoke_token
from scientist.contracts import Principal
from scientist.db import create_project, create_session, session as database_session


@asynccontextmanager
async def running_mcp(bearer: str):
    from scientist.mcp_api import create_mcp_app

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    host, port = sock.getsockname()
    server = uvicorn.Server(uvicorn.Config(create_mcp_app(), log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() >= deadline:
            raise RuntimeError("MCP test server did not start")
        await asyncio.sleep(0.01)
    try:
        yield f"http://{host}:{port}/mcp", {"Authorization": f"Bearer {bearer}"}
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        sock.close()


@asynccontextmanager
async def sdk_session(url: str, headers: dict[str, str]):
    http = httpx.AsyncClient(follow_redirects=False, headers=headers)
    transport = streamable_http_client(url, http_client=http)
    streams = await transport.__aenter__()
    session = ClientSession(streams[0], streams[1])
    await session.__aenter__()
    await session.initialize()
    try:
        yield session
    finally:
        await session.__aexit__(None, None, None)
        await transport.__aexit__(None, None, None)
        await http.aclose()


def decode_result(result) -> dict:
    assert not result.is_error
    return json.loads(result.content[0].text)


@pytest.fixture
def mcp_fixture(db, monkeypatch):
    from scientist import objects

    class TestStore:
        def __init__(self):
            self.data = {}
            self.get_calls = 0
            self.on_get = None

        def head_object(self, *, Bucket, Key):
            if (Bucket, Key) not in self.data:
                raise ClientError({"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
            return {"ContentLength": len(self.data[(Bucket, Key)])}

        def put_object(self, *, Bucket, Key, Body, ContentType):
            self.data[(Bucket, Key)] = bytes(Body)

        def get_object(self, *, Bucket, Key):
            self.get_calls += 1
            if self.on_get is not None:
                self.on_get()
            return {"Body": BytesIO(self.data[(Bucket, Key)])}

    store = TestStore()
    monkeypatch.setattr(objects, "_client", lambda: store)
    owner = Principal(identity=UUID(int=0), kind="owner")
    project_id = create_project(db, "MCP project")
    session_id = create_session(db, project_id, "existing session")
    other_project = create_project(db, "Other project")
    other_session = create_session(db, other_project, "Other session")
    token = create_token(db, owner, {project_id: [
        "project:read", "file:attach", "work:submit", "result:read", "work:cancel",
    ]})
    token_id = db.execute(text("SELECT id FROM access_tokens WHERE token_hash = :hash"),
                          {"hash": sha256(token.encode()).hexdigest()}).scalar_one()
    owner_cookie, _ = create_owner_session(db)
    db.commit()
    return {"owner": owner, "project_id": project_id, "session_id": session_id,
            "other_project": other_project, "other_session": other_session,
            "token": token, "token_id": token_id, "owner_cookie": owner_cookie, "store": store}


def test_official_sdk_exact_post_route_schemas_and_project_scope(mcp_fixture):
    async def run():
        async with running_mcp(mcp_fixture["token"]) as (url, headers):
            async with httpx.AsyncClient(follow_redirects=False) as anonymous:
                response = await anonymous.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "probe", "version": "1"}}})
                assert response.status_code == 401
                assert response.headers.get("location") is None
                owner_cookie = await anonymous.post(url, headers={"Cookie": f"owner_session={mcp_fixture['owner_cookie']}"}, json={"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "owner-cookie", "version": "1"}}})
                assert owner_cookie.status_code == 401
                get_response = await anonymous.get(url, headers=headers)
                assert get_response.status_code == 405
                assert get_response.headers.get("location") is None
            async with sdk_session(url, headers) as client:
                tools = await client.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "list_projects", "attach_input", "submit_research", "get_research",
                    "get_research_events", "list_results", "request_stop",
                }
                schemas = {tool.name: tool.input_schema for tool in tools.tools}
                contract = json.loads(Path("contracts/mcp-tools.json").read_text())
                expected = {item["name"]: item["inputSchema"] for item in contract["tools"]}
                descriptions = {item["name"]: item["description"] for item in contract["tools"]}
                assert set(schemas) == set(expected)
                assert {tool.name: tool.description for tool in tools.tools} == descriptions
                assert schemas["submit_research"]["required"] == [
                    "project_id", "session_id", "submission_key", "question", "input_ids", "provider_id", "model",
                ]
                for name, schema in schemas.items():
                    assert "principal" not in schema["properties"]
                    assert "db" not in schema["properties"]
                    assert set(schema["properties"]) == set(expected[name]["properties"])
                    assert schema.get("required", []) == expected[name].get("required", [])
                    for key, property_schema in schema["properties"].items():
                        actual_type = property_schema.get("type")
                        if actual_type is None:
                            actual_type = [item["type"] for item in property_schema["anyOf"]]
                        declared_type = expected[name]["properties"][key]["type"]
                        assert sorted(actual_type) == sorted(declared_type) if isinstance(actual_type, list) else actual_type == declared_type
                        if "default" in expected[name]["properties"][key]:
                            assert property_schema.get("default") == expected[name]["properties"][key]["default"]
                try:
                    injected = await client.call_tool("list_projects", {
                        "principal": {"identity": "00000000-0000-0000-0000-000000000000", "kind": "owner"},
                        "db": "caller-controlled",
                    })
                    page = decode_result(injected)
                except MCPError:
                    page = decode_result(await client.call_tool("list_projects", {}))
                assert [item["id"] for item in page["projects"]] == [str(mcp_fixture["project_id"])]
    asyncio.run(run())


def test_submission_replay_and_client_disconnect_leave_durable_awaiting_approval(mcp_fixture):
    async def run():
        args = {"project_id": str(mcp_fixture["project_id"]), "session_id": str(mcp_fixture["session_id"]),
                "submission_key": "stable-mcp-key", "question": "Compare the supplied evidence.",
                "input_ids": [], "provider_id": "00000000-0000-0000-0000-000000000777", "model": "test-model"}
        async with running_mcp(mcp_fixture["token"]) as (url, headers):
            async with sdk_session(url, headers) as client:
                first = decode_result(await client.call_tool("submit_research", args))
                second = decode_result(await client.call_tool("submit_research", args))
                assert first["run_id"] == second["run_id"]
                assert first["state"] == "awaiting_approval"
                with pytest.raises(MCPError) as conflict:
                    await client.call_tool("submit_research", {**args, "question": "Different content"})
                assert conflict.value.message == "idempotency_conflict"
        from scientist.domain import get_run
        with database_session() as db:
            view = get_run(db, mcp_fixture["owner"], UUID(first["run_id"]))
            assert view.state == "awaiting_approval"
            assert view.reserved_tokens == 0
            assert db.execute(text("SELECT count(*) FROM operations WHERE run_id = :run"),
                              {"run": view.run_id}).scalar_one() == 0
    asyncio.run(run())


def test_cross_project_resource_denial_and_revocation_are_checked_per_request(mcp_fixture):
    artifact_id, allowed_artifact_id = uuid4(), uuid4()
    with database_session() as db:
        db.execute(text("""INSERT INTO artifacts
            (id, project_id, title, kind, object_key, sha256, size, content_type)
            VALUES (:id, :project, :title, 'report', :key, :sha, 7, 'text/markdown')"""), [
                {"id": artifact_id, "project": mcp_fixture["other_project"], "title": "private", "key": "private/key", "sha": "a" * 64},
                {"id": allowed_artifact_id, "project": mcp_fixture["project_id"], "title": "granted", "key": "granted/key", "sha": "b" * 64},
            ])
        db.commit()

    async def run():
        async with running_mcp(mcp_fixture["token"]) as (url, headers):
            async with sdk_session(url, headers) as client:
                resources = await client.list_resource_templates()
                templates = [item.uri_template for item in resources.resource_templates]
                assert "scientist://artifacts/{artifact_id}" in [str(item) for item in templates]
                declared_resources = json.loads(Path("contracts/mcp-tools.json").read_text())["resources"]
                assert {resource["uriTemplate"] for resource in declared_resources} == {
                    "scientist://artifacts/{artifact_id}",
                    "scientist://artifacts/{artifact_id}/content",
                }
                declared_resource = declared_resources[0]
                template = next(item for item in resources.resource_templates
                                if str(item.uri_template) == declared_resource["uriTemplate"])
                assert template.description == declared_resource["description"]
                content_resource = declared_resources[1]
                content_template = next(item for item in resources.resource_templates
                                        if str(item.uri_template) == content_resource["uriTemplate"])
                assert content_template.description == content_resource["description"]
                assert content_template.mime_type == content_resource["mimeType"]
                resource = await client.read_resource(f"scientist://artifacts/{allowed_artifact_id}")
                metadata = json.loads(resource.contents[0].text)
                assert metadata["artifact_id"] == str(allowed_artifact_id)
                assert "object_key" not in metadata
                with pytest.raises(MCPError):
                    await client.read_resource(f"scientist://artifacts/{artifact_id}")
                with database_session() as db:
                    revoke_token(db, mcp_fixture["owner"], mcp_fixture["token_id"])
                    db.commit()
                with pytest.raises(MCPError):
                    await client.list_tools()
    asyncio.run(run())


def test_attach_parser_and_run_status_tools_use_domain_authority(mcp_fixture):
    async def run():
        import base64

        args = {"project_id": str(mcp_fixture["project_id"]), "session_id": str(mcp_fixture["session_id"]),
                "submission_key": "status-tools", "question": "Check status behavior.", "input_ids": [],
                "provider_id": "00000000-0000-0000-0000-000000000777", "model": "test-model"}
        async with running_mcp(mcp_fixture["token"]) as (url, headers):
            async with sdk_session(url, headers) as client:
                uploaded = decode_result(await client.call_tool("attach_input", {
                    "project_id": str(mcp_fixture["project_id"]), "filename": "notes.txt",
                    "declared_size": 5, "content_type": "text/plain", "content_base64": base64.b64encode(b"hello").decode(),
                }))
                assert uploaded["state"] == "preparing"
                assert uploaded["size"] == 5
                with pytest.raises(MCPError) as too_large:
                    await client.call_tool("attach_input", {
                        "project_id": str(mcp_fixture["project_id"]), "filename": "notes.txt",
                        "declared_size": 26_214_401, "content_type": "text/plain", "content_base64": "",
                    })
                assert too_large.value.message == "request_too_large"
                submitted = decode_result(await client.call_tool("submit_research", args))
                run_id = submitted["run_id"]
                view = decode_result(await client.call_tool("get_research", {"run_id": run_id}))
                assert view["state"] == "awaiting_approval"
                events = decode_result(await client.call_tool("get_research_events", {"run_id": run_id, "after": 0, "limit": 20}))
                assert events["cursor_expired"] is False
                assert events["events"]
                results = decode_result(await client.call_tool("list_results", {"run_id": run_id}))
                assert results["results"] == []
                stopped = decode_result(await client.call_tool("request_stop", {"run_id": run_id}))
                assert stopped["state"] == "stopping"
    asyncio.run(run())



def _register_artifact(mcp_fixture, *, project_id=None, content=b"# verified report\n"):
    from scientist import objects

    project_id = project_id or mcp_fixture["project_id"]
    artifact_id = uuid4()
    with database_session() as db:
        ref = objects.put(db, project_id, BytesIO(content), "text/markdown")
        db.execute(text("""INSERT INTO artifacts
            (id, project_id, title, kind, object_key, sha256, size, content_type)
            VALUES (:id, :project, 'Verified report', 'report', :key, :sha, :size, 'text/markdown')"""), {
                "id": artifact_id, "project": project_id, "key": ref.key,
                "sha": ref.sha256, "size": ref.size,
            })
        db.commit()
    return artifact_id, ref, content


def test_artifact_content_resource_returns_verified_bytes_with_compatible_metadata(mcp_fixture):
    artifact_id, ref, content = _register_artifact(mcp_fixture)
    other_artifact_id, _, _ = _register_artifact(
        mcp_fixture, project_id=mcp_fixture["other_project"]
    )

    async def run():
        async with running_mcp(mcp_fixture["token"]) as (url, headers):
            async with sdk_session(url, headers) as client:
                metadata = await client.read_resource(f"scientist://artifacts/{artifact_id}")
                assert isinstance(metadata.contents[0], TextResourceContents)
                metadata_view = json.loads(metadata.contents[0].text)
                assert metadata_view == {
                    "artifact_id": str(artifact_id),
                    "project_id": str(mcp_fixture["project_id"]),
                    "sha256": ref.sha256,
                    "size": len(content),
                    "content_type": "text/markdown",
                }
                result = await client.read_resource(f"scientist://artifacts/{artifact_id}/content")
                assert isinstance(result.contents[0], BlobResourceContents)
                assert result.contents[0].mime_type == "application/octet-stream"
                assert base64.b64decode(result.contents[0].blob) == content
                with pytest.raises(MCPError) as error:
                    await client.read_resource(
                        f"scientist://artifacts/{other_artifact_id}/content"
                    )
                assert error.value.message == "not_found"

    asyncio.run(run())



def test_artifact_content_requires_exact_project_result_read_grant(mcp_fixture):
    artifact_id, _, _ = _register_artifact(mcp_fixture)
    with database_session() as db:
        token = create_token(db, mcp_fixture["owner"], {mcp_fixture["project_id"]: ["project:read"]})
        db.commit()

    async def run():
        async with running_mcp(token) as (url, headers):
            async with sdk_session(url, headers) as client:
                with pytest.raises(MCPError) as error:
                    await client.read_resource(f"scientist://artifacts/{artifact_id}/content")
                assert error.value.message == "not_found"

    asyncio.run(run())


def test_artifact_content_revalidates_revocation_on_each_resource_read(mcp_fixture):
    artifact_id, _, _ = _register_artifact(mcp_fixture)

    async def run():
        async with running_mcp(mcp_fixture["token"]) as (url, headers):
            async with sdk_session(url, headers) as client:
                await client.read_resource(f"scientist://artifacts/{artifact_id}/content")
                with database_session() as db:
                    revoke_token(db, mcp_fixture["owner"], mcp_fixture["token_id"])
                    db.commit()
                with pytest.raises(MCPError):
                    await client.read_resource(f"scientist://artifacts/{artifact_id}/content")

    asyncio.run(run())


def test_artifact_content_fails_closed_when_stored_hash_does_not_match(mcp_fixture):
    from scientist import objects

    artifact_id, ref, content = _register_artifact(mcp_fixture)
    mcp_fixture["store"].data[(objects.BUCKET, ref.key)] = content + b"tampered"

    async def run():
        async with running_mcp(mcp_fixture["token"]) as (url, headers):
            async with sdk_session(url, headers) as client:
                with pytest.raises(MCPError) as error:
                    await client.read_resource(f"scientist://artifacts/{artifact_id}/content")
                assert error.value.message == "storage_integrity"

    asyncio.run(run())


def test_artifact_content_size_cap_blocks_storage_read(mcp_fixture):
    from scientist import objects

    artifact_id = uuid4()
    digest = "a" * 64
    with database_session() as db:
        db.execute(text("""INSERT INTO artifacts
            (id, project_id, title, kind, object_key, sha256, size, content_type)
            VALUES (:id, :project, 'Oversized report', 'report', :key, :sha, :size, 'text/markdown')"""), {
                "id": artifact_id, "project": mcp_fixture["project_id"],
                "key": f"{mcp_fixture['project_id']}/{digest}", "sha": digest,
                "size": objects.MAX_UPLOAD_BYTES + 1,
            })
        db.commit()
    before = mcp_fixture["store"].get_calls

    async def run():
        async with running_mcp(mcp_fixture["token"]) as (url, headers):
            async with sdk_session(url, headers) as client:
                with pytest.raises(MCPError) as error:
                    await client.read_resource(f"scientist://artifacts/{artifact_id}/content")
                assert error.value.message == "resource_too_large"

    asyncio.run(run())
    assert mcp_fixture["store"].get_calls == before



def test_artifact_content_rechecks_grant_after_object_read_before_delivery(mcp_fixture):
    artifact_id, _, _ = _register_artifact(mcp_fixture)

    def revoke_during_object_read():
        with database_session() as db:
            revoke_token(db, mcp_fixture["owner"], mcp_fixture["token_id"])
            db.commit()

    mcp_fixture["store"].on_get = revoke_during_object_read

    async def run():
        async with running_mcp(mcp_fixture["token"]) as (url, headers):
            async with sdk_session(url, headers) as client:
                with pytest.raises(MCPError) as error:
                    await client.read_resource(f"scientist://artifacts/{artifact_id}/content")
                assert error.value.message == "not_found"

    asyncio.run(run())
