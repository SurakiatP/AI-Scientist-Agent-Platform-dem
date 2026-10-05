"""Durable transport identities must keep caller and project/session bindings."""
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from scientist.auth import create_token
from scientist.contracts import Principal
from scientist.db import create_project, create_session
from scientist.domain import submit_run


def setup_mapping(db):
    owner = Principal(identity=uuid4(), kind="owner")
    project = create_project(db, "protocol mapping")
    session_id = create_session(db, project, "existing owner session")
    token = create_token(db, owner, {project: ["work:submit", "project:read"]})
    from scientist.auth import authenticate_bearer
    caller = authenticate_bearer(db, token)
    run = submit_run(db, caller, project, session_id, "mapping-test", "bounded question", [], uuid4(), "fixture")
    return caller, project, session_id, run


def test_context_cannot_bind_session_from_different_project(db):
    caller, project, session_id, _ = setup_mapping(db)
    other = create_project(db, "other project")
    context_id = uuid4()
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.execute(text("INSERT INTO a2a_contexts (id, caller_id, project_id, session_id) VALUES (:id, :caller, :project, :session)"),
                       {"id": context_id, "caller": caller.identity, "project": other, "session": session_id})


def test_message_identity_is_unique_within_caller_and_task_context_is_bound(db):
    caller, project, session_id, run = setup_mapping(db)
    context = uuid4()
    db.execute(text("INSERT INTO a2a_contexts (id, caller_id, project_id, session_id) VALUES (:id, :caller, :project, :session)"),
               {"id": context, "caller": caller.identity, "project": project, "session": session_id})
    db.execute(text("INSERT INTO a2a_tasks (run_id, context_id, caller_id, project_id, session_id) VALUES (:run, :context, :caller, :project, :session)"),
               {"run": run.run_id, "context": context, "caller": caller.identity, "project": project, "session": session_id})
    values = {"caller": caller.identity, "message": "stable-message", "hash": "a" * 64, "run": run.run_id, "context": context}
    statement = text("INSERT INTO a2a_messages (caller_id, message_id, payload_hash, run_id, context_id) VALUES (:caller, :message, :hash, :run, :context)")
    db.execute(statement, values)
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.execute(statement, {**values, "hash": "b" * 64})
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.execute(text("INSERT INTO a2a_messages (caller_id, message_id, payload_hash, run_id, context_id) VALUES (:caller, :message, :hash, :run, :context)"),
                       {**values, "caller": uuid4(), "message": "forged-caller"})
