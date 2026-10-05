"""Owner-only project delegation and encrypted A2A peer credential references."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import text
from sqlalchemy.orm import Session

from scientist import owner_settings
from scientist.auth import DomainError
from scientist.contracts import Principal
from scientist.db import session
from scientist.secrets import _fernet


router = APIRouter(prefix="/api/v1")
PeerLabel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
PeerCredential = Annotated[str, StringConstraints(min_length=1, max_length=16384)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DelegationCreate(_Body):
    project_id: UUID
    peer_id: UUID
    credential_label: PeerLabel
    credential: PeerCredential


class CredentialUpdate(_Body):
    credential_label: PeerLabel
    credential: PeerCredential


def _principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal) or principal.kind != "owner":
        raise DomainError("forbidden", 403)
    return principal


def _database_session():
    with session() as db:
        yield db


def _error(request: Request, error: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        content={
            "code": error.code,
            "message": "The request could not be completed.",
            "request_id": getattr(request.state, "request_id", ""),
        },
        headers={"Cache-Control": "no-store"},
    )


def _credential_ref(db: Session, project_id: UUID, peer_id: UUID):
    return db.execute(
        text("SELECT id, label, created_at FROM credentials WHERE project_id=:project AND model IS NULL AND provider=:provider ORDER BY created_at DESC, id LIMIT 1"),
        {"project": project_id, "provider": f"peer:{peer_id}"},
    ).one_or_none()


def _endpoint_view(peer_id: UUID) -> tuple[str | None, str | None]:
    endpoint = owner_settings.configured_peers().get(str(peer_id))
    if endpoint is None:
        return None, None
    return endpoint, hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def _store_peer_secret(db: Session, project_id: UUID, peer_id: UUID, label: str, secret: str) -> UUID:
    provider = f"peer:{peer_id}"
    encrypted = _fernet().encrypt(secret.encode("utf-8"))
    db.execute(text("DELETE FROM credentials WHERE project_id=:project AND model IS NULL AND provider=:provider"), {
        "project": project_id,
        "provider": provider,
    })
    credential_id = uuid4()
    db.execute(
        text("INSERT INTO credentials (id, project_id, label, provider, encrypted_value) VALUES (:id, :project, :label, :provider, :value)"),
        {"id": credential_id, "project": project_id, "label": label, "provider": provider, "value": encrypted},
    )
    return credential_id


@router.get("/peer-delegations")
def list_peer_delegations(
    request: Request,
    project_id: UUID | None = None,
    owner: Principal = Depends(_principal),
    db: Session = Depends(_database_session),
):
    try:
        clause = "AND d.project_id=:project" if project_id is not None else ""
        params = {"owner": owner.identity}
        if project_id is not None:
            params["project"] = project_id
        rows = db.execute(text(
            "SELECT d.id, d.project_id, d.peer_id, d.actions, d.revoked_at, d.created_at, p.name AS project_name "
            "FROM delegations d JOIN projects p ON p.id=d.project_id "
            "WHERE d.owner_identity=:owner " + clause + " ORDER BY d.created_at DESC, d.id"
        ), params).all()
        payload = []
        for row in rows:
            endpoint, fingerprint = _endpoint_view(row.peer_id)
            credential = _credential_ref(db, row.project_id, row.peer_id)
            payload.append({
                "id": str(row.id),
                "project_id": str(row.project_id),
                "project_name": row.project_name,
                "peer_id": str(row.peer_id),
                "endpoint": endpoint,
                "endpoint_fingerprint": fingerprint,
                "configured": endpoint is not None,
                "actions": list(row.actions),
                "credential_id": str(credential.id) if credential else None,
                "credential_label": credential.label if credential else None,
                "credential_configured": credential is not None,
                "network_check": "not_run",
                "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
                "created_at": row.created_at.isoformat(),
            })
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})
    except DomainError as error:
        return _error(request, error)
    except Exception:
        return JSONResponse(status_code=503, content={"code": "storage_unavailable", "message": "Secure storage is unavailable.", "request_id": getattr(request.state, "request_id", "")}, headers={"Cache-Control": "no-store"})


@router.post("/peer-delegations", status_code=201)
def create_peer_delegation(
    request: Request,
    body: DelegationCreate,
    owner: Principal = Depends(_principal),
    db: Session = Depends(_database_session),
):
    try:
        endpoint, fingerprint = _endpoint_view(body.peer_id)
        if endpoint is None:
            raise DomainError("peer_not_configured", 409)
        if db.execute(text("SELECT 1 FROM projects WHERE id=:project"), {"project": body.project_id}).scalar_one_or_none() is None:
            raise DomainError("not_found", 404)
        existing = db.execute(text(
            "SELECT id, revoked_at FROM delegations WHERE project_id=:project AND owner_identity=:owner AND peer_id=:peer"
        ), {"project": body.project_id, "owner": owner.identity, "peer": body.peer_id}).one_or_none()
        if existing and existing.revoked_at is None:
            raise DomainError("delegation_exists", 409)
        credential_id = _store_peer_secret(db, body.project_id, body.peer_id, body.credential_label, body.credential)
        delegation_id = existing.id if existing else uuid4()
        if existing:
            db.execute(text("UPDATE delegations SET actions=ARRAY['peer'], revoked_at=NULL WHERE id=:id AND owner_identity=:owner"), {
                "id": delegation_id,
                "owner": owner.identity,
            })
        else:
            db.execute(text("INSERT INTO delegations (id, project_id, owner_identity, peer_id, actions) VALUES (:id, :project, :owner, :peer, ARRAY['peer'])"), {
                "id": delegation_id,
                "project": body.project_id,
                "owner": owner.identity,
                "peer": body.peer_id,
            })
        db.commit()
        return JSONResponse({
            "id": str(delegation_id),
            "project_id": str(body.project_id),
            "peer_id": str(body.peer_id),
            "endpoint": endpoint,
            "endpoint_fingerprint": fingerprint,
            "configured": True,
            "actions": ["peer"],
            "credential_id": str(credential_id),
            "credential_label": body.credential_label,
            "credential_configured": True,
            "network_check": "not_run",
            "revoked_at": None,
        }, status_code=201, headers={"Cache-Control": "no-store"})
    except DomainError as error:
        return _error(request, error)
    except Exception:
        db.rollback()
        return JSONResponse(status_code=503, content={"code": "storage_unavailable", "message": "Secure storage is unavailable.", "request_id": getattr(request.state, "request_id", "")}, headers={"Cache-Control": "no-store"})


@router.put("/peer-delegations/{delegation_id}/credential")
def update_peer_credential(
    request: Request,
    delegation_id: UUID,
    body: CredentialUpdate,
    owner: Principal = Depends(_principal),
    db: Session = Depends(_database_session),
):
    try:
        delegation = db.execute(text(
            "SELECT project_id, peer_id FROM delegations WHERE id=:id AND owner_identity=:owner AND revoked_at IS NULL"
        ), {"id": delegation_id, "owner": owner.identity}).one_or_none()
        if delegation is None:
            raise DomainError("not_found", 404)
        if str(delegation.peer_id) not in owner_settings.configured_peers():
            raise DomainError("peer_not_configured", 409)
        credential_id = _store_peer_secret(db, delegation.project_id, delegation.peer_id, body.credential_label, body.credential)
        db.commit()
        return JSONResponse({
            "id": str(delegation_id),
            "credential_id": str(credential_id),
            "credential_label": body.credential_label,
            "credential_configured": True,
        }, headers={"Cache-Control": "no-store"})
    except DomainError as error:
        return _error(request, error)
    except Exception:
        db.rollback()
        return JSONResponse(status_code=503, content={"code": "storage_unavailable", "message": "Secure storage is unavailable.", "request_id": getattr(request.state, "request_id", "")}, headers={"Cache-Control": "no-store"})


@router.delete("/peer-delegations/{delegation_id}", status_code=204)
def revoke_peer_delegation(
    request: Request,
    delegation_id: UUID,
    owner: Principal = Depends(_principal),
    db: Session = Depends(_database_session),
):
    try:
        row = db.execute(text(
            "UPDATE delegations SET revoked_at=:now WHERE id=:id AND owner_identity=:owner AND revoked_at IS NULL RETURNING project_id, peer_id"
        ), {"id": delegation_id, "owner": owner.identity, "now": datetime.now(timezone.utc)}).one_or_none()
        if row is None:
            raise DomainError("not_found", 404)
        db.execute(text("DELETE FROM credentials WHERE project_id=:project AND model IS NULL AND provider=:provider"), {
            "project": row.project_id,
            "provider": f"peer:{row.peer_id}",
        })
        db.commit()
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    except DomainError as error:
        return _error(request, error)
    except Exception:
        db.rollback()
        return JSONResponse(status_code=503, content={"code": "storage_unavailable", "message": "Secure storage is unavailable.", "request_id": getattr(request.state, "request_id", "")}, headers={"Cache-Control": "no-store"})
