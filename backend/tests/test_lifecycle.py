from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from threading import Event

import pytest
from sqlalchemy import event, text
from scientist.auth import DomainError, create_token, authenticate_bearer
from scientist.contracts import PlanSpec, Principal, RunEvent
from scientist.domain import approve_run, get_events, get_plan, get_run, request_stop, revise_plan, submit_run
from scientist.db import create_project, create_session, engine as db_engine, session as database_session


def test_submission_is_idempotent_and_changed_body_conflicts(db, project_session):
    project_id, session_id = project_session
    owner = Principal(identity=uuid4(), kind="owner")
    provider_id = uuid4()
    first = submit_run(db, owner, project_id, session_id, "same-key", "question", [], provider_id, "fixture")
    replay = submit_run(db, owner, project_id, session_id, "same-key", "question", [], provider_id, "fixture")
    assert replay.run_id == first.run_id
    with pytest.raises(DomainError, match="idempotency_conflict"):
        submit_run(db, owner, project_id, session_id, "same-key", "changed", [], provider_id, "fixture")


def test_concurrent_submission_replay_creates_one_run(db, project_session):
    project_id, session_id = project_session
    db.commit()
    owner = Principal(identity=uuid4(), kind="owner")
    provider_id = uuid4()

    def submit_once(_):
        with database_session() as worker_db:
            result = submit_run(worker_db, owner, project_id, session_id, "concurrent-key", "same question", [], provider_id, "fixture")
            worker_db.commit()
            return result.run_id

    with ThreadPoolExecutor(max_workers=2) as workers:
        run_ids = list(workers.map(submit_once, range(2)))
    assert run_ids[0] == run_ids[1]


def test_external_submission_is_non_executing_and_cannot_approve(db, project_session):
    project_id, session_id = project_session
    owner = Principal(identity=uuid4(), kind="owner")
    token = create_token(db, owner, {project_id: ["work:submit"]})
    external = authenticate_bearer(db, token)
    provider_id = uuid4()
    run = submit_run(db, external, project_id, session_id, "external", "question", [], provider_id, "fixture")
    assert run.state == "awaiting_approval"
    with pytest.raises(DomainError, match="forbidden"):
        approve_run(db, external, run.run_id, run.revision, run.plan_digest)


def test_plan_revisions_are_immutable_and_approval_requires_current_digest(db, project_session):
    project_id, session_id = project_session
    owner = Principal(identity=uuid4(), kind="owner")
    run = submit_run(db, owner, project_id, session_id, "plan", "question", [], uuid4(), "fixture")
    snapshot_digest = get_plan(db, owner, run.run_id).plan.input_snapshot_digest
    first_plan = minimal_plan(snapshot_digest, uuid4())
    revised = revise_plan(db, owner, run.run_id, run.revision, first_plan)
    changed_plan = minimal_plan(snapshot_digest, uuid4(), token_limit=88, elapsed_limit_ms=1234)
    current = revise_plan(db, owner, run.run_id, revised.revision, changed_plan)
    db.execute(text("UPDATE runs SET usage_tokens = 11, reserved_tokens = 4 WHERE id = :run"), {"run": run.run_id})
    with pytest.raises(DomainError, match="revision_conflict"):
        approve_run(db, owner, run.run_id, revised.revision, revised.plan_digest)
    approved = approve_run(db, owner, run.run_id, current.revision, current.plan_digest)
    assert approved.state == "queued"
    assert approved.token_limit == 88
    assert db.execute(text("SELECT elapsed_limit_ms, usage_tokens, reserved_tokens FROM runs WHERE id = :run"), {"run": run.run_id}).one() == (1234, 11, 4)
    with pytest.raises(DomainError, match="revision_conflict"):
        revise_plan(db, owner, run.run_id, current.revision, changed_plan)


def test_events_are_bounded_paginated_and_payloads_do_not_leak_question(db, project_session):
    project_id, session_id = project_session
    owner = Principal(identity=uuid4(), kind="owner")
    run = submit_run(db, owner, project_id, session_id, "events", "private prompt", [], uuid4(), "fixture")
    events = get_events(db, owner, run.run_id, 0, 1)
    assert len(events) == 1
    assert "private prompt" not in str(events)
    with pytest.raises(DomainError, match="not_found"):
        get_events(db, Principal(identity=uuid4(), kind="external"), run.run_id, 0, 1)
    with pytest.raises(DomainError, match="cursor_expired"):
        get_events(db, owner, run.run_id, -1)
    with pytest.raises(DomainError, match="cursor_expired"):
        get_events(db, owner, run.run_id, 0, 201)
    for event in get_events(db, owner, run.run_id, 0):
        RunEvent.model_validate(event)


