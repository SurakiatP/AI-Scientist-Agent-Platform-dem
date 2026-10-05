from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hmac
import ipaddress
from threading import Lock
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from scientist.auth import DomainError, _consume_bootstrap, _rotate_owner_csrf, authenticate_bearer, authenticate_owner_session, create_owner_session, verify_csrf
from scientist.db import session as database_session


def create_app(*, bootstrap_token: str, bootstrap_expires_at: datetime | None = None) -> FastAPI:
    if len(bootstrap_token) < 32:
        raise ValueError("bootstrap token must contain at least 32 characters")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)  # no unguarded schema/docs endpoints
    state_lock = Lock()
    consumed = False
    expiry = bootstrap_expires_at or datetime.now(timezone.utc) + timedelta(minutes=5)
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise ValueError("bootstrap expiry must be a future timezone-aware datetime")

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status, content={
            "code": exc.code, "message": _message(exc.code),
            "request_id": getattr(request.state, "request_id", ""),
        })

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Never echo pydantic input/ctx: request bodies can carry secrets.
        return JSONResponse(status_code=422, headers={"Cache-Control": "no-store"}, content={
            "code": "invalid_request", "message": _message("invalid_request"),
            "request_id": getattr(request.state, "request_id", ""),
        })

    @app.middleware("http")
    async def guard_owner(request: Request, call_next):
        request.state.request_id = str(uuid4())
        bootstrap_path = request.url.path == "/api/v1/bootstrap"
        external_path = request.url.path.startswith("/api/v1/external/")
        owner_api = request.url.path.startswith("/api/v1/") and not bootstrap_path and not external_path
        if bootstrap_path or owner_api:
            if not _loopback_peer(request) or not _valid_host_origin(request, require_origin=request.method != "GET"):
                return _error(403, "forbidden", request.state.request_id)
        if owner_api:
            cookie = request.cookies.get("owner_session", "")
            try:
                with database_session() as db:
                    principal, csrf_hash = authenticate_owner_session(db, cookie)
            except DomainError:
                return _error(401, "forbidden", request.state.request_id)
            request.state.principal = principal
            if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not verify_csrf(csrf_hash, request.headers.get("x-csrf-token", "")):
                return _error(403, "forbidden", request.state.request_id)
        return await call_next(request)

    @app.post("/api/v1/bootstrap")
    async def bootstrap(request: Request):
        nonlocal consumed
        body = await request.json()
        supplied = body.get("token", "") if isinstance(body, dict) else ""
        with state_lock:
            if consumed or datetime.now(timezone.utc) >= expiry or not hmac.compare_digest(str(supplied), bootstrap_token):
                return _error(401, "forbidden", request.state.request_id)
            try:
                with database_session() as db:
                    _consume_bootstrap(db, bootstrap_token, expiry)
                    cookie, csrf = create_owner_session(db)
                    db.commit()
            except DomainError as exc:
                return _error(exc.status, exc.code, request.state.request_id)
            except Exception:
                return _error(503, "storage_unavailable", request.state.request_id)
            consumed = True
        response = JSONResponse({"csrf_token": csrf}, headers={
            "Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
        })
        response.set_cookie("owner_session", cookie, httponly=True, samesite="lax", path="/api/v1", max_age=43200)
        return response

    @app.get("/api/v1/owner/session")
    async def owner_session(request: Request):
        with database_session() as db:
            csrf = _rotate_owner_csrf(db, request.cookies.get("owner_session", ""))
            db.commit()
        return JSONResponse({"identity": str(request.state.principal.identity), "kind": "owner", "csrf_token": csrf}, headers={
            "Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
        })

    @app.get("/api/v1/external/session")
    async def external_session(request: Request):
        auth = request.headers.get("authorization", "")
        scheme, _, value = auth.partition(" ")
        if scheme.lower() != "bearer" or not value:
            return _error(401, "forbidden", request.state.request_id)
        try:
            with database_session() as db:
                principal = authenticate_bearer(db, value)
        except DomainError:
            return _error(401, "forbidden", request.state.request_id)
        return {"identity": str(principal.identity), "kind": "external"}

    from scientist import api, owner_settings, peers  # deferred: api imports domain modules that need the database settings
    app.include_router(api.router)
    app.include_router(api.control_router)
    app.include_router(owner_settings.router)
    app.include_router(peers.router)
    return app


def _loopback_peer(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _valid_host_origin(request: Request, *, require_origin: bool) -> bool:
    host = request.headers.get("host", "").lower()
    try:
        hostname = urlsplit("//" + host).hostname if host else None
    except ValueError:
        return False
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        return False
    origin = request.headers.get("origin")
    if not origin:
        return not require_origin
    try:
        parsed = urlsplit(origin)
        return parsed.scheme == request.url.scheme and parsed.netloc.lower() == host and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    except ValueError:
        return False


def _error(status: int, code: str, request_id: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": _message(code), "request_id": request_id}, headers={"Cache-Control": "no-store"})


def _message(code: str) -> str:
    return {"forbidden": "This request is not authorized.", "not_found": "The requested item was not found.",
            "revision_conflict": "The item changed. Reload and try again.", "idempotency_conflict": "This request key was already used with different content.",
            "approval_required": "Owner approval is required.", "cursor_expired": "The event cursor or page size is invalid.",
            "budget_exhausted": "The approved usage limit has been reached.", "storage_unavailable": "Secure storage is unavailable.",
            "request_too_large": "The captured research context is too large.", "runtime_unavailable": "The run service is not available.",
            "decision_ambiguous": "More than one step needs a decision. Reload and try again.",
            "data_destinations_not_configured": "Research data destinations are not configured.",
            "invalid_request": "The request is not valid.", "provider_not_configured": "This provider is not configured on the server.",
            "connection_exists": "A connection for this provider already exists. Revoke it first."}.get(code, "The request could not be completed.")
