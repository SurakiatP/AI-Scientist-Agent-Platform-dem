from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text
from sqlalchemy.orm import Session

from scientist.auth import DomainError
from scientist.contracts import Principal


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


def _fernet() -> Fernet:
    key_path = os.environ.get("SCIENTIST_MASTER_KEY_FILE")
    if not key_path:
        raise DomainError("storage_unavailable", 503)
    try:
        return Fernet(Path(key_path).read_bytes().strip())
    except (OSError, ValueError, TypeError) as exc:
        raise DomainError("storage_unavailable", 503) from exc
