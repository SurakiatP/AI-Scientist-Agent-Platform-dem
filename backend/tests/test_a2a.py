"""Real protocol and durable-domain checks; all work stays awaiting owner approval."""
import asyncio
import json
from pathlib import Path
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from io import BytesIO
from uuid import UUID, uuid4
import httpx
import pytest
from fastapi.testclient import TestClient
from google.protobuf.json_format import MessageToDict
from sqlalchemy import text
from a2a.types import a2a_pb2 as p
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.client import ClientConfig, ClientFactory
import uvicorn
from scientist.auth import create_token, revoke_token
from scientist.contracts import Principal
from scientist.db import create_project, create_session
from scientist import domain, objects
from a2a.utils.errors import TaskNotFoundError
from scientist.a2a_api import create_a2a_app


@pytest.fixture
def caller(db):
    owner = Principal(identity=uuid4(), kind="owner")
    project = create_project(db, "A2A research")
    session_id = create_session(db, project, "Questions")
    token = create_token(db, owner, {project: ["project:read", "work:submit", "result:read", "work:cancel"]})
    db.commit()
    return owner, project, session_id, token


def message(caller, message_id="m1", **patch):
    _, project, session_id, _ = caller
    meta = {"project_id": str(project), "session_id": str(session_id), "provider_id": str(uuid4()), "model": "fixture-model", "input_ids": []}
    meta.update(patch)
    return p.SendMessageRequest(message=p.Message(message_id=message_id, role=p.ROLE_USER, parts=[p.Part(text="Summarize diffusion evidence")]), metadata=meta)


def rpc(client, method, params):
    return client.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": MessageToDict(params)})


def client_for(token, **kwargs):
    return TestClient(create_a2a_app(**kwargs), headers={"Authorization": "Bearer " + token, "A2A-Version": "1.0"})


def test_send_persists_one_domain_run_and_requires_owner_approval(caller, db):
    with client_for(caller[3]) as client:
        response = rpc(client, "SendMessage", message(caller))
    assert response.status_code == 200
    task = response.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert task["contextId"] and task["id"]
    row = db.execute(text("SELECT state, caller_identity FROM runs WHERE id=:id"), {"id": task["id"]}).one()
    assert row.state == "awaiting_approval"
    assert db.execute(text("SELECT count(*) FROM a2a_tasks WHERE run_id=:id"), {"id": task["id"]}).scalar_one() == 1


def test_list_is_caller_scoped_and_default_pagination_works(caller):
    with client_for(caller[3]) as client:
        task = rpc(client, "SendMessage", message(caller)).json()["result"]["task"]
        listed = rpc(client, "ListTasks", p.ListTasksRequest()).json()
    assert [t["id"] for t in listed["result"]["tasks"]] == [task["id"]]


def test_invalid_session_rolls_back_without_database_details(caller, db):
    with client_for(caller[3]) as client:
        response = rpc(client, "SendMessage", message(caller, session_id=str(uuid4())))
    assert "error" in response.json()
    assert response.json()["error"]["message"] == "Task unavailable."
    caller_id = db.execute(text("SELECT id FROM access_tokens WHERE owner_identity=:owner"), {"owner": caller[0].identity}).scalar_one()
    assert db.execute(text("SELECT count(*) FROM a2a_contexts WHERE caller_id=:caller"), {"caller": caller_id}).scalar_one() == 0
    assert db.execute(text("SELECT count(*) FROM runs WHERE caller_identity=:caller"), {"caller": caller_id}).scalar_one() == 0


@pytest.fixture
def live_server():
    sockets = []
    servers = []
    threads = []
    def start(**kwargs):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        app = create_a2a_app(base_url=url, poll_interval=0.02, **kwargs)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        servers.append(server); sockets.append(sock); threads.append(thread)
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        return url, app
    yield start
    for server in servers:
        server.should_exit = True
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    for sock in sockets:
        sock.close()


def test_official_sdk_client_stream_disconnect_keeps_durable_submission(caller, db, live_server):
    url, app = live_server()
    async def scenario():
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + caller[3], "A2A-Version": "1.0"}) as http:
            sdk = ClientFactory(ClientConfig(httpx_client=http)).create(app.state.a2a_card)
            stream = sdk.send_message(message(caller))
            try:
                first = await asyncio.wait_for(anext(stream), 3)
                assert first.task.status.state == p.TASK_STATE_INPUT_REQUIRED
                return first.task.id
            finally:
                await stream.aclose()
    run_id = asyncio.run(scenario())
    assert db.execute(text("SELECT state FROM runs WHERE id=:id"), {"id": run_id}).scalar_one() == "awaiting_approval"