def test_plan_ready_event_matches_payload_contract(db, project_session):
    project_id, session_id = project_session
    owner = Principal(identity=uuid4(), kind="owner")
    run = submit_run(db, owner, project_id, session_id, "plan-event", "question", [], uuid4(), "fixture")
    snapshot_digest = get_plan(db, owner, run.run_id).plan.input_snapshot_digest
    revised = revise_plan(db, owner, run.run_id, run.revision, minimal_plan(snapshot_digest, uuid4()))
    event = get_events(db, owner, run.run_id, 0, 10)[-1]
    assert event["kind"] == "plan.ready"
    assert set(event["payload"]) == {"plan_digest"}
    RunEvent.model_validate(event)


def test_external_can_stop_only_its_own_submission(db, project_session):
    project_id, session_id = project_session
    first_owner, second_owner = Principal(identity=uuid4(), kind="owner"), Principal(identity=uuid4(), kind="owner")
    first = authenticate_bearer(db, create_token(db, first_owner, {project_id: ["work:submit", "work:cancel"]}))
    second = authenticate_bearer(db, create_token(db, second_owner, {project_id: ["work:submit", "work:cancel"]}))
    first_run = submit_run(db, first, project_id, session_id, "caller-one", "question one", [], uuid4(), "fixture")
    second_run = submit_run(db, second, project_id, session_id, "caller-two", "question two", [], uuid4(), "fixture")
    assert request_stop(db, first, first_run.run_id).state == "stopping"
    with pytest.raises(DomainError, match="forbidden"):
        request_stop(db, first, second_run.run_id)
    assert request_stop(db, first_owner, second_run.run_id).state == "stopping"


