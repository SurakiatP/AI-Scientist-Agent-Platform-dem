"""Serve the operator-built React application from the local owner host."""

from pathlib import Path
import re
import stat

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

_SPAS = (
    re.compile(r"^/$"),
    re.compile(r"^/(?:projects|sources|history|settings|settings/appearance)$"),
    re.compile(r"^/projects/[^/]+$"),
    re.compile(r"^/projects/[^/]+/(?:library|runs|research-setup)$"),
    re.compile(r"^/projects/[^/]+/sessions/[^/]+$"),
)
_RESERVED_ROOTS = {"api", "control", "effects", "mcp", "a2a", "owner", "external", "broker", "private", "internal", "worker"}


def _publishable(parts: tuple[str, ...]) -> bool:
    return (not parts or parts[0].casefold() not in _RESERVED_ROOTS) and not any(part.startswith(".") for part in parts)


def checked_web_root(dist_dir: Path) -> Path:
    try:
        root = dist_dir.resolve(strict=True)
        info = (root / "index.html").lstat()
    except (OSError, RuntimeError) as exc:
        raise ValueError("web distribution must contain a regular index.html") from exc
    if not root.is_dir() or not stat.S_ISREG(info.st_mode):
        raise ValueError("web distribution must contain a regular index.html")
    return root


def create_web_router(dist_dir: Path) -> APIRouter:
    root = checked_web_root(dist_dir)
    index = root / "index.html"

    router = APIRouter()

    @router.get("/{asset_path:path}", include_in_schema=False)
    @router.get("/", include_in_schema=False)
    async def web(request: Request, asset_path: str = "") -> Response:
        if not _publishable(tuple(part for part in request.url.path.split("/") if part)):
            return Response(status_code=404)
        candidate = (root / asset_path).resolve()
        if not candidate.is_relative_to(root):
            return Response(status_code=404)
        if not _publishable(candidate.relative_to(root).parts):
            return Response(status_code=404)
        if candidate.is_file():
            return FileResponse(candidate)
        if request.headers.get("accept", "").find("text/html") >= 0 and any(pattern.fullmatch(request.url.path) for pattern in _SPAS):
            try:
                checked_web_root(root)
                current_index = index.resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                return Response(status_code=404)
            if not current_index.is_relative_to(root) or not current_index.is_file():
                return Response(status_code=404)
            return FileResponse(current_index)
        return Response(status_code=404)

    return router