def test_sdk_snapshot_subscribe_get_list_and_cancel_wait_for_confirmation(caller, db, live_server):
    stopped = []
    def stop(db, run_id):
        stopped.append(run_id)
        db.execute(text("UPDATE runs SET state='stopping', cancel_requested=true, revision=revision+1 WHERE id=:id"), {"id": run_id})
        run = domain._run_view(db, run_id)
        db.commit()
        return run
    url, app = live_server(on_stop=stop)
    async def scenario():
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + caller[3], "A2A-Version": "1.0"}) as http:
            sdk = JsonRpcTransport(http, app.state.a2a_card, url + "/a2a")
            first = (await sdk.send_message(message(caller))).task
            assert (await sdk.get_task(p.GetTaskRequest(id=first.id))).status.state == p.TASK_STATE_INPUT_REQUIRED
            assert [t.id for t in (await sdk.list_tasks(p.ListTasksRequest(context_id=first.context_id))).tasks] == [first.id]
            stream = sdk.subscribe(p.SubscribeToTaskRequest(id=first.id))
            try:
                assert (await asyncio.wait_for(anext(stream), 3)).task.id == first.id
            finally:
                await stream.aclose()
            assert (await sdk.cancel_task(p.CancelTaskRequest(id=first.id))).status.state == p.TASK_STATE_WORKING
            assert stopped == [UUID(first.id)]
            db.execute(text("UPDATE runs SET state='canceled', revision=revision+1 WHERE id=:id"), {"id": first.id})
            db.commit()
            assert (await sdk.get_task(p.GetTaskRequest(id=first.id))).status.state == p.TASK_STATE_CANCELED
            # Already completed work wins a later cancellation request, without invoking stop.
            db.execute(text("UPDATE runs SET state='completed', revision=revision+1 WHERE id=:id"), {"id": first.id})
            db.commit()
            assert (await sdk.cancel_task(p.CancelTaskRequest(id=first.id))).status.state == p.TASK_STATE_COMPLETED
            assert len(stopped) == 1
    asyncio.run(scenario())


def test_revocation_closes_sdk_stream_but_keeps_owner_managed_work(caller, db, live_server):
    url, app = live_server()
    async def scenario():
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + caller[3], "A2A-Version": "1.0"}) as http:
            sdk = JsonRpcTransport(http, app.state.a2a_card, url + "/a2a")
            first = (await sdk.send_message(message(caller))).task
            stream = sdk.subscribe(p.SubscribeToTaskRequest(id=first.id))
            try:
                assert (await asyncio.wait_for(anext(stream), 3)).task.id == first.id
                token_id = db.execute(text("SELECT id FROM access_tokens WHERE owner_identity=:owner"), {"owner": caller[0].identity}).scalar_one()
                revoke_token(db, caller[0], token_id)
                db.commit()
                with pytest.raises(TaskNotFoundError):
                    await asyncio.wait_for(anext(stream), 3)
            finally:
                await stream.aclose()
            return first.id
    run_id = asyncio.run(scenario())
    assert db.execute(text("SELECT state FROM runs WHERE id=:id"), {"id": run_id}).scalar_one() == "awaiting_approval"


def test_other_caller_cannot_get_cancel_or_reuse_context_even_with_same_project(caller, db):
    with client_for(caller[3]) as client:
        task = rpc(client, "SendMessage", message(caller)).json()["result"]["task"]
    other = create_token(db, caller[0], {caller[1]: ["work:submit", "result:read", "work:cancel"]})
    db.commit()
    with client_for(other) as client:
        for method, params in [("GetTask", p.GetTaskRequest(id=task["id"])), ("CancelTask", p.CancelTaskRequest(id=task["id"])),
                               ("ListTasks", p.ListTasksRequest(context_id=task["contextId"]))]:
            assert rpc(client, method, params).json()["error"]["message"] == "Task unavailable."
        foreign = message(caller)
        foreign.message.context_id = task["contextId"]
        assert rpc(client, "SendMessage", foreign).json()["error"]["message"] == "Task unavailable."
        assert rpc(client, "ListTasks", p.ListTasksRequest()).json()["result"].get("tasks", []) == []


