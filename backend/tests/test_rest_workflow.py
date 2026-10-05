import json
import uuid
from io import BytesIO
from secrets import token_urlsafe
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from scientist import api, files, objects
from scientist.app import create_app
from scientist.auth import DomainError, authenticate_bearer, create_token
from scientist.contracts import ObjectRef, Principal
from scientist import domain


class MemoryS3:
    def __init__(self):
        self.data = {}

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.data[(Bucket, Key)] = bytes(Body)

    def get_object(self, *, Bucket, Key):
        return {"Body": BytesIO(self.data[(Bucket, Key)])}

    def head_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.data:
            raise ClientError({"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
        return {"ContentLength": len(self.data[(Bucket, Key)])}


@pytest.fixture
def storage(monkeypatch):
    s3 = MemoryS3()
    monkeypatch.setattr(objects, "_client", lambda: s3)
    return s3


def make_client(app: FastAPI) -> TestClient:
    client = TestClient(app, client=("127.0.0.1", 12345))
    return client


@pytest.fixture
def client(db, storage):
    token = token_urlsafe(32)
    app = create_app(bootstrap_token=token)
    c = make_client(app)
    resp = c.post("/api/v1/bootstrap", headers={"host": "localhost", "origin": "http://localhost"}, json={"token": token})
    assert resp.status_code == 200
    c.headers.update({"host": "localhost", "origin": "http://localhost", "x-csrf-token": resp.json()["csrf_token"]})
    return c


def new_project(client, name="p"):
    r = client.post("/api/v1/projects", json={"name": name, "instructions": "rules"})
    assert r.status_code == 201, r.text
    return r.json()


def new_session(client, project_id):
    r = client.post(f"/api/v1/projects/{project_id}/sessions", json={"title": "s"})
    assert r.status_code == 201, r.text
    return r.json()


def ready_file(client, db, project_id, name="data.txt", body=b"hello"):
    r = client.post(f"/api/v1/projects/{project_id}/files", params={"filename": name}, content=body)
    assert r.status_code == 201, r.text
    view = r.json()
    assert view["state"] == "preparing"
    ref = objects.put(db, project_id=uuid.UUID(project_id), content=BytesIO(body), content_type="text/plain")
    files.mark_prepared(db, uuid.UUID(view["id"]), ref, "ready")
    db.commit()
    return view["id"]


def submit(client, session_id, input_ids=(), key=None):
    r = client.post(f"/api/v1/sessions/{session_id}/runs", json={
        "submission_key": key or token_urlsafe(8), "question": "What is known?", "input_ids": list(input_ids),
        "provider_id": str(uuid4()), "model": "fixture"})
    assert r.status_code == 201, r.text
    return r.json()


def test_capabilities_and_unauthenticated_denied(client):
    caps = client.get("/api/v1/capabilities").json()
    assert ".pdf" in caps["file_types"] and caps["max_upload_bytes"] == objects.MAX_UPLOAD_BYTES
    anonymous = make_client(client.app)
    assert anonymous.get("/api/v1/projects", headers={"host": "localhost"}).status_code == 401


def all_project_ids(client, limit):
    ids, after = [], None
    while True:
        params = {"limit": limit, **({"after": after} if after else {})}
        page = [p["id"] for p in client.get("/api/v1/projects", params=params).json()]
        ids += page
        if len(page) < limit:
            return ids
        after = page[-1]


def test_project_session_message_resources(client, db):
    project = new_project(client)
    full = all_project_ids(client, 200)
    assert full == sorted(set(full)) and project["id"] in full
    page = [p["id"] for p in client.get("/api/v1/projects", params={"after": project["id"], "limit": 200}).json()]
    assert page == [i for i in full if i > project["id"]][:200]
    assert all_project_ids(client, 1) == full
    got = client.get(f"/api/v1/projects/{project['id']}").json()
    assert got["revision"] == 1 and got["instructions"] == "rules"
    session = new_session(client, project["id"])
    assert [s["id"] for s in client.get(f"/api/v1/projects/{project['id']}/sessions").json()] == [session["id"]]
    db.execute(text("INSERT INTO messages (id, project_id, session_id, role, content) VALUES (:i, :p, :s, 'user', 'hi')"),
               {"i": uuid4(), "p": project["id"], "s": session["id"]})
    db.commit()
    messages = client.get(f"/api/v1/sessions/{session['id']}/messages").json()
    assert [m["content"] for m in messages] == ["hi"] and messages[0]["role"] == "user"
    assert client.get(f"/api/v1/sessions/{uuid4()}/messages").status_code == 404


def test_revision_conflicts_on_project_and_plan(client):
    project = new_project(client)
    ok = client.patch(f"/api/v1/projects/{project['id']}", json={"expected_revision": 1, "instructions": "v2"})
    assert ok.status_code == 200 and ok.json()["revision"] == 2
    stale = client.patch(f"/api/v1/projects/{project['id']}", json={"expected_revision": 1, "instructions": "v3"})
    assert stale.status_code == 409 and stale.json()["code"] == "revision_conflict"
    run = submit(client, new_session(client, project["id"])["id"])
    plan = client.get(f"/api/v1/runs/{run['run_id']}/plan").json()
    stale_plan = client.patch(f"/api/v1/runs/{run['run_id']}/plan", json={"expected_revision": 99, "plan": plan["plan"]})
    assert stale_plan.status_code == 409


def test_full_workflow_snapshot_plan_events_artifacts_publish(client, db, monkeypatch):
    monkeypatch.setenv("SCIENTIST_SCHOLARLY_ENDPOINTS", "https://api.scholar.example")
    project = new_project(client)
    pid = project["id"]
    session = new_session(client, pid)
    file_id = ready_file(client, db, pid)
    assert [f["state"] for f in client.get(f"/api/v1/projects/{pid}/files").json()] == ["ready"]
    assert client.get(f"/api/v1/projects/{pid}/files/{file_id}/content").content == b"hello"
    run = submit(client, session["id"], [file_id])
    rid = run["run_id"]
    assert run["state"] == "awaiting_approval"
    plan_view = client.get(f"/api/v1/runs/{rid}/plan").json()
    prepared = client.post(f"/api/v1/runs/{rid}/prepare-plan", json={"expected_revision": plan_view["revision"], "search_terms": ["graphene"]})
    assert prepared.status_code == 200, prepared.text
    new_plan = client.get(f"/api/v1/runs/{rid}/plan").json()
    assert new_plan["revision"] == 2 and new_plan["plan"]["stages"][0] == "Search literature: graphene"
    patched = client.patch(f"/api/v1/runs/{rid}/plan", json={"expected_revision": 2, "plan": {**new_plan["plan"], "token_limit": 1000}})
    assert patched.status_code == 200 and patched.json()["revision"] == 3
    # Immutable running input: later instruction/file changes do not alter the snapshot digest.
    digest = client.get(f"/api/v1/runs/{rid}/plan").json()["plan"]["input_snapshot_digest"]
    manifest = lambda: db.execute(text("SELECT manifest FROM input_snapshots WHERE run_id = :r"), {"r": rid}).scalar_one()
    before = manifest()
    changed = client.patch(f"/api/v1/projects/{pid}", json={"expected_revision": 1, "instructions": "changed after submit"})
    assert changed.status_code == 200
    assert client.delete(f"/api/v1/projects/{pid}/files/{file_id}").status_code == 204
    assert client.get(f"/api/v1/runs/{rid}/plan").json()["plan"]["input_snapshot_digest"] == digest
    db.rollback()
    assert manifest() == before and before["project"]["instructions"] == "rules"
    assert client.get(f"/api/v1/projects/{pid}/files").json() == []
    assert client.get(f"/api/v1/projects/{pid}/runs").json()[0]["run_id"] == rid
    assert client.get(f"/api/v1/runs/{rid}").json()["latest_cursor"] >= 2
    # Partial results retained: a completed run with a partial artifact is listed and downloadable.
    body = b"partial report"
    ref = objects.put(db, uuid.UUID(pid), BytesIO(body), "text/plain")
    art = uuid4()
    db.execute(text("INSERT INTO artifacts (id, project_id, run_id, title, kind, object_key, sha256, size, content_type, partial) VALUES (:i,:p,:r,'Report','report',:k,:h,:s,'text/markdown',true)"),
               {"i": art, "p": pid, "r": rid, "k": ref.key, "h": ref.sha256, "s": ref.size})
    db.execute(text("UPDATE runs SET state='completed' WHERE id=:r"), {"r": rid})
    db.commit()
    listed = client.get(f"/api/v1/runs/{rid}/artifacts").json()
    assert listed[0]["partial"] is True and listed[0]["artifact_id"] == str(art)
    content = client.get(f"/api/v1/artifacts/{art}/content")
    assert content.content == body and content.headers["x-content-type-options"] == "nosniff"
    assert content.headers["content-disposition"].startswith("attachment")
    revision = client.get(f"/api/v1/projects/{pid}").json()["revision"]
    assert client.post(f"/api/v1/runs/{rid}/publish", json={"object_keys": [ref.key], "expected_project_revision": revision, "publication_key": "x"}).status_code == 422
    other_run = uuid.UUID(str(domain_run(db, pid, session["id"])))
    foreign = uuid4()
    db.execute(text("INSERT INTO artifacts (id, project_id, run_id, title, kind, object_key, sha256, size, content_type) VALUES (:i,:p,:r,'F','report',:k,:h,:s,'text/plain')"),
               {"i": foreign, "p": pid, "r": other_run, "k": ref.key, "h": ref.sha256, "s": ref.size})
    db.commit()
    assert client.post(f"/api/v1/runs/{rid}/publish", json={"artifact_ids": [str(foreign)], "expected_project_revision": revision, "publication_key": "x"}).status_code == 404
    stale = client.post(f"/api/v1/runs/{rid}/publish", json={"artifact_ids": [str(art)], "expected_project_revision": revision - 1, "publication_key": "pub1"})
    assert stale.status_code == 409
    done = client.post(f"/api/v1/runs/{rid}/publish", json={"artifact_ids": [str(art)], "expected_project_revision": revision, "publication_key": "pub1"})
    assert done.status_code == 200 and len(done.json()["file_ids"]) == 1


def test_findings_sources_and_citation_provenance(client, db):
    project, other = new_project(client), new_project(client, "other")
    pid, session = project["id"], new_session(client, project["id"])
    source, citation, foreign = uuid4(), uuid4(), uuid4()
    db.execute(text("INSERT INTO sources (id, project_id, metadata) VALUES (:s,:p,'{\"retrieved\":\"fixture\"}'::jsonb)"), {"s": source, "p": pid})
    db.execute(text("INSERT INTO citations (id, project_id, source_id, title, authors, identifier, access, verification) VALUES (:c,:p,:s,'Fixture','[\"A\"]'::jsonb,'doi:10.0000/x','abstract','unverified')"),
               {"c": citation, "p": pid, "s": source})
    db.execute(text("INSERT INTO citations (id, project_id, title, authors) VALUES (:c,:p,'Other','[]'::jsonb)"), {"c": foreign, "p": other["id"]})
    db.commit()
    created = client.post(f"/api/v1/projects/{pid}/findings", json={"session_id": session["id"], "text": "finding", "citation_ids": [str(citation)]})
    assert created.status_code == 201, created.text
    assert client.post(f"/api/v1/projects/{pid}/findings", json={"session_id": session["id"], "text": "x", "citation_ids": [str(foreign)]}).status_code == 404
    assert client.get(f"/api/v1/projects/{pid}/findings").json()[0]["citation_ids"] == [str(citation)]
    srcs = client.get(f"/api/v1/projects/{pid}/sources").json()
    assert srcs[0]["id"] == str(citation) and srcs[0]["access"] == "abstract" and srcs[0]["verification"] == "unverified"
    assert client.get(f"/api/v1/citations/{citation}").json()["identifier"] == "doi:10.0000/x"
    assert client.delete(f"/api/v1/projects/{pid}/findings/{created.json()['id']}").status_code == 204
    assert client.get(f"/api/v1/projects/{pid}/findings").json() == []
    assert client.delete(f"/api/v1/projects/{pid}/findings/{uuid4()}").status_code == 404


def test_cross_project_ids_confer_no_access(client, db):
    a, b = new_project(client, "a"), new_project(client, "b")
    file_b = ready_file(client, db, b["id"])
    assert client.get(f"/api/v1/projects/{a['id']}/files/{file_b}/content").status_code == 404
    assert client.delete(f"/api/v1/projects/{a['id']}/files/{file_b}").status_code == 404
    assert client.get(f"/api/v1/projects/{b['id']}/files").json()[0]["id"] == file_b
    session_b = new_session(client, b["id"])
    r = client.post(f"/api/v1/projects/{a['id']}/findings", json={"session_id": session_b["id"], "text": "x", "citation_ids": []})
    assert r.status_code == 404
    # A project-scoped external token sees only granted projects and gets 404 for foreign object ids.
    owner = Principal(identity=uuid4(), kind="owner")
    external = authenticate_bearer(db, create_token(db, owner, {uuid.UUID(a["id"]): ["project:read", "result:read"]}))
    db.commit()
    assert [str(p.id) for p in domain.list_projects(db, external, None)] == [a["id"]]
    citation = uuid4()
    db.execute(text("INSERT INTO citations (id, project_id, title) VALUES (:c,:p,'t')"), {"c": citation, "p": b["id"]})
    db.commit()
    with pytest.raises(DomainError) as denied:
        domain.get_citation(db, external, citation)
    assert denied.value.status == 404
    run_b = domain_run(db, b["id"], session_b["id"])
    with pytest.raises(DomainError) as denied:
        domain.list_artifacts(db, external, run_b)
    assert denied.value.status == 404


def domain_run(db, project_id, session_id):
    run = domain.submit_run(db, Principal(identity=uuid.UUID(int=0), kind="owner"), uuid.UUID(project_id), uuid.UUID(session_id),
                            token_urlsafe(6), "q", [], uuid4(), "fixture")
    db.commit()
    return run.run_id


def test_upload_is_bounded_and_typed(client, monkeypatch):
    pid = new_project(client)["id"]
    monkeypatch.setattr(objects, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 10)
    assert client.post(f"/api/v1/projects/{pid}/files", params={"filename": "a.txt"}, content=b"x" * 11).status_code == 413
    assert client.post(f"/api/v1/projects/{pid}/files", params={"filename": "a.exe"}, content=b"x").status_code == 415
    assert client.post(f"/api/v1/projects/{pid}/files", params={"filename": "../a.txt"}, content=b"x").status_code == 400


def _seed_events(db, client):
    pid = new_project(client)["id"]
    rid = submit(client, new_session(client, pid)["id"])["run_id"]
    for stage in ("search", "verify"):
        domain._event(db, uuid.UUID(rid), 1, "stage.started", {"stage": stage})
    db.commit()
    return rid


def parse_sse(raw: str):
    out = []
    for block in raw.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if fields:
            out.append(fields)
    return out


def test_event_page_expired_cursor_and_sse_replay_without_duplicates(client, db):
    rid = _seed_events(db, client)
    page = client.get(f"/api/v1/runs/{rid}/event-page", params={"after": 0}).json()
    assert [e["sequence"] for e in page["events"]] == [1, 2, 3] and page["latest_cursor"] == 3
    tail = client.get(f"/api/v1/runs/{rid}/event-page", params={"after": 2}).json()
    assert [e["sequence"] for e in tail["events"]] == [3]
    expired = client.get(f"/api/v1/runs/{rid}/event-page", params={"after": 99})
    assert expired.status_code == 410 and expired.json()["code"] == "cursor_expired"
    assert expired.json()["snapshot"]["run_id"] == rid
    assert client.get(f"/api/v1/runs/{rid}/event-page", params={"after": -1}).status_code == 400
    assert client.get(f"/api/v1/runs/{rid}/events", params={"after": 99}).status_code == 410
    db.execute(text("UPDATE runs SET state='completed' WHERE id=:r"), {"r": rid})
    db.commit()
    with client.stream("GET", f"/api/v1/runs/{rid}/events", params={"after": 1}) as stream:
        assert stream.headers["content-type"].startswith("text/event-stream")
        events = parse_sse("".join(stream.iter_text()))
    assert [e["id"] for e in events] == ["2", "3"] and events[0]["event"] == "stage.started"
    assert json.loads(events[0]["data"])["payload"] == {"stage": "search"}
    with client.stream("GET", f"/api/v1/runs/{rid}/events", headers={"last-event-id": "2"}) as stream:
        assert [e["id"] for e in parse_sse("".join(stream.iter_text()))] == ["3"]


def test_sse_emits_heartbeat_then_closes_at_bound(client, db, monkeypatch):
    rid = _seed_events(db, client)
    monkeypatch.setattr(api, "SSE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(api, "SSE_HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(api, "SSE_MAX_SECONDS", 0.3)
    with client.stream("GET", f"/api/v1/runs/{rid}/events", params={"after": 3}) as stream:
        raw = "".join(stream.iter_text())
    assert ": heartbeat" in raw and "data:" not in raw


def test_control_routes_are_separate_and_nothing_is_mounted():
    paths = {r.path for r in api.router.routes}
    assert not {"/api/v1/runs/{run_id}/approve", "/api/v1/runs/{run_id}/stop", "/api/v1/runs/{run_id}/decisions"} & paths
    control = {r.path: r for r in api.control_router.routes}
    assert set(control) == {"/api/v1/runs/{run_id}/approve", "/api/v1/runs/{run_id}/stop", "/api/v1/runs/{run_id}/decisions"}


def test_chunked_upload_over_bound_is_rejected_and_leaves_no_row(client, db, monkeypatch):
    pid = new_project(client)["id"]
    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 10)
    count = lambda: db.execute(text("SELECT count(*) FROM file_versions WHERE project_id = :p"), {"p": pid}).scalar_one()
    resp = client.post(f"/api/v1/projects/{pid}/files", params={"filename": "a.txt"}, content=(b"x" * 4 for _ in range(5)))
    assert resp.status_code == 413
    db.rollback()
    assert count() == 0


def test_mutation_without_csrf_token_is_forbidden(client):
    assert client.post("/api/v1/projects", json={"name": "x"}, headers={"x-csrf-token": ""}).status_code == 403
    assert client.post("/api/v1/projects", json={"name": "x"}, headers={"x-csrf-token": "wrong"}).status_code == 403


def test_non_ascii_filename_download_header(client, db):
    pid = new_project(client)["id"]
    name = "\u7814\u7a76\u30c7\u30fc\u30bf.txt"
    fid = ready_file(client, db, pid, name=name)
    resp = client.get(f"/api/v1/projects/{pid}/files/{fid}/content")
    assert resp.status_code == 200 and resp.content == b"hello"
    disposition = resp.headers["content-disposition"]
    assert disposition.isascii() and 'filename="' in disposition and "filename*=UTF-8''%E7%A0%94" in disposition


def test_session_resolution_masks_missing_and_ungranted_for_external(db, project_session):
    project_id, session_id = project_session
    external = authenticate_bearer(db, create_token(db, Principal(identity=uuid4(), kind="owner"), {uuid4_project(db): ["project:read"]}))
    db.commit()
    for sid in (session_id, uuid4()):
        with pytest.raises(DomainError) as err:
            domain.session_project(db, external, sid, "work:submit")
        assert (err.value.status, err.value.code) == (404, "not_found")
        with pytest.raises(DomainError) as err:
            domain.list_messages(db, external, sid)
        assert err.value.status == 404


def uuid4_project(db):
    from scientist.db import create_project
    pid = create_project(db, "granted")
    db.commit()
    return pid


def test_last_event_id_and_after_use_the_larger_cursor(client, db):
    rid = _seed_events(db, client)
    db.execute(text("UPDATE runs SET state='completed' WHERE id=:r"), {"r": rid})
    db.commit()
    with client.stream("GET", f"/api/v1/runs/{rid}/events", params={"after": 1}, headers={"last-event-id": "2"}) as stream:
        assert [e["id"] for e in parse_sse("".join(stream.iter_text()))] == ["3"]
    with client.stream("GET", f"/api/v1/runs/{rid}/events", params={"after": 2}, headers={"last-event-id": "1"}) as stream:
        assert [e["id"] for e in parse_sse("".join(stream.iter_text()))] == ["3"]


def test_sse_closes_when_owner_session_is_revoked_mid_stream(client, db, monkeypatch):
    import time
    from hashlib import sha256
    rid = _seed_events(db, client)
    monkeypatch.setattr(api, "SSE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(api, "SSE_HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(api, "SSE_MAX_SECONDS", 5.0)
    token_hash = sha256(client.cookies.get("owner_session").encode()).hexdigest()
    real, calls = domain.get_events, []

    def revoking(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            from scientist.db import session as s
            with s() as other:
                other.execute(text("UPDATE owner_sessions SET revoked_at = now() WHERE token_hash = :h"), {"h": token_hash})
                other.commit()
        return real(*args, **kwargs)

    monkeypatch.setattr(domain, "get_events", revoking)
    started = time.monotonic()
    with client.stream("GET", f"/api/v1/runs/{rid}/events", params={"after": 3}) as stream:
        "".join(stream.iter_text())
    assert time.monotonic() - started < 2.5 and len(calls) < 10


def test_list_runs_orders_by_creation_then_id_and_messages_match_view(client, db):
    # W4 resource test: random run ids must not decide which run is "latest".
    project, session = new_project(client), None
    session = new_session(client, project["id"])
    created = [submit(client, session["id"])["run_id"] for _ in range(6)]
    listed = [r["run_id"] for r in client.get(f"/api/v1/projects/{project['id']}/runs").json()]
    assert listed == created
    from scientist.contracts import MessageView
    db.execute(text("INSERT INTO messages (id, project_id, session_id, role, content) VALUES (:i, :p, :s, 'user', 'hi')"),
               {"i": uuid4(), "p": project["id"], "s": session["id"]})
    db.commit()
    (message,) = client.get(f"/api/v1/sessions/{session['id']}/messages").json()
    assert MessageView.model_validate(message).run_id is None


# --- Wave 5b control routes: queued-only owner retry through the real REST host ---
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

from scientist import broker, supervisor  # noqa: E402
from test_broker import broker_fixture, request as op_request  # noqa: E402,F401
from test_owner_decisions import bind_executor, dispatch, make_unknown  # noqa: E402,F401


@pytest.fixture
def control_client(db, storage):
    token = token_urlsafe(32)
    app = create_app(bootstrap_token=token)
    c = make_client(app)
    resp = c.post("/api/v1/bootstrap", headers={"host": "localhost", "origin": "http://localhost"}, json={"token": token})
    c.headers.update({"host": "localhost", "origin": "http://localhost", "x-csrf-token": resp.json()["csrf_token"]})
    return c


def _decide(c, run_id, decision_id, revision, key="k1", choice="retry", **extra):
    return c.post(f"/api/v1/runs/{run_id}/decisions", json={
        "decision_id": str(decision_id), "expected_revision": revision, "idempotency_key": key, "choice": choice, **extra})


def _revision(db, run_id):
    return db.execute(text("SELECT revision FROM runs WHERE id=:r"), {"r": run_id}).scalar_one()


def test_rest_retry_queues_and_the_host_never_calls_the_provider(broker_fixture, dispatch, control_client):
    db, _, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id)
    assert broker._dispatch_is_inactive is None  # only supervisor.configure registered a proof
    db.execute(text("UPDATE runs SET lease_expires_at = now() + interval '1 hour' WHERE id=:r"), {"r": run_id})
    db.commit()  # lease alive: only queued_only keeps the host from dispatching in-process
    calls = transport.calls
    r = _decide(control_client, run_id, decision_id, _revision(db, run_id))
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "queued"
    assert transport.calls == calls
    row = db.execute(text("SELECT state, result FROM operations WHERE run_id=:r AND operation_id='operation-1'"), {"r": run_id}).one()
    assert row.state == "unknown" and row.result["retry_identity"] == row.result["retry_request"]["operation_id"]
    assert row.result["retry_request"]["generation"] == 1 + 1
    assert db.execute(text("SELECT reserved_tokens FROM runs WHERE id=:r"), {"r": run_id}).scalar_one() == 5
    assert db.execute(text("SELECT count(*) FROM operations WHERE run_id=:r"), {"r": run_id}).scalar_one() == 1


def test_rest_retry_negatives(broker_fixture, dispatch, control_client):
    db, _, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id, bind=False)
    rev = _revision(db, run_id)
    assert _decide(control_client, run_id, decision_id, rev).status_code == 409  # no binding row
    bind_executor(db, run_id)
    dispatch.inactive_result = False
    assert _decide(control_client, run_id, decision_id, rev).status_code == 409  # executor not inactive
    dispatch.inactive_result = True
    assert _decide(control_client, run_id, decision_id, rev + 5).status_code == 409  # stale revision
    assert _decide(control_client, run_id, uuid4(), rev).status_code == 404  # wrong decision
    assert db.execute(text("SELECT state FROM owner_decisions WHERE decision_id=:d"), {"d": decision_id}).scalar_one() == "pending"


def test_rest_two_pending_candidates_conflict(broker_fixture, dispatch, control_client):
    db, _, run_id, transport, _ = broker_fixture
    first = make_unknown(db, transport, run_id)
    db.execute(text("UPDATE runs SET state='running', waiting_reason=NULL, lease_expires_at = now() + interval '1 hour' WHERE id=:r"), {"r": run_id})
    db.commit()
    make_unknown(db, transport, run_id, operation_id="operation-2")
    r = _decide(control_client, run_id, first, _revision(db, run_id))
    assert r.status_code == 409 and r.json()["code"] == "decision_ambiguous"


def test_rest_decision_idempotency(broker_fixture, dispatch, control_client):
    db, _, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id)
    rev = _revision(db, run_id)
    first = _decide(control_client, run_id, decision_id, rev)
    second = _decide(control_client, run_id, decision_id, rev)  # double click / response-loss replay
    assert first.status_code == second.status_code == 200 and first.json() == second.json()
    conflict = _decide(control_client, run_id, decision_id, rev, choice="stop")
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
    assert db.execute(text("SELECT count(*) FROM owner_decisions WHERE run_id=:r AND state='resolved'"), {"r": run_id}).scalar_one() == 1
    assert transport.calls == 1


def test_rest_concurrent_decisions_make_one_retry(broker_fixture, dispatch, control_client):
    db, _, run_id, transport, _ = broker_fixture
    decision_id = make_unknown(db, transport, run_id)
    rev = _revision(db, run_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = [f.result(timeout=20).status_code for f in [pool.submit(_decide, control_client, run_id, decision_id, rev) for _ in range(2)]]
    assert codes == [200, 200]
    assert db.execute(text("SELECT count(*) FROM owner_decisions WHERE run_id=:r AND state='resolved'"), {"r": run_id}).scalar_one() == 1
    assert transport.calls == 1


def test_rest_stop_uses_supervisor_fencing(broker_fixture, dispatch, control_client):
    db, _, run_id, _, _ = broker_fixture
    r = control_client.post(f"/api/v1/runs/{run_id}/stop")
    assert r.status_code == 200, r.text
    assert r.json()["state"] in {"canceled", "stopping", "waiting_input"}
    assert control_client.post(f"/api/v1/runs/{uuid4()}/stop").status_code == 404


def test_rest_approve_route(control_client, db):
    pid = control_client.post("/api/v1/projects", json={"name": "p", "instructions": ""}).json()["id"]
    r = control_client.post(f"/api/v1/runs/{uuid4()}/approve", json={"expected_revision": 1, "plan_digest": "x"})
    assert r.status_code == 404
    assert control_client.post(f"/api/v1/runs/{uuid4()}/approve", json={}).status_code == 422


def test_rest_budget_extend_resumes_and_replays_without_second_extension(broker_fixture, dispatch, control_client):
    db, _, run_id, _, _ = broker_fixture
    db.execute(text("UPDATE runs SET token_limit = 10, usage_tokens = 8 WHERE id=:r"), {"r": run_id})
    db.commit()
    with pytest.raises(DomainError):
        broker.execute(db, broker.issue_capability(db, run_id, 1, 300), op_request(run_id, reserve_tokens=5))
    row = db.execute(text("SELECT budget_decision_id, revision, token_limit FROM runs WHERE id=:r"), {"r": run_id}).one()
    send = lambda: _decide(control_client, run_id, row.budget_decision_id, row.revision, key="b1", choice="extend",
                           add_tokens=100, add_elapsed_ms=0)
    first = send()
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "queued" and first.json()["token_limit"] == row.token_limit + 100
    again = send()  # response-loss replay of the same client body
    assert again.status_code == 200 and again.json() == first.json()
    assert db.execute(text("SELECT count(*) FROM run_budget_extensions WHERE run_id=:r"), {"r": run_id}).scalar_one() == 1
    assert db.execute(text("SELECT token_limit FROM runs WHERE id=:r"), {"r": run_id}).scalar_one() == row.token_limit + 100


def test_patch_plan_cannot_add_unconfigured_recipient(client, db, monkeypatch):
    monkeypatch.setenv("SCIENTIST_SCHOLARLY_ENDPOINTS", "https://api.scholar.example")
    project = new_project(client)
    session = new_session(client, project["id"])
    run = submit(client, session["id"])
    plan = client.get(f"/api/v1/runs/{run['run_id']}/plan").json()["plan"]
    bad = client.patch(f"/api/v1/runs/{run['run_id']}/plan", json={"expected_revision": 1, "plan": {**plan, "data_recipients": ["https://evil.example"]}})
    assert (bad.status_code, bad.json()["code"]) == (409, "data_destinations_not_configured")
    ok = client.patch(f"/api/v1/runs/{run['run_id']}/plan", json={"expected_revision": 1, "plan": {**plan, "data_recipients": ["https://research.example", "peer:x"]}})
    assert ok.status_code == 200


def test_create_app_mounts_resource_and_control_routes():
    paths = set(create_app(bootstrap_token=token_urlsafe(32)).openapi()["paths"])
    assert {"/api/v1/projects", "/api/v1/runs/{run_id}/events", "/api/v1/runs/{run_id}/stop", "/api/v1/runs/{run_id}/decisions"} <= paths


def test_stop_and_runtime_decisions_fail_closed_without_a_configured_supervisor(control_client, monkeypatch):
    monkeypatch.setattr(supervisor, "_config", None)
    run_id = "00000000-0000-4000-8000-000000000001"
    resp = control_client.post(f"/api/v1/runs/{run_id}/stop")
    assert (resp.status_code, resp.json()["code"]) == (503, "runtime_unavailable")
    resp = control_client.post(f"/api/v1/runs/{run_id}/decisions", json={
        "decision_id": run_id, "expected_revision": 1, "idempotency_key": "k", "choice": "stop"})
    assert (resp.status_code, resp.json()["code"]) == (503, "runtime_unavailable")
