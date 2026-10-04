from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import secrets
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from scientist.contracts import Principal


class DomainError(Exception):
    def __init__(self, code: str, status: int):
        self.code, self.status = code, status
        super().__init__(code)


def authorize(db: Session, principal: Principal, action: str, project_id: UUID) -> None:
    if principal.kind == "owner":
        return
    if principal.kind != "external":
        raise DomainError("forbidden", 403)
    allowed = db.execute(text("""
        SELECT 1 FROM access_tokens t JOIN access_grants g ON g.token_id = t.id
        WHERE t.id = :token_id AND t.revoked_at IS NULL
          AND (t.expires_at IS NULL OR t.expires_at > now())
          AND g.project_id = :project_id AND :action = ANY(g.actions)
    """), {"token_id": principal.identity, "project_id": project_id, "action": action}).scalar_one_or_none()
    if not allowed:
        raise DomainError("forbidden", 403)


def create_token(db: Session, owner: Principal, grants: dict[UUID, list[str]]) -> str:
    _owner(owner)
    if not grants or any(not actions or set(actions) - {"project:read", "file:attach", "work:submit", "result:read", "work:cancel"} for actions in grants.values()):
        raise ValueError("token grants must contain supported project actions")
    token_id, token = uuid4(), secrets.token_urlsafe(32)
    expiry = datetime.now(timezone.utc) + timedelta(days=30)
    db.execute(text("INSERT INTO access_tokens (id, token_hash, owner_identity, expires_at) VALUES (:id, :hash, :owner, :expiry)"), {
        "id": token_id, "hash": _hash(token), "owner": owner.identity, "expiry": expiry,
    })
    for project_id, actions in grants.items():
        db.execute(text("INSERT INTO access_grants (token_id, project_id, actions) VALUES (:token, :project, :actions)"), {
            "token": token_id, "project": project_id, "actions": sorted(set(actions)),
        })
    return token


def revoke_token(db: Session, owner: Principal, token_id: UUID) -> None:
    _owner(owner)
    updated = db.execute(text("UPDATE access_tokens SET revoked_at = now() WHERE id = :id AND owner_identity = :owner AND revoked_at IS NULL RETURNING id"), {
        "id": token_id, "owner": owner.identity,
    }).scalar_one_or_none()
    if not updated:
        raise DomainError("not_found", 404)


def authenticate_bearer(db: Session, token: str) -> Principal:
    if not token or len(token) > 256:
        raise DomainError("forbidden", 403)
    row = db.execute(text("""
        SELECT id FROM access_tokens WHERE token_hash = :hash
          AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())
    """), {"hash": _hash(token)}).scalar_one_or_none()
    if row is None:
        raise DomainError("forbidden", 403)
    return Principal(identity=row, kind="external")


def create_owner_session(db: Session) -> tuple[str, str]:
    session_token, csrf_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.execute(text("INSERT INTO owner_sessions (id, token_hash, csrf_hash, expires_at) VALUES (:id, :token_hash, :csrf_hash, :expiry)"), {
        "id": uuid4(), "token_hash": _hash(session_token), "csrf_hash": _hash(csrf_token),
        "expiry": datetime.now(timezone.utc) + timedelta(hours=12),
    })
    return session_token, csrf_token


def _consume_bootstrap(db: Session, token: str, expires_at: datetime) -> None:
    token_hash = _hash(token)
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:bootstrap_hash))"), {"bootstrap_hash": token_hash})
    used = db.execute(text("SELECT 1 FROM owner_sessions WHERE token_hash = :hash"), {"hash": token_hash}).scalar_one_or_none()
    if used:
        raise DomainError("forbidden", 401)
    # A revoked, unguessable marker makes bootstrap consumption survive service restarts.
    db.execute(text("INSERT INTO owner_sessions (id, token_hash, csrf_hash, expires_at, revoked_at) VALUES (:id, :hash, :csrf, :expiry, now())"), {
        "id": uuid4(), "hash": token_hash, "csrf": _hash(secrets.token_urlsafe(32)), "expiry": expires_at,
    })


def authenticate_owner_session(db: Session, token: str) -> tuple[Principal, str]:
    row = db.execute(text("SELECT id, csrf_hash FROM owner_sessions WHERE token_hash = :hash AND revoked_at IS NULL AND expires_at > now()"), {
        "hash": _hash(token),
    }).one_or_none()
    if row is None:
        raise DomainError("forbidden", 401)
    return Principal(identity=UUID(int=0), kind="owner"), row.csrf_hash.strip()


def _rotate_owner_csrf(db: Session, token: str) -> str:
    csrf_token = secrets.token_urlsafe(32)
    updated = db.execute(text("""
        UPDATE owner_sessions SET csrf_hash = :csrf_hash
        WHERE token_hash = :token_hash AND revoked_at IS NULL AND expires_at > now()
        RETURNING id
    """), {"csrf_hash": _hash(csrf_token), "token_hash": _hash(token)}).scalar_one_or_none()
    if not updated:
        raise DomainError("forbidden", 401)
    return csrf_token


def _owner(principal: Principal) -> None:
    if principal.kind != "owner":
        raise DomainError("forbidden", 403)


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def verify_csrf(expected_hash: str, token: str) -> bool:
    return bool(token) and hmac.compare_digest(expected_hash, _hash(token))
