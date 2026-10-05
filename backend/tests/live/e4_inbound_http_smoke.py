#!/usr/bin/env python3
"""Narrow composed-listener smoke with real loopback HTTP, PostgreSQL and S3.

This is not aggregate E4 acceptance: no peer TLS/container recovery, native
scientific execution or host stop is certified. All submitted work stays behind
owner approval. Config/credentials/evidence must be operator-provided local files.
No credentials, headers, payloads or exception messages enter the proof.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

CONTEXT = "colima-scientist-platform-test"
ENGINE = "e3285329-4f64-4c8c-a566-40a098af03da"
ROOT = Path(__file__).resolve().parents[3]
KEYS = {"database_url_file", "s3_access_key_file", "s3_secret_key_file", "s3_endpoint",
        "bucket", "postgres_container_id", "minio_container_id", "image_verification"}


class NotRun(Exception):
    pass


class CheckFailure(Exception):
    pass


def need(value, reason):
    if not value:
        raise CheckFailure(reason)


def private_file(path):
    path = Path(path).resolve()
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise NotRun("private_file_unavailable")
    if path.is_relative_to(ROOT) and not path.is_relative_to(ROOT / ".local"):
        raise NotRun("private_file_location")
    return path


def load_config(path):
    path = private_file(path)
    try:
        cfg = json.loads(path.read_text())
    except (OSError, ValueError):
        raise NotRun("configuration_unreadable") from None
    if not isinstance(cfg, dict) or set(cfg) != KEYS:
        raise NotRun("configuration_keys")
    for key in ("database_url_file", "s3_access_key_file", "s3_secret_key_file"):
        cfg[key] = private_file(cfg[key])
    endpoint = urlsplit(cfg["s3_endpoint"])
    if (endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or endpoint.port != 54332
            or endpoint.username or endpoint.password or endpoint.path not in {"", "/"}
            or endpoint.query or endpoint.fragment):
        raise NotRun("storage_endpoint")
    if not isinstance(cfg["bucket"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,62}", cfg["bucket"]):
        raise NotRun("storage_bucket")
    for key in ("postgres_container_id", "minio_container_id"):
        if not isinstance(cfg[key], str) or not re.fullmatch(r"[a-f0-9]{64}", cfg[key]):
            raise NotRun("service_identity")
    cfg["image_verification"] = private_file(cfg["image_verification"])
    return cfg


def preflight(cfg):
    def docker(*args):
        return subprocess.check_output(["rtk", "proxy", "docker", "--context", CONTEXT, *args],
                                       stderr=subprocess.DEVNULL, timeout=15)
    need(docker("info", "--format", "{{.ID}}").decode().strip() == ENGINE, "engine_identity")
    for key in ("postgres_container_id", "minio_container_id"):
        info = json.loads(docker("inspect", cfg[key]))[0]
        need(info["Id"] == cfg[key] and info["State"]["Running"], "service_not_running")
        need(any(m["Type"] == "volume" for m in info["Mounts"]), "service_not_persistent")
    record = json.loads(cfg["image_verification"].read_text())
    need(record.get("status") == "PASS" and record.get("engine_id") == ENGINE, "image_gate")
    source_hashes = json.loads(cfg["image_verification"].with_name("server-image-source-hashes.json").read_text())
    need(source_hashes and all((ROOT / name).is_file()
         and sha256((ROOT / name).read_bytes()).hexdigest() == digest
         for name, digest in source_hashes.items()), "current_source_image_match")
    for role in ("server", "worker"):
        result = record["results"][role]
        need(result["source_match"] and result["exact_ancestor_layers"]
             and result["scan_high_critical"] == 0, "image_gate")
        need(json.loads(docker("image", "inspect", result["image"]))[0]["Id"]
             == result["image"].split("@", 1)[1], "image_identity")
    from sqlalchemy.engine import make_url
    raw = cfg["database_url_file"].read_text().strip()
    url = make_url(raw)
    need(url.drivername == "postgresql+psycopg" and url.host == "127.0.0.1" and url.port == 54331
         and url.username and url.password and not url.query
         and re.fullmatch(r"scientist_test_e4[a-z0-9]+", url.database or ""), "isolated_database")
    # Keep credentials in memory only. No subprocess, logs or proof receive raw.
    os.environ["SCIENTIST_DATABASE_URL"] = raw
    return record


@asynccontextmanager
async def listener():
    import uvicorn
    from scientist.protocol_app import create_protocol_app
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(create_protocol_app(public_base_url=base),
                            log_level="critical", access_log=False, lifespan="on",
                            timeout_graceful_shutdown=2))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    try:
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            need(thread.is_alive() and time.monotonic() < deadline, "listener_start")
            await asyncio.sleep(0.01)
        yield base
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        if thread.is_alive():
            server.force_exit = True
            await asyncio.to_thread(thread.join, 5)
        sock.close()
        need(not thread.is_alive(), "listener_cleanup")


async def scenario(cfg, proof):
    # This standalone evidence process logs only the safe proof below. SDK INFO
    # logs otherwise include transport session IDs, even with ASGI access off.
    logging.disable(logging.CRITICAL)
    provider = uuid4()
    os.environ["SCIENTIST_PROVIDER_DESTINATIONS"] = json.dumps({str(provider): "https://synthetic.invalid"})
    import boto3
    import httpx
    from botocore.config import Config
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from a2a.client.transports.jsonrpc import JsonRpcTransport
    from a2a.types import a2a_pb2 as p
    from google.protobuf.json_format import ParseDict
    from sqlalchemy import text
    from scientist import objects
    from scientist.auth import create_token, revoke_token
    from scientist.contracts import FileView, ObjectRef, Principal
    from scientist.db import create_project, create_session, migrate, session

    migrate()
    store = boto3.client("s3", endpoint_url=cfg["s3_endpoint"], region_name="us-east-1",
        aws_access_key_id=cfg["s3_access_key_file"].read_text().strip(),
        aws_secret_access_key=cfg["s3_secret_key_file"].read_text().strip(),
        config=Config(signature_version="s3v4", connect_timeout=3, read_timeout=3,
                      retries={"total_max_attempts": 1}, s3={"addressing_style": "path"}))
    store.head_bucket(Bucket=cfg["bucket"])
    objects.configure(store, cfg["bucket"])
    owner = Principal(identity=uuid4(), kind="owner")
    with session() as db:
        need(db.execute(text("SELECT count(*) FROM projects")).scalar_one() == 0, "database_not_fresh")
        project = create_project(db, "E4 synthetic inbound HTTP")
        conversation = create_session(db, project, "Synthetic approval boundary")
        foreign = create_project(db, "E4 foreign scope")
        actions = ["project:read", "file:attach", "work:submit", "work:cancel", "result:read"]
        token = create_token(db, owner, {project: actions})
        foreign_token = create_token(db, owner, {foreign: actions})
        token_id = db.execute(text("SELECT id FROM access_tokens WHERE token_hash=:hash"),
                              {"hash": sha256(token.encode()).hexdigest()}).scalar_one()
        db.commit()
    proof.update(project_id=str(project), session_id=str(conversation), checks=[], paid_calls=0)
    def check(name):
        proof["checks"].append(name)
    def decoded(result):
        need(not result.is_error, "mcp_tool_error")
        return json.loads(result.content[0].text)
    async with listener() as base:
        async with httpx.AsyncClient(base_url=base, follow_redirects=False, trust_env=False, timeout=10) as http:
            for path in ("/api/v1/projects", "/api/v1/connections", "/api/v1/bootstrap", "/api/v1/settings", "/docs", "/openapi.json"):
                need((await http.get(path)).status_code == 404, "owner_listener_isolation")
            need((await http.post("/mcp", json={})).status_code == 401, "anonymous_mcp")
            need((await http.post("/mcp", json={}, headers={"Cookie": "owner_session=synthetic"})).status_code == 401,
                 "cookie_not_bearer")
            need((await http.get("/.well-known/agent-card.json", headers={"Host": "foreign.invalid"})).status_code == 403,
                 "host_guard")
            need((await http.post("/mcp", json={}, headers={"Origin": "https://foreign.invalid"})).status_code == 403,
                 "origin_guard")
            check("owner_listener_and_transport_guards")
            card = ParseDict((await http.get("/.well-known/agent-card.json")).json(), p.AgentCard())
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, follow_redirects=False,
                                     trust_env=False, timeout=10) as http:
            async with streamable_http_client(base + "/mcp", http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as sdk:
                    await sdk.initialize()
                    names = {tool.name for tool in (await sdk.list_tools()).tools}
                    need(names == {"list_projects", "attach_input", "submit_research", "get_research",
                                   "get_research_events", "list_results", "request_stop"}, "mcp_tools")
                    projects = decoded(await sdk.call_tool("list_projects", {}))
                    need([item["id"] for item in projects["projects"]] == [str(project)], "project_scope")
                    uploaded = FileView.model_validate(decoded(await sdk.call_tool("attach_input", {
                        "project_id": str(project), "filename": "synthetic.txt", "declared_size": 5,
                        "content_type": "text/plain", "content_base64": base64.b64encode(b"hello").decode()})))
                    proof["file_id"] = str(uploaded.id)
                    need(uploaded.size == 5 and uploaded.state == "preparing", "real_upload")
                    args = {"project_id": str(project), "session_id": str(conversation), "submission_key": "mcp-once",
                            "question": "Review synthetic evidence", "input_ids": [], "provider_id": str(provider), "model": "synthetic-no-calls"}
                    submitted = decoded(await sdk.call_tool("submit_research", args))
                    proof["mcp_run_id"] = submitted["run_id"]
                    replay = decoded(await sdk.call_tool("submit_research", args))
                    need(submitted["run_id"] == replay["run_id"], "mcp_replay")
                    run_id = submitted["run_id"]
                    view = decoded(await sdk.call_tool("get_research", {"run_id": run_id}))
                    need(view["state"] == "awaiting_approval", "owner_approval_barrier")
                    events = decoded(await sdk.call_tool("get_research_events", {"run_id": run_id, "after": 0, "limit": 20}))
                    need(events["events"] and not events["cursor_expired"], "mcp_events")
                    need(decoded(await sdk.call_tool("list_results", {"run_id": run_id}))["results"] == [], "mcp_results")
                    need(decoded(await sdk.call_tool("request_stop", {"run_id": run_id}))["state"] == "stopping", "mcp_stop_request")
                    proof["mcp_run_id"] = run_id
                    check("official_mcp_all_seven_tools_and_durable_replay")
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token, "A2A-Version": "1.0"},
                                     trust_env=False, follow_redirects=False, timeout=10) as http:
            sdk = JsonRpcTransport(http, card, base + "/a2a")
            request = p.SendMessageRequest(message=p.Message(message_id="a2a-once", role=p.ROLE_USER,
                parts=[p.Part(text="Review only this synthetic evidence")]), metadata={
                "project_id": str(project), "session_id": str(conversation), "provider_id": str(provider),
                "model": "synthetic-no-calls", "input_ids": []})
            task = (await sdk.send_message(request)).task
            proof["a2a_run_id"] = task.id
            need((await sdk.send_message(request)).task.id == task.id, "a2a_replay")
            need((await sdk.get_task(p.GetTaskRequest(id=task.id))).status.state == p.TASK_STATE_INPUT_REQUIRED, "a2a_get")
            need([item.id for item in (await sdk.list_tasks(p.ListTasksRequest(context_id=task.context_id))).tasks] == [task.id], "a2a_list")
            stream = sdk.subscribe(p.SubscribeToTaskRequest(id=task.id))
            try:
                need((await asyncio.wait_for(anext(stream), 5)).task.id == task.id, "a2a_snapshot")
            finally:
                await stream.aclose()
            proof["a2a_run_id"] = task.id
            check("official_a2a_message_get_list_sse_disconnect")
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + foreign_token, "A2A-Version": "1.0"},
                                     trust_env=False, timeout=10) as http:
            response = await http.post(base + "/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": task.id}})
            need("error" in response.json(), "foreign_task_scope")
        with session() as db:
            row = db.execute(text("""SELECT s.project_id,s.key,s.sha256,s.size,s.content_type FROM stored_objects s
                JOIN file_versions f ON f.object_key=s.key AND f.project_id=s.project_id WHERE f.id=:file"""),
                {"file": uploaded.id}).mappings().one()
            reference = ObjectRef.model_validate(dict(row))
            with objects.open_verified(reference) as content:
                need(content.read() == b"hello", "independent_s3_readback")
            need(db.execute(text("SELECT count(*) FROM operations")).scalar_one() == 0, "no_provider_effects")
            need(db.execute(text("SELECT state FROM runs WHERE id=:id"), {"id": task.id}).scalar_one() == "awaiting_approval", "durable_disconnect")
            revoke_token(db, owner, token_id)
            db.commit()
            proof["object_sha256"] = reference.sha256
            proof["object_size"] = reference.size
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False, timeout=10) as http:
            need((await http.post(base + "/mcp", json={})).status_code == 401, "revoked_mcp")
            response = await http.post(base + "/a2a", headers={"A2A-Version": "1.0"}, json={"jsonrpc": "2.0", "id": 2, "method": "GetTask", "params": {"id": task.id}})
            # BearerContext distinguishes absent auth (401) from unavailable
            # credentials (403); match the existing A2A transport contract.
            need(response.status_code == 403 and "result" not in response.json(), "revoked_a2a")
        check("scope_revocation_real_pg_s3_and_zero_operations")
    return proof


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    result = {"status": "FAIL", "gate": "E4-INBOUND-HTTP-SMOKE", "aggregate_e4": "NOT RUN",
              "harness_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}
    evidence_created = False
    try:
        cfg = load_config(args.config)
        evidence = args.evidence.resolve()
        if (evidence.exists() or (evidence.is_relative_to(ROOT) and not evidence.is_relative_to(ROOT / ".local"))):
            raise NotRun("evidence_location_or_reuse")
        evidence.mkdir(mode=0o700, parents=True)
        evidence_created = True
        record = preflight(cfg)
        asyncio.run(asyncio.wait_for(scenario(cfg, result), 90))
        result.update(status="PASS", source_image_head=record["source_head"], engine_id=ENGINE,
                      scope="current-source composed HTTP/SDK with actual PG/S3; no host or peer TLS acceptance")
        code = 0
    except NotRun as exc:
        result.update(status="NOT RUN", reason=str(exc))
        code = 77
    except Exception as exc:
        result["failure_class"] = type(exc).__name__
        frames = []
        tb = exc.__traceback__
        while tb is not None:
            frames.append({"file": Path(tb.tb_frame.f_code.co_filename).name, "line": tb.tb_lineno})
            tb = tb.tb_next
        result["failure_frames"] = frames[-6:]
        def leaves(error):
            children = getattr(error, "exceptions", ())
            if children:
                return [item for child in children for item in leaves(child)]
            frames, trace = [], error.__traceback__
            while trace is not None:
                frames.append({"file": Path(trace.tb_frame.f_code.co_filename).name, "line": trace.tb_lineno})
                trace = trace.tb_next
            item = {"class": type(error).__name__, "frames": frames[-4:]}
            if isinstance(error, CheckFailure) and re.fullmatch(r"[a-z0-9_]{1,64}", str(error)):
                item["check"] = str(error)
            return [item]
        result["failure_leaves"] = leaves(exc)
        code = 1
    if evidence_created:
        proof_file = evidence / "proof.json"
        # Reused directories are never overwritten, even on preflight failure.
        if not proof_file.exists():
            proof_file.write_text(json.dumps(result, indent=2) + "\n")
            proof_file.chmod(0o600)
    print(json.dumps(result))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