def test_replay_survives_application_restart_and_changed_payload_conflicts(caller, db):
    request = message(caller)
    with client_for(caller[3]) as client:
        first = rpc(client, "SendMessage", request).json()["result"]["task"]
    with client_for(caller[3]) as restarted:
        assert rpc(restarted, "SendMessage", request).json()["result"]["task"]["id"] == first["id"]
        request.message.parts[0].text = "Changed question"
        assert "error" in rpc(restarted, "SendMessage", request).json()
    assert db.execute(text("SELECT count(*) FROM a2a_messages WHERE run_id=:id"), {"id": first["id"]}).scalar_one() == 1


def test_forged_approval_and_non_text_inputs_cannot_create_work(caller, db):
    with client_for(caller[3]) as client:
        for request in [message(caller, choice="approve"), message(caller, stages=["execute"]), message(caller, retry_of=str(uuid4()))]:
            assert "error" in rpc(client, "SendMessage", request).json()
        request = message(caller)
        request.message.parts[0].url = "https://unapproved.example/data"
        assert "error" in rpc(client, "SendMessage", request).json()
    assert db.execute(text("SELECT count(*) FROM runs WHERE project_id=:project"), {"project": caller[1]}).scalar_one() == 0


def test_bearer_only_version_and_malformed_requests(caller):
    with client_for(caller[3]) as client:
        request = {"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}}
        for version in ["0.3", "1.0.1", "", "2.0"]:
            assert client.post("/a2a", json=request, headers={"A2A-Version": version}).status_code == 400
        for bad in [{"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {"role": "owner", "parts": []}}}, [request], {**request, "jsonrpc": "1.0"}]:
            assert "error" in client.post("/a2a", json=bad).json()
    with TestClient(create_a2a_app()) as client:
        client.cookies.set("owner_session", "forged")
        assert client.post("/a2a", json=request, headers={"A2A-Version": "1.0"}).status_code == 401
        assert client.post("/a2a", json=request, headers={"A2A-Version": "1.0", "Authorization": "Bearer forged"}).status_code == 403


def test_concurrent_same_message_creates_one_run_and_context(caller, db):
    request = message(caller)
    gate = threading.Barrier(4)
    def send(_):
        with client_for(caller[3]) as client:
            gate.wait(timeout=5)
            return rpc(client, "SendMessage", request).json()["result"]["task"]
    with ThreadPoolExecutor(4) as pool:
        tasks = list(pool.map(send, range(4)))
    assert len({task["id"] for task in tasks}) == len({task["contextId"] for task in tasks}) == 1
    assert db.execute(text("SELECT count(*) FROM runs WHERE project_id=:id"), {"id": caller[1]}).scalar_one() == 1
    assert db.execute(text("SELECT count(*) FROM a2a_contexts WHERE project_id=:id"), {"id": caller[1]}).scalar_one() == 1


def test_context_can_continue_only_in_original_project_and_session(caller, db):
    with client_for(caller[3]) as client:
        first = rpc(client, "SendMessage", message(caller)).json()["result"]["task"]
        next_message = message(caller, "m2")
        next_message.message.context_id = first["contextId"]
        second = rpc(client, "SendMessage", next_message).json()["result"]["task"]
        assert first["contextId"] == second["contextId"] and first["id"] != second["id"]
        session_id = create_session(db, caller[1], "Another session")
        db.commit()
        changed = message(caller, "m3", session_id=str(session_id))
        changed.message.context_id = first["contextId"]
        assert rpc(client, "SendMessage", changed).json()["error"]["message"] == "Task unavailable."
        forged = message(caller, "m4")
        forged.message.task_id = first["id"]
        assert "error" in rpc(client, "SendMessage", forged).json()


def test_card_is_product_only_and_optional_methods_are_safely_unsupported(caller):
    with client_for(caller[3]) as client:
        card = client.get("/.well-known/agent-card.json").json()
        serialized = str(card).lower()
        assert not any(name in serialized for name in ["hermes", "container", "docker", "broker", "k-dense"])
        assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"
        assert card["capabilities"]["streaming"] is True
        assert "bearer" in card["securitySchemes"]
        for method, params in [("GetExtendedAgentCard", {}), ("ListTaskPushNotificationConfigs", {"taskId": str(uuid4())})]:
            response = client.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).json()
            assert "error" in response
            assert "unavailable" in response["error"]["message"].lower()


def test_pagination_counts_only_authorized_matching_tasks(caller):
    with client_for(caller[3]) as client:
        tasks = [rpc(client, "SendMessage", message(caller, f"m{i}")).json()["result"]["task"] for i in range(3)]
        first = rpc(client, "ListTasks", p.ListTasksRequest(page_size=2)).json()["result"]
        assert first["totalSize"] == 3 and len(first["tasks"]) == 2
        last = rpc(client, "ListTasks", p.ListTasksRequest(page_size=2, page_token=first["nextPageToken"])).json()["result"]
        assert last["totalSize"] == 3 and len(last["tasks"]) == 1
        assert {t["id"] for t in first["tasks"] + last["tasks"]} == {t["id"] for t in tasks}
        working = rpc(client, "ListTasks", p.ListTasksRequest(status=p.TASK_STATE_WORKING)).json()["result"]
        assert working.get("tasks", []) == [] and working.get("totalSize", 0) == 0


@pytest.fixture
def object_store():
    class MemoryS3:
        data = {}
        def get_object(self, *, Bucket, Key):
            return {"Body": BytesIO(self.data[Key])}
    store = MemoryS3()
    previous = objects._configured_client
    objects.configure(store)
    yield store
    objects.configure(previous)


def add_artifact(db, store, caller, task_id, data=b"Verified evidence"):
    artifact_id = uuid4()
    digest = sha256(data).hexdigest()
    key = f"{caller[1]}/{digest}"
    store.data[key] = data
    db.execute(text("INSERT INTO artifacts (id, project_id, run_id, title, kind, object_key, sha256, size, content_type) VALUES (:id,:project,:run,'Evidence','report',:key,:sha,:size,'text/plain')"),
               {"id": artifact_id, "project": caller[1], "run": task_id, "key": key, "sha": digest, "size": len(data)})
    db.commit()
    return artifact_id


def test_task_results_use_verified_caller_bound_bearer_artifact_gateway(caller, db, object_store):
    with client_for(caller[3], base_url="http://testserver") as client:
        task = rpc(client, "SendMessage", message(caller)).json()["result"]["task"]
        artifact_id = add_artifact(db, object_store, caller, task["id"])
        snapshot = rpc(client, "GetTask", p.GetTaskRequest(id=task["id"])).json()["result"]
        part = snapshot["artifacts"][0]["parts"][0]
        assert part["url"] == f"http://testserver/a2a/artifacts/{artifact_id}"
        assert snapshot["artifacts"][0]["metadata"]["sha256"] == sha256(b"Verified evidence").hexdigest()
        response = client.get(part["url"])
        assert response.content == b"Verified evidence"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["x-content-type-options"] == "nosniff"
        # Integrity errors fail before any bytes, without private object keys.
        object_store.data[next(iter(object_store.data))] = b"Corrupted"
        bad = client.get(part["url"])
        assert bad.status_code == 503 and "Corrupted" not in bad.text
    other = create_token(db, caller[0], {caller[1]: ["project:read", "result:read"]})
    read_only = create_token(db, caller[0], {caller[1]: ["project:read"]})
    db.commit()
    for token in [other, read_only]:
        with client_for(token) as client:
            assert client.get(f"/a2a/artifacts/{artifact_id}").status_code == 404


def test_own_submission_without_result_grant_never_exposes_artifacts(caller, db, object_store):
    request = message(caller)
    with client_for(caller[3]) as client:
        task = rpc(client, "SendMessage", request).json()["result"]["task"]
        artifact_id = add_artifact(db, object_store, caller, task["id"])
        db.execute(text("UPDATE access_grants SET actions=ARRAY['project:read','work:submit'] WHERE token_id=(SELECT id FROM access_tokens WHERE owner_identity=:owner)"), {"owner": caller[0].identity})
        db.commit()
        replay = rpc(client, "SendMessage", request).json()["result"]["task"]
        assert "artifacts" not in replay and "metadata" not in replay
        assert "error" in rpc(client, "GetTask", p.GetTaskRequest(id=task["id"])).json()
        assert client.get(f"/a2a/artifacts/{artifact_id}").status_code == 404


def test_revocation_interrupts_verified_artifact_stream(caller, db, object_store, live_server):
    url, app = live_server()
    async def scenario():
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + caller[3], "A2A-Version": "1.0"}) as http:
            sdk = JsonRpcTransport(http, app.state.a2a_card, url + "/a2a")
            task = (await sdk.send_message(message(caller))).task
            payload = b"x" * (8 * 1024 * 1024)
            artifact_id = add_artifact(db, object_store, caller, task.id, payload)
            async with http.stream("GET", f"{url}/a2a/artifacts/{artifact_id}") as response:
                assert response.status_code == 200
                chunks = response.aiter_bytes(64 * 1024)
                received = len(await asyncio.wait_for(anext(chunks), 3))
                token_id = db.execute(text("SELECT id FROM access_tokens WHERE owner_identity=:owner"), {"owner": caller[0].identity}).scalar_one()
                revoke_token(db, caller[0], token_id)
                db.commit()
                with pytest.raises(httpx.RemoteProtocolError):
                    async for chunk in chunks:
                        received += len(chunk)
                assert received < len(payload)
    asyncio.run(scenario())


