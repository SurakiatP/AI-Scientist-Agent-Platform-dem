"""Durable caller-scoped A2A identities over the existing run ledger."""
from hashlib import sha256
import json
from uuid import UUID, uuid4

from google.protobuf.json_format import MessageToDict
from a2a.types import a2a_pb2 as p
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text

from scientist import domain
from scientist.auth import DomainError, authorize


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    session_id: UUID
    provider_id: UUID
    model: str = Field(min_length=1, max_length=200)
    input_ids: list[UUID] = Field(default_factory=list, max_length=1000)


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise DomainError("not_found", 404) from None


def lookup(db, principal, task_id: str, action="result:read"):
    row = db.execute(text("SELECT * FROM a2a_tasks WHERE run_id=:run AND caller_id=:caller"),
                     {"run": _uuid(task_id), "caller": principal.identity}).one_or_none()
    if row is None:
        raise DomainError("not_found", 404)
    authorize(db, principal, action, row.project_id)
    return row


def check_context(db, principal, context_id: str, project_id=None, session_id=None):
    row = db.execute(text("SELECT * FROM a2a_contexts WHERE id=:id AND caller_id=:caller"),
                     {"id": _uuid(context_id), "caller": principal.identity}).one_or_none()
    if row is None or (project_id is not None and (row.project_id != project_id or row.session_id != session_id)):
        raise DomainError("not_found", 404)
    return row


def submit(db, principal, params):
    message = params.message
    configuration = params.configuration
    if (params.tenant or message.role != p.ROLE_USER or message.task_id or not 1 <= len(message.message_id) <= 200
            or message.metadata or not message.parts or configuration.HasField("task_push_notification_config")
            or configuration.history_length or any(mode != "text/plain" for mode in configuration.accepted_output_modes)):
        raise DomainError("forbidden", 400)
    if any(part.WhichOneof("content") != "text" or part.metadata or part.filename or part.media_type for part in message.parts):
        raise DomainError("forbidden", 400)
    try:
        metadata = Submission.model_validate(MessageToDict(params.metadata))
    except ValidationError:
        raise DomainError("forbidden", 400) from None
    authorize(db, principal, "work:submit", metadata.project_id)
    question = "\n".join(part.text for part in message.parts)
    digest = sha256(json.dumps(MessageToDict(params), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
               {"key": f"a2a:{principal.identity}:{message.message_id}"})
    prior = db.execute(text("SELECT * FROM a2a_messages WHERE caller_id=:caller AND message_id=:message"),
                       {"caller": principal.identity, "message": message.message_id}).one_or_none()
    if prior is not None:
        if prior.payload_hash.strip() != digest:
            raise DomainError("idempotency_conflict", 409)
        mapping = lookup(db, principal, str(prior.run_id), "work:submit")
        return domain._run_view(db, prior.run_id), mapping.context_id
    if message.context_id:
        context_id = check_context(db, principal, message.context_id, metadata.project_id, metadata.session_id).id
    else:
        context_id = uuid4()
    # Domain and transport records share this transaction. An aborted request cannot leave an unmapped run.
    key = "a2a:" + sha256(message.message_id.encode()).hexdigest()
    run = domain.submit_run(db, principal, metadata.project_id, metadata.session_id, key, question,
                            metadata.input_ids, metadata.provider_id, metadata.model)
    if not message.context_id:
        db.execute(text("INSERT INTO a2a_contexts (id, caller_id, project_id, session_id) VALUES (:id,:caller,:project,:session)"),
                   {"id": context_id, "caller": principal.identity, "project": metadata.project_id, "session": metadata.session_id})
    values = {"run": run.run_id, "context": context_id, "caller": principal.identity,
              "project": metadata.project_id, "session": metadata.session_id}
    db.execute(text("INSERT INTO a2a_tasks (run_id,context_id,caller_id,project_id,session_id) VALUES (:run,:context,:caller,:project,:session)"), values)
    db.execute(text("INSERT INTO a2a_messages (caller_id,message_id,payload_hash,run_id,context_id) VALUES (:caller,:message,:hash,:run,:context)"),
               {**values, "message": message.message_id, "hash": digest})
    return run, context_id
