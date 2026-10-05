"""A2A v1 JSON-RPC/SSE adapter for owner-approved research work."""
import asyncio
from contextlib import ExitStack
from functools import wraps

from fastapi import FastAPI, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, StreamingResponse
from sqlalchemy import text
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import RequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.common import ServerCallContextBuilder
from a2a.types import a2a_pb2 as p
from a2a.utils.errors import A2AError, InternalError, InvalidParamsError, TaskNotFoundError, UnsupportedOperationError, VersionNotSupportedError
from a2a.server.request_handlers.response_helpers import build_error_response

from scientist import a2a_mapping as mapping, domain, objects
from scientist.auth import DomainError, authenticate_bearer, authorize
from scientist.contracts import ObjectRef
from scientist.db import session

STATES = {
    "planning": p.TASK_STATE_SUBMITTED, "queued": p.TASK_STATE_SUBMITTED,
    "running": p.TASK_STATE_WORKING, "recovering": p.TASK_STATE_WORKING, "stopping": p.TASK_STATE_WORKING,
    "awaiting_approval": p.TASK_STATE_INPUT_REQUIRED, "waiting_input": p.TASK_STATE_INPUT_REQUIRED,
    "completed": p.TASK_STATE_COMPLETED, "failed": p.TASK_STATE_FAILED,
    "canceled": p.TASK_STATE_CANCELED, "rejected": p.TASK_STATE_REJECTED,
}
TERMINAL = {"completed", "failed", "canceled", "rejected"}
MAX_ENVELOPE_BYTES = 2 * 1024 * 1024