def test_snapshot_captures_immutable_shared_context_and_exact_file_version(db, project_session):
    project_id, session_id = project_session
    db.execute(text("UPDATE projects SET revision = 7, instructions = 'shared rules v1' WHERE id = :project"), {"project": project_id})
    earlier_message = uuid4()
    db.execute(text("INSERT INTO messages (id, project_id, session_id, role, content) VALUES (:id, :project, :session, 'user', 'prior question')"), {
        "id": earlier_message, "project": project_id, "session": session_id,
    })
    source_id, citation_id, artifact_id, finding_id, file_id = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    db.execute(text("INSERT INTO sources (id, project_id, metadata) VALUES (:id, :project, CAST(:metadata AS jsonb))"), {
        "id": source_id, "project": project_id, "metadata": '{"source":"fixture-v1"}',
    })
    db.execute(text("INSERT INTO citations (id, project_id, source_id, title, authors, identifier, verification) VALUES (:id, :project, :source, 'Evidence v1', '[\"A. Author\"]'::jsonb, 'doi:fixture-v1', 'verified')"), {
        "id": citation_id, "project": project_id, "source": source_id,
    })
    db.execute(text("INSERT INTO artifacts (id, project_id, title, kind, object_key, sha256, size, content_type) VALUES (:id, :project, 'Report v1', 'report', 'project/report-v1', :sha, 24, 'text/markdown')"), {
        "id": artifact_id, "project": project_id, "sha": "c" * 64,
    })
    db.execute(text("INSERT INTO findings (id, project_id, session_id, artifact_id, text, citation_ids) VALUES (:id, :project, :session, :artifact, 'saved result v1', :citation_ids)"), {
        "id": finding_id, "project": project_id, "session": session_id, "artifact": artifact_id, "citation_ids": [citation_id],
    })
    db.execute(text("INSERT INTO finding_citations (finding_id, citation_id, project_id) VALUES (:finding, :citation, :project)"), {
        "finding": finding_id, "citation": citation_id, "project": project_id,
    })
    db.execute(text("INSERT INTO file_versions (id, project_id, filename, object_key, size, content_type, state, sha256) VALUES (:id, :project, 'evidence.txt', 'project/file-v1', 17, 'text/plain', 'ready', :sha)"), {
        "id": file_id, "project": project_id, "sha": "a" * 64,
    })
    owner = Principal(identity=uuid4(), kind="owner")
    run = submit_run(db, owner, project_id, session_id, "snapshot", "new question", [file_id], uuid4(), "fixture")
    snapshot = db.execute(text("SELECT digest, manifest FROM input_snapshots WHERE run_id = :run"), {"run": run.run_id}).one()
    captured = snapshot.manifest
    db.execute(text("UPDATE projects SET revision = 8, instructions = 'shared rules v2' WHERE id = :project"), {"project": project_id})
    db.execute(text("UPDATE messages SET content = 'prior question changed' WHERE id = :id"), {"id": earlier_message})
    db.execute(text("UPDATE findings SET text = 'saved result v2' WHERE id = :id"), {"id": finding_id})
    db.execute(text("UPDATE citations SET title = 'Evidence v2', identifier = 'doi:fixture-v2' WHERE id = :id"), {"id": citation_id})
    db.execute(text("UPDATE sources SET metadata = '{\"source\":\"fixture-v2\"}' WHERE id = :id"), {"id": source_id})
    db.execute(text("UPDATE artifacts SET title = 'Report v2', object_key = 'project/report-v2', sha256 = :sha WHERE id = :id"), {
        "id": artifact_id, "sha": "d" * 64,
    })
    db.execute(text("UPDATE file_versions SET object_key = 'project/file-v2', size = 99, sha256 = :sha WHERE id = :id"), {
        "id": file_id, "sha": "b" * 64,
    })
    assert captured["project"] == {"id": str(project_id), "revision": 7, "instructions": "shared rules v1"}
    assert captured["conversation"][0]["content"] == "prior question"
    assert captured["findings"][0]["text"] == "saved result v1"
    assert captured["findings"][0]["artifact"] == {"id": str(artifact_id), "title": "Report v1", "kind": "report", "object_key": "project/report-v1", "sha256": "c" * 64, "size": 24, "content_type": "text/markdown", "partial": False}
    assert captured["findings"][0]["citations"][0]["source_metadata"] == {"source": "fixture-v1"}
    assert captured["files"][0] == {"id": str(file_id), "filename": "evidence.txt", "object_key": "project/file-v1", "sha256": "a" * 64, "size": 17, "content_type": "text/plain"}
    assert snapshot.digest.strip() == sha256(json.dumps(captured, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    assert db.execute(text("SELECT manifest FROM input_snapshots WHERE run_id = :run"), {"run": run.run_id}).scalar_one() == captured


def test_snapshot_reads_one_mvcc_version_across_concurrent_shared_context_edit(db, project_session):
    project_id, session_id = project_session
    finding_id, source_id, citation_id = uuid4(), uuid4(), uuid4()
    db.execute(text("UPDATE projects SET revision = 1, instructions = 'context-before' WHERE id = :project"), {"project": project_id})
    db.execute(text("INSERT INTO sources (id, project_id, metadata) VALUES (:id, :project, '{\"version\":\"before\"}'::jsonb)"), {
        "id": source_id, "project": project_id,
    })
    db.execute(text("INSERT INTO citations (id, project_id, source_id, title, authors) VALUES (:id, :project, :source, 'citation-before', '[\"Author\"]'::jsonb)"), {
        "id": citation_id, "project": project_id, "source": source_id,
    })
    db.execute(text("INSERT INTO findings (id, project_id, session_id, text, citation_ids) VALUES (:id, :project, :session, 'finding-before', :citation_ids)"), {
        "id": finding_id, "project": project_id, "session": session_id, "citation_ids": [citation_id],
    })
    db.execute(text("INSERT INTO finding_citations (finding_id, citation_id, project_id) VALUES (:finding, :citation, :project)"), {
        "finding": finding_id, "citation": citation_id, "project": project_id,
    })
    db.commit()
    snapshot_query_seen, writer_committed = Event(), Event()
    listener_fired = False

    def pause_after_context_select(conn, cursor, statement, parameters, context, executemany):
        nonlocal listener_fired
        normalized = " ".join(statement.lower().split())
        if not listener_fired and ("scientist.snapshot.capture" in normalized or "select revision, instructions from projects" in normalized):
            listener_fired = True
            snapshot_query_seen.set()
            if not writer_committed.wait(10):
                raise TimeoutError("snapshot race writer did not commit")

    target_engine = db_engine()
    event.listen(target_engine, "after_cursor_execute", pause_after_context_select)

    def capture():
        owner = Principal(identity=uuid4(), kind="owner")
        with database_session() as snapshot_db:
            run = submit_run(snapshot_db, owner, project_id, session_id, "atomic-context", "question", [], uuid4(), "fixture")
            snapshot_db.commit()
            return snapshot_db.execute(text("SELECT manifest FROM input_snapshots WHERE run_id = :run"), {"run": run.run_id}).scalar_one()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(capture)
            assert snapshot_query_seen.wait(10), "capture did not reach its context SELECT"
            with database_session() as writer_db:
                writer_db.execute(text("UPDATE projects SET revision = 2, instructions = 'context-after' WHERE id = :project"), {"project": project_id})
                writer_db.execute(text("UPDATE findings SET text = 'finding-after' WHERE id = :finding"), {"finding": finding_id})
                writer_db.execute(text("UPDATE citations SET title = 'citation-after' WHERE id = :citation"), {"citation": citation_id})
                writer_db.execute(text("UPDATE sources SET metadata = '{\"version\":\"after\"}'::jsonb WHERE id = :source"), {"source": source_id})
                writer_db.commit()
                writer_committed.set()
            captured = future.result(timeout=10)
    finally:
        event.remove(target_engine, "after_cursor_execute", pause_after_context_select)
    assert (captured["project"]["instructions"], captured["findings"][0]["text"],
            captured["findings"][0]["citations"][0]["title"],
            captured["findings"][0]["citations"][0]["source_metadata"]) == (
                "context-before", "finding-before", "citation-before", {"version": "before"},
            )


def test_tombstoned_file_cannot_be_selected_for_a_new_run(db, project_session):
    project_id, session_id = project_session
    file_id = uuid4()
    digest = sha256(b"retained fixture").hexdigest()
    db.execute(text("""INSERT INTO file_versions
        (id,project_id,filename,object_key,size,content_type,state,sha256,tombstoned_at)
        VALUES (:id,:project,'retained.txt',:key,16,'text/plain','ready',:sha,now())"""),
        {"id":file_id,"project":project_id,"key":f"{project_id}/{digest}","sha":digest})
    with pytest.raises(DomainError, match="not_found"):
        submit_run(db, Principal(identity=uuid4(), kind="owner"), project_id, session_id,
                   "tombstone-fixture", "question", [file_id], uuid4(), "fixture")


def test_ready_file_without_object_metadata_fails_closed(db, project_session):
    project_id, session_id = project_session
    file_id = uuid4()
    db.execute(text("INSERT INTO file_versions (id, project_id, filename, size, content_type, state) VALUES (:id, :project, 'broken.txt', 1, 'text/plain', 'ready')"), {
        "id": file_id, "project": project_id,
    })
    with pytest.raises(DomainError, match="storage_unavailable"):
        submit_run(db, Principal(identity=uuid4(), kind="owner"), project_id, session_id, "broken-file", "question", [file_id], uuid4(), "fixture")


def test_oversized_shared_context_is_rejected_before_run_creation(db, project_session):
    project_id, session_id = project_session
    db.execute(text("INSERT INTO messages (id, project_id, session_id, role, content) VALUES (:id, :project, :session, 'user', :content)"), {
        "id": uuid4(), "project": project_id, "session": session_id, "content": "x" * (1024 * 1024 + 1),
    })
    with pytest.raises(DomainError, match="request_too_large") as error:
        submit_run(db, Principal(identity=uuid4(), kind="owner"), project_id, session_id, "too-large", "question", [], uuid4(), "fixture")
    assert error.value.status == 413


def test_cross_project_run_access_is_indistinguishable_from_missing(db, project_session):
    project_id, session_id = project_session
    other_project = create_project(db, "other project")
    other_session = create_session(db, other_project, "other session")
    owner = Principal(identity=uuid4(), kind="owner")
    other_run = submit_run(db, owner, other_project, other_session, "other-run", "question", [], uuid4(), "fixture")
    token = create_token(db, owner, {project_id: ["result:read"]})
    external = authenticate_bearer(db, token)
    with pytest.raises(DomainError, match="not_found") as cross_project:
        get_run(db, external, other_run.run_id)
    with pytest.raises(DomainError, match="not_found") as missing:
        get_run(db, external, uuid4())
    assert (cross_project.value.code, cross_project.value.status) == (missing.value.code, missing.value.status)


def minimal_plan(snapshot_digest, provider_id, token_limit=0, elapsed_limit_ms=0):
    return PlanSpec(input_snapshot_digest=snapshot_digest, provider_id=provider_id, model="fixture", stages=["search"], allowed_ops=["search"], data_recipients=[], packages=[], token_limit=token_limit, elapsed_limit_ms=elapsed_limit_ms)
