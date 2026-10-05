from __future__ import annotations

import base64
import binascii
from contextlib import asynccontextmanager
from contextvars import ContextVar
import json
from io import BytesIO
from typing import Callable, ContextManager
from uuid import UUID

from mcp.server import MCPServer
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp
from sqlalchemy.orm import Session

from scientist import domain, files
from scientist.auth import DomainError, authenticate_bearer
from scientist.contracts import FileView, ObjectRef, Principal
from scientist.db import session as database_session
from scientist.objects import MAX_UPLOAD_BYTES, StorageIntegrityError, open_verified


_principal: ContextVar[Principal | None] = ContextVar("mcp_principal", default=None)
_MAX_ENCODED_UPLOAD = ((MAX_UPLOAD_BYTES + 2) // 3) * 4


def create_mcp_app(*, session_factory: Callable[[], ContextManager[Session]] = database_session) -> ASGIApp:
    """Create the scoped Streamable HTTP MCP endpoint mounted at the exact `/mcp` path."""
    server = MCPServer("scientist-platform", version="1")

    def current_principal() -> Principal:
        principal = _principal.get()
        if principal is None or principal.kind != "external":
            raise MCPError(-32001, "forbidden")
        return principal

    def invoke(call):
        principal = current_principal()
        try:
            with session_factory() as db:
                result = call(db, principal)
                return result
        except DomainError as exc:
            raise MCPError(-32000, exc.code) from None
        except (ValueError, TypeError) as exc:
            # Do not forward validation inputs, SQL errors or exception details to clients.
            raise MCPError(-32602, "invalid_request") from None

    def parse_id(value: str) -> UUID:
        try:
            return UUID(value)
        except (ValueError, TypeError, AttributeError):
            raise DomainError("invalid_request", 400) from None

    @server.tool(description="List projects granted to the authenticated bearer.")
    def list_projects(after: str | None = None, limit: int = 50) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise MCPError(-32602, "invalid_request")
        cursor = parse_id(after) if after is not None else None

        def run(db, principal):
            rows = domain.list_projects(db, principal, cursor, limit + 1)
            more = len(rows) > limit
            page = rows[:limit]
            return {"projects": [row.model_dump(mode="json") for row in page],
                    "next_cursor": str(page[-1].id) if more and page else None}

        return invoke(run)

    @server.tool(description="Attach a supported, bounded input file to a granted project.")
    def attach_input(project_id: str, filename: str, declared_size: int, content_type: str,
                     content_base64: str) -> dict:
        if type(declared_size) is not int or declared_size < 0 or declared_size > MAX_UPLOAD_BYTES:
            raise MCPError(-32602, "request_too_large")
        if not isinstance(content_base64, str) or len(content_base64) > _MAX_ENCODED_UPLOAD:
            raise MCPError(-32602, "request_too_large")
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise MCPError(-32602, "invalid_request") from None
        if len(content) != declared_size:
            raise MCPError(-32602, "invalid_request")
        suffix = files.PurePath(filename).suffix.lower()
        expected_type = files._TYPES.get(suffix, (None, None))[0]
        if expected_type is None or content_type != expected_type:
            raise MCPError(-32602, "unsupported_file_type")

        def run(db, principal):
            file_id = files.attach(db, principal, parse_id(project_id), filename, BytesIO(content))
            db.commit()
            return FileView(id=file_id, project_id=parse_id(project_id), filename=filename,
                            size=declared_size, content_type=expected_type,
                            state="preparing", error_code=None).model_dump(mode="json")

        return invoke(run)

    @server.tool(description="Submit research to the owner's approval queue. Submission is durable and returns promptly.")
    def submit_research(project_id: str, session_id: str, submission_key: str, question: str,
                        input_ids: list[str], provider_id: str, model: str) -> dict:
        if len(input_ids) > 1000:
            raise MCPError(-32602, "request_too_large")

        def run(db, principal):
            result = domain.submit_run(db, principal, parse_id(project_id), parse_id(session_id),
                                       submission_key, question, [parse_id(item) for item in input_ids],
                                       parse_id(provider_id), model)
            db.commit()
            return result.model_dump(mode="json")

        return invoke(run)

    @server.tool(description="Read an authorized research run snapshot.")
    def get_research(run_id: str) -> dict:
        return invoke(lambda db, principal: domain.get_run(db, principal, parse_id(run_id)).model_dump(mode="json"))

    @server.tool(description="Read application event history after a bounded sequence cursor.")
    def get_research_events(run_id: str, after: int = 0, limit: int = 100) -> dict:
        if type(after) is not int or type(limit) is not int or not 1 <= limit <= 200:
            raise MCPError(-32602, "invalid_request")

        def run(db, principal):
            try:
                events = domain.get_events(db, principal, parse_id(run_id), after, limit)
            except DomainError as exc:
                if exc.code == "cursor_expired":
                    return {"events": [], "cursor": None, "cursor_expired": True}
                raise
            cursor = events[-1]["sequence"] if events else after
            return {"events": events, "cursor": cursor, "cursor_expired": False}

        return invoke(run)

    @server.tool(description="List result metadata for an authorized research run.")
    def list_results(run_id: str) -> dict:
        return invoke(lambda db, principal: {
            "results": [item.model_dump(mode="json") for item in domain.list_artifacts(db, principal, parse_id(run_id))]
        })

    @server.tool(description="Request cancellation of an authorized submitted run.")
    def request_stop(run_id: str) -> dict:
        def run(db, principal):
            view = domain.request_stop(db, principal, parse_id(run_id))
            db.commit()
            return view.model_dump(mode="json")

        return invoke(run)

    @server.resource("scientist://artifacts/{artifact_id}", name="research-artifact", description="Read authorized artifact metadata by application artifact identity.")
    def artifact_resource(artifact_id: str) -> str:
        result = invoke(lambda db, principal: domain.get_artifact(db, principal, parse_id(artifact_id)))
        return json.dumps({"artifact_id": str(result.id), "project_id": str(result.project_id),
                           "sha256": result.sha256.strip(), "size": result.size,
                           "content_type": result.content_type}, separators=(",", ":"))

    @server.resource(
        "scientist://artifacts/{artifact_id}/content",
        name="research-artifact-content",
        description="Read bounded, hash-verified artifact bytes with the caller's result:read grant.",
        mime_type="application/octet-stream",
    )
    def artifact_content_resource(artifact_id: str) -> bytes:
        def read(db, principal):
            artifact = domain.get_artifact(db, principal, parse_id(artifact_id))
            if artifact.size < 0 or artifact.size > MAX_UPLOAD_BYTES:
                raise MCPError(-32000, "resource_too_large")
            try:
                reference = ObjectRef(
                    project_id=artifact.project_id,
                    key=artifact.object_key,
                    sha256=artifact.sha256.strip(),
                    size=artifact.size,
                    content_type=artifact.content_type,
                )
                with open_verified(reference) as stream:
                    content = stream.read(MAX_UPLOAD_BYTES + 1)
            except StorageIntegrityError:
                raise MCPError(-32000, "storage_integrity") from None
            if len(content) != artifact.size or len(content) > MAX_UPLOAD_BYTES:
                raise MCPError(-32000, "storage_integrity")
            # Recheck the same caller and project after the potentially slow object read.
            domain.get_artifact(db, principal, parse_id(artifact_id))
            return content

        return invoke(read)


    mcp_app = server.streamable_http_app(streamable_http_path="/mcp",
                                        max_request_body_size=_MAX_ENCODED_UPLOAD + 128 * 1024)

    @asynccontextmanager
    async def lifespan(app):
        async with server.session_manager.run():
            yield

    app = Starlette(routes=[Route("/mcp", endpoint=mcp_app, methods=["POST"])], lifespan=lifespan)

    async def authenticated_app(scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/mcp":
            await app(scope, receive, send)
            return
        auth_values = [value for key, value in scope.get("headers", []) if key.lower() == b"authorization"]
        authorization = auth_values[0].decode("latin-1") if len(auth_values) == 1 else ""
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token or token.strip() != token:
            response = JSONResponse({"code": "forbidden", "message": "Authentication required"}, status_code=401,
                                    headers={"Cache-Control": "no-store"})
            await response(scope, receive, send)
            return
        try:
            with session_factory() as db:
                principal = authenticate_bearer(db, token)
        except DomainError:
            response = JSONResponse({"code": "forbidden", "message": "Authentication required"}, status_code=401,
                                    headers={"Cache-Control": "no-store"})
            await response(scope, receive, send)
            return
        context_token = _principal.set(principal)
        try:
            await app(scope, receive, send)
        finally:
            _principal.reset(context_token)

    # Parent route composition mounts the callable at `/mcp` and enters this SDK manager
    # from the parent lifespan; standalone use can let the inner Starlette lifespan run it.
    authenticated_app.session_manager = server.session_manager
    return authenticated_app
