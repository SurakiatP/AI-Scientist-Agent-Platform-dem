"""Owner-only peer configuration metadata and scoped access-token administration."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from scientist import settings
from scientist.auth import DomainError, _hash, create_token, revoke_token
from scientist.contracts import Principal
from scientist.db import session


router = APIRouter(prefix="/api/v1")
_PEER_MAP_LIMIT = 16 * 1024


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TokenGrant(_Body):
    project_id: UUID
    actions: list[Literal["project:read", "file:attach", "work:submit", "result:read", "work:cancel"]] = Field(min_length=1, max_length=5)


class TokenCreate(_Body):
    grants: list[TokenGrant] = Field(min_length=1, max_length=100)


def _principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal) or principal.kind != "owner":
        raise DomainError("forbidden", 403)
    return principal


def configured_peers() -> dict[str, str]:
    """Read endpoint authority only from bounded, operator-owned configuration."""
    raw = os.environ.get("SCIENTIST_PEER_DESTINATIONS", "")
    try:
        encoded = raw.encode("utf-8")
    except UnicodeError:
        return {}
    if not raw or len(encoded) > _PEER_MAP_LIMIT:
        return {}
    try:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate peer configuration key")
                result[key] = value
            return result

        parsed = json.loads(raw, object_pairs_hook=pairs)
        if not isinstance(parsed, dict) or len(parsed) > 100:
            return {}
        result: dict[str, str] = {}
        for peer_id, endpoint in parsed.items():
            normalized_id = str(UUID(peer_id))
            if normalized_id != peer_id:
                return {}
            normalized_endpoint = settings._origin(endpoint) if isinstance(endpoint, str) else None
            if normalized_endpoint is None or normalized_endpoint != endpoint:
                return {}
            parts = urlsplit(endpoint)
            if parts.path not in {"", "/"} or parts.query or parts.fragment:
                return {}
            result[normalized_id] = endpoint.rstrip("/")
        return result
    except (ValueError, TypeError, UnicodeError):
        return {}


def _no_store_error(request: Request, error: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        content={
            "code": error.code,
            "message": "The request could not be completed.",
            "request_id": getattr(request.state, "request_id", ""),
        },
        headers={"Cache-Control": "no-store"},
    )


def _storage_error(request: Request) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "code": "storage_unavailable",
            "message": "Secure storage is unavailable.",
            "request_id": getattr(request.state, "request_id", ""),
        },
        headers={"Cache-Control": "no-store"},
    )


def _db(db: Session | None) -> Session:
    if db is None:
        raise DomainError("storage_unavailable", 503)
    return db


def _database_session():
    with session() as db:
        yield db


@router.get("/peers")
def list_peer_settings(
    request: Request,
    owner: Principal = Depends(_principal),
    db: Session = Depends(_database_session),
):
    try:
        db = _db(db)
        peers = configured_peers()
        ids = list(UUID(peer_id) for peer_id in peers)
        credential_ids: set[str] = set()
        if ids:
            credential_ids = {
                row.provider
                for row in db.execute(
                    text("SELECT DISTINCT provider FROM credentials WHERE model IS NULL AND provider = ANY(:providers)"),
                    {"providers": [f"peer:{value}" for value in ids]},
                )
            }
        payload = [
            {
                "peer_id": peer_id,
                "endpoint": endpoint,
                "endpoint_fingerprint": hashlib.sha256(endpoint.encode("utf-8")).hexdigest(),
                "configured": True,
                "credential_configured": f"peer:{peer_id}" in credential_ids,
                "network_check": "not_run",
            }
            for peer_id, endpoint in sorted(peers.items())
        ]
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})
    except DomainError as error:
        return _no_store_error(request, error)
    except Exception:
        db.rollback()
        return _storage_error(request)


@router.post("/access-tokens", status_code=201)
def create_access_token(
    request: Request,
    body: TokenCreate,
    owner: Principal = Depends(_principal),
    db: Session = Depends(_database_session),
):
    try:
        db = _db(db)
        grants: dict[UUID, list[str]] = {}
        for grant in body.grants:
            if grant.project_id in grants:
                raise DomainError("invalid_token_grants", 400)
            if db.execute(text("SELECT 1 FROM projects WHERE id=:project"), {"project": grant.project_id}).scalar_one_or_none() is None:
                raise DomainError("not_found", 404)
            grants[grant.project_id] = list(dict.fromkeys(grant.actions))
        token = create_token(db, owner, grants)
        row = db.execute(
            text("SELECT id, expires_at FROM access_tokens WHERE token_hash=:hash AND owner_identity=:owner"),
            {"hash": _hash(token), "owner": owner.identity},
        ).one()
        db.commit()
        return JSONResponse(
            {"id": str(row.id), "expires_at": row.expires_at.isoformat(), "token": token},
            status_code=201,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"},
        )
    except DomainError as error:
        return _no_store_error(request, error)
    except Exception:
        if db is not None:
            db.rollback()
        return _storage_error(request)


@router.get("/access-tokens")
def list_access_tokens(
    request: Request,
    owner: Principal = Depends(_principal),
    db: Session = Depends(_database_session),
):
    try:
        db = _db(db)
        rows = db.execute(
            text("SELECT id, expires_at, revoked_at, created_at FROM access_tokens WHERE owner_identity=:owner ORDER BY created_at DESC, id"),
            {"owner": owner.identity},
        ).all()
        payload = []
        for row in rows:
            grants = db.execute(
                text("SELECT project_id, actions FROM access_grants WHERE token_id=:token ORDER BY project_id"),
                {"token": row.id},
            ).all()
            payload.append({
                "id": str(row.id),
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
                "created_at": row.created_at.isoformat(),
                "grants": [{"project_id": str(grant.project_id), "actions": sorted(grant.actions)} for grant in grants],
            })
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})
    except DomainError as error:
        return _no_store_error(request, error)
    except Exception:
        db.rollback()
        return _storage_error(request)


@router.delete("/access-tokens/{token_id}", status_code=204)
def delete_access_token(
    request: Request,
    token_id: UUID,
    owner: Principal = Depends(_principal),
    db: Session = Depends(_database_session),
):
    try:
        db = _db(db)
        revoke_token(db, owner, token_id)
        db.commit()
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    except DomainError as error:
        return _no_store_error(request, error)
    except Exception:
        db.rollback()
        return _storage_error(request)