class EnvelopeLimit:
    """Bound JSON parsing without changing the SDK's wire parser or streaming receive channel."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/a2a" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_ENVELOPE_BYTES:
                response = JSONResponse(build_error_response(None, InvalidParamsError(message="Request is too large.")), status_code=413)
                return await response(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        first = True
        async def bounded_receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()
        return await self.app(scope, bounded_receive, send)


def task_from_run(run, context_id, *, artifact_base_url="", include_results=False):
    note = "Waiting for the project owner to review the request." if run.state in {"awaiting_approval", "waiting_input"} else (
        "Stopping; waiting for confirmation." if run.state == "stopping" else "Research status updated.")
    task = p.Task(id=str(run.run_id), context_id=str(context_id),
                  status=p.TaskStatus(state=STATES[run.state], message=p.Message(
                      message_id=f"status-{run.run_id}-{run.revision}", role=p.ROLE_AGENT,
                      parts=[p.Part(text=note)])))
    if include_results:
        task.metadata.update({"revision": run.revision, "usage_tokens": run.usage_tokens,
                              "reserved_tokens": run.reserved_tokens, "token_limit": run.token_limit,
                              "latest_cursor": run.latest_cursor})
        if artifact_base_url:
            task.artifacts.extend(p.Artifact(artifact_id=str(artifact.artifact_id), name=artifact.title,
                parts=[p.Part(url=f"{artifact_base_url}/a2a/artifacts/{artifact.artifact_id}", media_type=artifact.content_type)],
                metadata={"sha256": artifact.sha256, "size": artifact.size, "kind": artifact.kind, "partial": artifact.partial})
                for artifact in run.artifacts)
    return task


def _protocol_error(error):
    return TaskNotFoundError(message="Task unavailable.") if error.status in {403, 404} else InvalidParamsError(message="Request cannot be applied.")


def _safe(method):
    @wraps(method)
    async def wrapped(*args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except DomainError as error:
            raise _protocol_error(error) from None
        except A2AError:
            raise
        except Exception:
            raise InternalError(message="Research service unavailable.") from None
    return wrapped


def _principal(db, context):
    return authenticate_bearer(db, context.state["bearer"])


class BearerContext(ServerCallContextBuilder):
    def build(self, request):
        token = request.headers.get("authorization", "")
        if not token.startswith("Bearer ") or not token[7:]:
            raise HTTPException(401, "Authentication required.")
        with session() as db:
            try:
                authenticate_bearer(db, token[7:])
            except DomainError:
                raise HTTPException(403, "Authentication unavailable.") from None
            except Exception:
                raise HTTPException(503, "Research service unavailable.") from None
        return ServerCallContext(state={"bearer": token[7:], "headers": dict(request.headers)})


class ResearchHandler(RequestHandler):
    def __init__(self, on_stop=None, poll_interval=0.25, base_url=""):
        self.on_stop, self.poll_interval, self.base_url = on_stop, poll_interval, base_url

    def _task(self, db, principal, run, context_id, include_artifacts=True):
        try:
            authorize(db, principal, "result:read", run.project_id)
        except DomainError:
            return task_from_run(run, context_id)
        return task_from_run(run, context_id, artifact_base_url=self.base_url if include_artifacts else "", include_results=True)

    @_safe
    async def on_message_send(self, params, context):
        with session() as db:
            principal = _principal(db, context)
            run, context_id = mapping.submit(db, principal, params)
            db.commit()
            return self._task(db, principal, run, context_id)

    @_safe
    async def on_get_task(self, params, context):
        if params.tenant or params.history_length:
            raise InvalidParamsError(message="History is not available in this profile.")
        with session() as db:
            principal = _principal(db, context)
            bound = mapping.lookup(db, principal, params.id)
            return self._task(db, principal, domain.get_run(db, principal, bound.run_id), bound.context_id)

    @_safe
    async def on_list_tasks(self, params, context):
        if params.tenant or params.history_length or params.HasField("status_timestamp_after"):
            raise InvalidParamsError(message="Requested list options are unavailable.")
        size = params.page_size if params.HasField("page_size") else 50
        if not 1 <= size <= 100:
            raise InvalidParamsError(message="Invalid page size.")
        with session() as db:
            principal = _principal(db, context)
            context_id = mapping.check_context(db, principal, params.context_id).id if params.context_id else None
            after = mapping.lookup(db, principal, params.page_token).run_id if params.page_token else None
            rows = db.execute(text("SELECT * FROM a2a_tasks WHERE caller_id=:caller AND (CAST(:context AS uuid) IS NULL OR context_id=CAST(:context AS uuid)) ORDER BY run_id"),
                              {"caller": principal.identity, "context": context_id}).all()
            tasks = []
            total = 0
            for row in rows:
                try:
                    authorize(db, principal, "result:read", row.project_id)
                except DomainError:
                    continue
                run = domain.get_run(db, principal, row.run_id)
                task = self._task(db, principal, run, row.context_id, params.include_artifacts)
                if not params.status or task.status.state == params.status:
                    total += 1
                    if (after is None or row.run_id > after) and len(tasks) <= size:
                        tasks.append(task)
            return p.ListTasksResponse(tasks=tasks[:size], page_size=size,
                                       total_size=total,
                                       next_page_token=tasks[size - 1].id if len(tasks) > size else "")

    @_safe
    async def on_cancel_task(self, params, context):
        # Supervisor fencing can wait for worker exit; it must not delay independent reads/revocation polls.
        return await asyncio.to_thread(self._cancel, params, context)

    def _cancel(self, params, context):
        if params.tenant:
            raise InvalidParamsError(message="Invalid request.")
        with session() as db:
            principal = _principal(db, context)
            bound = mapping.lookup(db, principal, params.id, "work:cancel")
            run = domain._run_view(db, bound.run_id)
            domain.authorize_stop(db, principal, bound.run_id)
            if run.state not in TERMINAL:
                if self.on_stop is None:
                    raise UnsupportedOperationError(message="Cancellation is unavailable.")
                db.rollback()  # supervisor callback acquires its own fenced run lock
                run = self.on_stop(db, bound.run_id)
            return self._task(db, principal, run, bound.context_id)

    async def _stream(self, task_id, context):
        last_position = None
        deadline = asyncio.get_running_loop().time() + 300
        while asyncio.get_running_loop().time() < deadline:
            try:
                with session() as db:
                    principal = _principal(db, context)
                    bound = mapping.lookup(db, principal, task_id)
                    run = domain.get_run(db, principal, bound.run_id)
                    task = self._task(db, principal, run, bound.context_id)
                position = (run.revision, run.latest_cursor)
                if position != last_position:
                    yield task  # every subscription starts with the current authoritative snapshot
                    last_position = position
                if run.state in TERMINAL:
                    return
                await asyncio.sleep(self.poll_interval)
            except DomainError as error:
                raise _protocol_error(error) from None
            except Exception:
                raise InternalError(message="Research service unavailable.") from None

    async def on_subscribe_to_task(self, params, context):
        if params.tenant:
            raise InvalidParamsError(message="Invalid request.")
        async for event in self._stream(params.id, context):
            yield event

    async def on_message_send_stream(self, params, context):
        task = await self.on_message_send(params, context)
        async for event in self._stream(task.id, context):
            yield event

    async def on_create_task_push_notification_config(self, params, context):
        raise UnsupportedOperationError(message="Push notifications are unavailable.")

    async def on_get_task_push_notification_config(self, params, context):
        raise UnsupportedOperationError(message="Push notifications are unavailable.")

    async def on_list_task_push_notification_configs(self, params, context):
        raise UnsupportedOperationError(message="Push notifications are unavailable.")

    async def on_delete_task_push_notification_config(self, params, context):
        raise UnsupportedOperationError(message="Push notifications are unavailable.")

    async def on_get_extended_agent_card(self, params, context):
        raise UnsupportedOperationError(message="Extended cards are unavailable.")


def create_a2a_app(*, base_url="http://127.0.0.1", on_stop=None, poll_interval=0.25):
    if not 0.01 <= poll_interval <= 1:
        raise ValueError("Authorization polling must stay between 0.01 and 1 second.")
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.add_middleware(EnvelopeLimit)
    card = p.AgentCard(name="Research Platform", description="Submit research questions for project owner review and follow their progress.",
        version="1.0.0", supported_interfaces=[p.AgentInterface(url=base_url.rstrip("/") + "/a2a", protocol_binding="JSONRPC", protocol_version="1.0")],
        capabilities=p.AgentCapabilities(streaming=True, push_notifications=False, extended_agent_card=False),
        default_input_modes=["text/plain"], default_output_modes=["text/plain"],
        security_schemes={"bearer": p.SecurityScheme(http_auth_security_scheme=p.HTTPAuthSecurityScheme(scheme="Bearer"))},
        security_requirements=[p.SecurityRequirement(schemes={"bearer": p.StringList()})],
        skills=[p.AgentSkill(id="research", name="Research questions", description="Owner-reviewed literature research and evidence synthesis.", tags=["research"])])

    @app.middleware("http")
    async def version_header(request, call_next):
        if request.url.path == "/a2a" and request.headers.get("a2a-version") != "1.0":
            return JSONResponse(build_error_response(None, VersionNotSupportedError(message="A2A version 1.0 is required.")), status_code=400)
        return await call_next(request)

    @app.get("/a2a/artifacts/{artifact_id}")
    async def artifact_content(request: Request, artifact_id: str):
        verified = ExitStack()
        try:
            context = BearerContext().build(request)
            with session() as db:
                principal = _principal(db, context)
                bound = db.execute(text("SELECT t.run_id FROM artifacts a JOIN a2a_tasks t ON t.run_id=a.run_id WHERE a.id=:id AND t.caller_id=:caller"),
                                   {"id": mapping._uuid(artifact_id), "caller": principal.identity}).one_or_none()
                if bound is None:
                    raise DomainError("not_found", 404)
                mapping.lookup(db, principal, str(bound.run_id))
                artifact = domain.get_artifact(db, principal, mapping._uuid(artifact_id))
                ref = ObjectRef(project_id=artifact.project_id, key=artifact.object_key, sha256=artifact.sha256.strip(),
                                size=artifact.size, content_type="application/octet-stream")
            stream = verified.enter_context(objects.open_verified(ref))  # verify bytes before response headers
        except HTTPException as error:
            verified.close()
            return JSONResponse({"message": "Authentication unavailable."}, status_code=error.status_code)
        except DomainError:
            verified.close()
            return JSONResponse({"message": "Artifact unavailable."}, status_code=404)
        except Exception:
            verified.close()
            return JSONResponse({"message": "Research storage unavailable."}, status_code=503)

        async def chunks():
            try:
                while True:
                    with session() as db:
                        principal = _principal(db, context)
                        mapping.lookup(db, principal, str(bound.run_id))
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        return
                    yield chunk
                    await asyncio.sleep(0)
            except DomainError:
                return  # revoke closes delivery; declared length makes partial delivery detectable
            finally:
                verified.close()

        return StreamingResponse(chunks(), media_type="application/octet-stream", background=BackgroundTask(verified.close),
            headers={"Content-Length": str(ref.size), "Content-Disposition": f'attachment; filename="artifact-{artifact_id}"',
                     "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    handler = ResearchHandler(on_stop, poll_interval, base_url.rstrip("/"))
    add_a2a_routes_to_fastapi(app, agent_card_routes=create_agent_card_routes(card),
                             jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/a2a", context_builder=BearerContext()))
    app.state.a2a_card, app.state.a2a_handler = card, handler
    return app
