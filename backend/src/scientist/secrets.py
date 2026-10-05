from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text
from sqlalchemy.orm import Session

from scientist import settings
from scientist.auth import DomainError
from scientist.contracts import ConnectionView, Principal


def save_secret(db: Session, owner: Principal, label: str, value: str) -> UUID:
    if owner.kind != "owner":
        raise DomainError("forbidden", 403)
    if not label.strip() or len(label) > 200 or not value or len(value) > 16384:
        raise ValueError("invalid credential label or value")
    credential_id = uuid4()
    encrypted = _fernet().encrypt(value.encode())
    db.execute(text("INSERT INTO credentials (id, label, provider, encrypted_value) VALUES (:id, :label, 'manual', :value)"), {
        "id": credential_id, "label": label.strip(), "value": encrypted,
    })
    return credential_id


def read_secret(db: Session, credential_id: UUID) -> str:
    """Trusted broker only; never expose this function from a public route."""
    encrypted = db.execute(text("SELECT encrypted_value FROM credentials WHERE id = :id"), {"id": credential_id}).scalar_one_or_none()
    if encrypted is None:
        raise DomainError("not_found", 404)
    try:
        return _fernet().decrypt(bytes(encrypted)).decode()
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise DomainError("storage_unavailable", 503) from exc


def _owner(owner: Principal) -> None:
    if owner.kind != "owner":
        raise DomainError("forbidden", 403)


def create_connection(db: Session, owner: Principal, provider_id: UUID, label: str, model: str, secret: str) -> ConnectionView:
    """Store an owner-supplied provider credential keyed by provider_id (the id the broker matches)."""
    _owner(owner)
    origin = settings.provider_destinations().get(str(provider_id))
    if origin is None or len(origin) > 100:  # provider column is varchar(100); never truncate silently
        raise DomainError("provider_not_configured", 409)
    encrypted = _fernet().encrypt(secret.encode())
    inserted = db.execute(text("INSERT INTO credentials (id, label, provider, model, encrypted_value) VALUES (:id, :label, :provider, :model, :value) "
                               "ON CONFLICT (id) DO NOTHING RETURNING id"),
                          {"id": provider_id, "label": label, "provider": origin, "model": model, "value": encrypted}).first()
    if inserted is None:
        raise DomainError("connection_exists", 409)
    return ConnectionView(id=provider_id, label=label, provider=origin, model=model, state="ready", has_secret=True)


def list_connections(db: Session, owner: Principal) -> list[ConnectionView]:
    _owner(owner)
    destinations = settings.provider_destinations()
    out = []
    for row in db.execute(text("SELECT id, label, provider, model, encrypted_value FROM credentials WHERE model IS NOT NULL ORDER BY created_at, id")):
        if str(row.id) not in destinations:
            state = "unavailable_provider"
        else:
            try:
                _fernet().decrypt(bytes(row.encrypted_value))
                state = "ready"
            except (InvalidToken, ValueError):
                state = "invalid_credentials"
        out.append(ConnectionView(id=row.id, label=row.label, provider=destinations.get(str(row.id), row.provider), model=row.model, state=state, has_secret=True))
    return out


def revoke_connection(db: Session, owner: Principal, connection_id: UUID) -> None:
    _owner(owner)
    if not db.execute(text("DELETE FROM credentials WHERE id = :id AND model IS NOT NULL RETURNING id"), {"id": connection_id}).first():
        raise DomainError("not_found", 404)


def _fernet() -> Fernet:
    key_path = os.environ.get("SCIENTIST_MASTER_KEY_FILE")
    if not key_path:
        raise DomainError("storage_unavailable", 503)
    try:
        return Fernet(Path(key_path).read_bytes().strip())
    except (OSError, ValueError, TypeError) as exc:
        raise DomainError("storage_unavailable", 503) from exc
