"""Separate scoped protocol listener; local owner routes are never mounted here."""
from contextlib import asynccontextmanager
import ipaddress
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.responses import JSONResponse
from starlette.routing import Route


def _loopback(value: str) -> bool:
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def create_protocol_app(*, public_base_url: str = "http://127.0.0.1", allowed_origins: tuple[str, ...] = (), on_stop=None):
    public = urlsplit(public_base_url)
    if (public.scheme not in {"http", "https"} or not public.hostname or public.username or public.password
            or public.path not in {"", "/"} or public.query or public.fragment):
        raise ValueError("invalid_public_url")
    if public.scheme != "https" and not _loopback(public.hostname):
        raise ValueError("https_required")
    # Lazy SDK imports preserve the trusted dispatch role's dependency boundary.
    from scientist.mcp_api import create_mcp_app
    from scientist.a2a_api import create_a2a_app
    mcp = create_mcp_app()
    a2a = create_a2a_app(base_url=public_base_url.rstrip("/"), on_stop=on_stop)

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    class MCPEndpoint:
        async def __call__(self, scope, receive, send):
            await mcp(scope, receive, send)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # Starlette treats plain functions as Request -> Response handlers. A class
    # preserves this SDK wrapper's three-argument ASGI calling convention.
    app.router.routes.append(Route("/mcp", endpoint=MCPEndpoint(), methods=["POST"]))
    app.mount("/", a2a)
    host = public.hostname.lower()
    origins = frozenset(allowed_origins)

    class Guard:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.inner(scope, receive, send)
            headers = scope.get("headers", [])
            hosts = [v.decode("latin1") for k, v in headers if k.lower() == b"host"]
            supplied_origins = [v.decode("latin1") for k, v in headers if k.lower() == b"origin"]
            try:
                parsed_host = urlsplit("//" + hosts[0]) if len(hosts) == 1 else None
                invalid = (parsed_host is None or parsed_host.hostname != host or parsed_host.username
                           or parsed_host.password or parsed_host.path or parsed_host.query or parsed_host.fragment
                           or (parsed_host.port is not None and not 1 <= parsed_host.port <= 65535))
            except ValueError:
                invalid = True
            invalid = invalid or len(supplied_origins) > 1 or any(value not in origins for value in supplied_origins)
            if public.scheme == "https":
                invalid = invalid or scope.get("scheme") != "https"
            else:
                peer = scope.get("client")
                invalid = invalid or not peer or not _loopback(peer[0])
            if invalid:
                return await JSONResponse({"code": "forbidden"}, status_code=403)(scope, receive, send)
            return await self.inner(scope, receive, send)

    # Wrapped application keeps the parent's lifespan accessible to ASGI servers.
    return Guard(app)