def test_stream_emits_usage_events_even_when_run_revision_does_not_change(caller, db, live_server):
    url, app = live_server()
    async def scenario():
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + caller[3], "A2A-Version": "1.0"}) as http:
            sdk = JsonRpcTransport(http, app.state.a2a_card, url + "/a2a")
            task = (await sdk.send_message(message(caller))).task
            stream = sdk.subscribe(p.SubscribeToTaskRequest(id=task.id))
            try:
                before = (await asyncio.wait_for(anext(stream), 3)).task
                db.execute(text("UPDATE runs SET usage_tokens=3, token_limit=100 WHERE id=:id"), {"id": task.id})
                revision = db.execute(text("SELECT revision FROM runs WHERE id=:id"), {"id": task.id}).scalar_one()
                domain._event(db, UUID(task.id), revision, "usage.updated", {"usage_tokens": 3, "reserved_tokens": 0, "token_limit": 100})
                db.commit()
                after = (await asyncio.wait_for(anext(stream), 3)).task
                assert before.metadata["revision"] == after.metadata["revision"]
                assert after.metadata["latest_cursor"] > before.metadata["latest_cursor"]
                assert after.metadata["usage_tokens"] == 3
            finally:
                await stream.aclose()
    asyncio.run(scenario())


def test_protocol_envelope_size_is_bounded_before_sdk_parsing(caller):
    with client_for(caller[3]) as client:
        assert client.post("/a2a", content=b"x" * (2 * 1024 * 1024 + 1), headers={"Content-Type": "application/json"}).status_code == 413


