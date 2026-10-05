from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from scientist.auth import DomainError
from scientist.contracts import Principal
from scientist.domain import list_messages, submit_run
from test_rest_workflow import client, new_project, new_session, storage  # noqa: F401
from scientist.db import create_project, create_session

OWNER = Principal(identity=uuid4(), kind="owner")
PROVIDER = uuid4()


def _submit(db, project_id, session_id, key, retry_of=None, question="why?"):
    kw = {"retry_of": retry_of} if retry_of else {}
    return submit_run(db, OWNER, project_id, session_id, key, question, [], PROVIDER, "m", **kw)


def test_submit_records_owner_question_once(db, project_session):
    project_id, session_id = project_session
    run = _submit(db, project_id, session_id, "k1")
    _submit(db, project_id, session_id, "k1")
    rows = db.execute(text("SELECT role, content FROM messages WHERE run_id = :r"), {"r": run.run_id}).all()
    assert [(r.role, r.content) for r in rows] == [("user", "why?")]
    msgs = list_messages(db, OWNER, session_id)
    assert len(msgs) == 1 and msgs[0]["run_id"] == str(run.run_id)
    manifest = db.execute(text("SELECT manifest FROM input_snapshots WHERE run_id = :r"), {"r": run.run_id}).scalar_one()
    assert manifest["conversation"] == []


def _fails(db, status, code, *args):
    with pytest.raises(DomainError) as e:
        _submit(db, *args)
    assert (e.value.status, e.value.code) == (status, code)


def test_retry_links_terminal_predecessor_only(db, project_session):
    project_id, session_id = project_session
    first = _submit(db, project_id, session_id, "a")
    for n, state in enumerate(["awaiting_approval", "running", "completed"]):
        db.execute(text("UPDATE runs SET state=:s WHERE id=:r"), {"s": state, "r": first.run_id})
        _fails(db, 409, "revision_conflict", project_id, session_id, f"b{n}", first.run_id)
    db.execute(text("UPDATE runs SET state='canceled' WHERE id=:r"), {"r": first.run_id})
    retry = _submit(db, project_id, session_id, "c", first.run_id)
    assert retry.retry_of == first.run_id
    assert db.execute(text("SELECT count(*) FROM messages WHERE run_id=:r"), {"r": retry.run_id}).scalar_one() == 1
    other = create_project(db, "other")
    _fails(db, 404, "not_found", other, create_session(db, other, "s"), "d", first.run_id)
    _fails(db, 404, "not_found", project_id, session_id, "e", uuid4())


def test_retry_of_in_idempotency(db, project_session):
    project_id, session_id = project_session
    first = _submit(db, project_id, session_id, "a")
    db.execute(text("UPDATE runs SET state='canceled' WHERE id=:r"), {"r": first.run_id})
    run = _submit(db, project_id, session_id, "k", first.run_id)
    assert _submit(db, project_id, session_id, "k", first.run_id).run_id == run.run_id
    with pytest.raises(DomainError) as e:
        _submit(db, project_id, session_id, "k")
    assert "idempotency_conflict" in str(e.value.args)
    plain = _submit(db, project_id, session_id, "p")
    old = db.execute(text("SELECT submission_hash FROM runs WHERE id=:r"), {"r": plain.run_id}).scalar_one()
    from scientist.domain import _digest
    assert old.strip() == _digest({"project_id": str(project_id), "session_id": str(session_id), "question": "why?",
                                   "input_ids": [], "provider_id": str(PROVIDER), "model": "m"})


def test_rest_create_run_with_retry_of(client, db):
    project = new_project(client)
    session = new_session(client, project["id"])
    body = {"submission_key": uuid4().hex, "question": "q", "provider_id": str(uuid4()), "model": "m"}
    first = client.post(f"/api/v1/sessions/{session['id']}/runs", json=body)
    assert first.status_code == 201 and first.json()["retry_of"] is None
    retry_body = {**body, "submission_key": uuid4().hex, "retry_of": first.json()["run_id"]}
    assert client.post(f"/api/v1/sessions/{session['id']}/runs", json=retry_body).status_code == 409
    db.execute(text("UPDATE runs SET state='canceled' WHERE id=:r"), {"r": first.json()["run_id"]})
    db.commit()
    r = client.post(f"/api/v1/sessions/{session['id']}/runs", json=retry_body)
    assert r.status_code == 201 and r.json()["retry_of"] == first.json()["run_id"]
    msgs = client.get(f"/api/v1/sessions/{session['id']}/messages").json()
    assert [m["run_id"] for m in (msgs if isinstance(msgs, list) else msgs["messages"])] == [first.json()["run_id"], r.json()["run_id"]]


def test_008_foreign_keys_block_cross_project(db, project_session):
    project_id, session_id = project_session
    run = _submit(db, project_id, session_id, "a")
    other = create_project(db, "other")
    other_session = create_session(db, other, "s")
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.execute(text("INSERT INTO messages (id, project_id, session_id, role, content, run_id) VALUES (:i,:p,:s,'user','x',:r)"),
                       {"i": uuid4(), "p": other, "s": other_session, "r": run.run_id})
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.execute(text("INSERT INTO runs (id, project_id, session_id, caller_identity, submission_key, submission_hash, retry_of) "
                            "VALUES (:i,:p,:s,:c,'x',:h,:r)"),
                       {"i": uuid4(), "p": other, "s": other_session, "c": uuid4(), "h": "0" * 64, "r": run.run_id})


def test_second_snapshot_has_first_question_not_its_own(db, project_session):
    project_id, session_id = project_session
    _submit(db, project_id, session_id, "q1", question="first?")
    second = _submit(db, project_id, session_id, "q2", question="second?")
    manifest = db.execute(text("SELECT manifest FROM input_snapshots WHERE run_id = :r"), {"r": second.run_id}).scalar_one()
    contents = [m["content"] for m in manifest["conversation"]]
    assert "first?" in contents and "second?" not in contents


def test_external_may_retry_only_own_submission(db, project_session):
    project_id, session_id = project_session
    owned = _submit(db, project_id, session_id, "o")
    db.execute(text("UPDATE runs SET state='canceled' WHERE id=:r"), {"r": owned.run_id})
    from scientist.auth import authenticate_bearer, create_token
    ext = authenticate_bearer(db, create_token(db, OWNER, {project_id: ["project:read", "work:submit"]}))
    with pytest.raises(DomainError) as e:
        submit_run(db, ext, project_id, session_id, "x", "why?", [], PROVIDER, "m", retry_of=owned.run_id)
    assert (e.value.status, e.value.code) == (404, "not_found")
    mine = submit_run(db, ext, project_id, session_id, "x1", "why?", [], PROVIDER, "m")
    db.execute(text("UPDATE runs SET state='failed' WHERE id=:r"), {"r": mine.run_id})
    assert submit_run(db, ext, project_id, session_id, "x2", "why?", [], PROVIDER, "m", retry_of=mine.run_id).retry_of == mine.run_id


def test_one_user_question_per_run_but_other_roles_allowed(db, project_session):
    project_id, session_id = project_session
    run = _submit(db, project_id, session_id, "m1")
    for _ in range(2):
        db.execute(text("INSERT INTO messages (id, project_id, session_id, role, content, run_id) VALUES (:i, :p, :s, 'assistant', 'a', :r)"),
                   {"i": uuid4(), "p": project_id, "s": session_id, "r": run.run_id})
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.execute(text("INSERT INTO messages (id, project_id, session_id, role, content, run_id) VALUES (:i, :p, :s, 'user', 'again', :r)"),
                       {"i": uuid4(), "p": project_id, "s": session_id, "r": run.run_id})