def test_profile_metadata_schema_matches_public_sdk_submission(caller):
    from jsonschema import Draft202012Validator, FormatChecker
    profile = json.loads((Path(__file__).resolve().parents[2] / "contracts/a2a-profile.json").read_text())
    validator = Draft202012Validator(profile["submission_metadata_schema"], format_checker=FormatChecker())
    metadata = MessageToDict(message(caller).metadata)
    validator.validate(metadata)
    assert list(validator.iter_errors({**metadata, "approve": True}))
    assert profile["binding"]["required_header"] == {"A2A-Version": "1.0"}


def test_slow_supervisor_stop_does_not_block_other_sdk_reads(caller, live_server):
    entered, release = threading.Event(), threading.Event()
    def stop(db, run_id):
        entered.set()
        assert release.wait(timeout=3)
        return domain._run_view(db, run_id)
    url, app = live_server(on_stop=stop)
    async def scenario():
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + caller[3], "A2A-Version": "1.0"}) as http:
            sdk = JsonRpcTransport(http, app.state.a2a_card, url + "/a2a")
            task = (await sdk.send_message(message(caller))).task
            cancel = asyncio.create_task(sdk.cancel_task(p.CancelTaskRequest(id=task.id)))
            try:
                deadline = time.monotonic() + 2
                while not entered.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                assert entered.is_set()
                snapshot = await asyncio.wait_for(sdk.get_task(p.GetTaskRequest(id=task.id)), 0.5)
                assert snapshot.id == task.id
            finally:
                release.set()
                await cancel
    asyncio.run(scenario())
