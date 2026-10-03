"""Project file versions and owner-controlled publication."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from io import BytesIO
from pathlib import PurePath
from typing import BinaryIO, Literal
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from scientist.auth import DomainError, authorize
from scientist.contracts import ObjectRef, Principal
from scientist.objects import MAX_UPLOAD_BYTES, lock_object, open_verified, put

_TYPES = {
    ".pdf": ("application/pdf", b"%PDF-"),
    ".csv": ("text/csv", None),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"PK\x03\x04"),
    ".json": ("application/json", None),
    ".txt": ("text/plain", None),
    ".md": ("text/markdown", None),
}


def attach(db: Session, principal: Principal, project_id: UUID, filename: str, content: BinaryIO) -> UUID:
    authorize(db, principal, "file:attach", project_id)
    if not filename or len(filename) > 255 or PurePath(filename).name != filename or re.search(r"[\x00-\x1f\x7f]", filename):
        raise DomainError("invalid_file", 400)
    suffix = PurePath(filename).suffix.lower()
    if suffix not in _TYPES:
        raise DomainError("unsupported_file_type", 415)
    body = content.read(MAX_UPLOAD_BYTES + 1)
    if len(body) > MAX_UPLOAD_BYTES:
        raise DomainError("request_too_large", 413)
    content_type, signature = _TYPES[suffix]
    if signature and not body.startswith(signature):
        raise DomainError("file_type_mismatch", 415)
    if not signature and suffix in {".txt", ".md", ".csv", ".json"}:
        try:
            body.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            raise DomainError("file_type_mismatch", 415) from exc
    # Preserve the attempt even when the object service is unavailable.
    file_id = uuid4()
    db.execute(sql_text("""
        INSERT INTO file_versions (id, project_id, filename, size, content_type, state)
        VALUES (:id, :project, :filename, :size, :content_type, 'uploading')
    """), {"id": file_id, "project": project_id, "filename": filename,
          "size": len(body), "content_type": content_type})
    try:
        ref = put(db, project_id, BytesIO(body), content_type)
    except DomainError:
        db.execute(sql_text("UPDATE file_versions SET state = 'failed', error_code = 'storage_unavailable' WHERE id = :id"), {"id": file_id})
        raise
    db.execute(sql_text("""
        UPDATE file_versions SET object_key = :key, sha256 = :sha, state = 'preparing'
        WHERE id = :id AND state = 'uploading'
    """), {"key": ref.key, "sha": ref.sha256, "id": file_id})
    return file_id


def mark_prepared(db: Session, file_id: UUID, extracted: ObjectRef, status: Literal["ready", "failed"]) -> None:
    if status not in {"ready", "failed"}:
        raise ValueError("invalid preparation status")
    row = db.execute(sql_text("SELECT project_id, filename, state, object_key FROM file_versions WHERE id = :id FOR UPDATE"), {"id": file_id}).one_or_none()
    if row is None or row.state != "preparing" or extracted.project_id != row.project_id:
        raise DomainError("not_found", 404)
    if status == "ready":
        lock_object(db, row.project_id, extracted.key)
        try:
            with open_verified(extracted) as stream:
                stream.read()
        except Exception as exc:
            db.execute(sql_text("UPDATE file_versions SET state = 'failed', error_code = 'storage_unavailable' WHERE id = :id"), {"id": file_id})
            raise DomainError("storage_unavailable", 503) from exc
        extracted_artifact_id = uuid4()
        db.execute(sql_text("""
            INSERT INTO artifacts (id, project_id, title, kind, object_key, sha256, size, content_type)
            VALUES (:id, :project, :title, 'file', :key, :sha, :size, :content_type)
        """), {"id": extracted_artifact_id, "project": row.project_id,
              "title": f"{row.filename} (extracted)", "key": extracted.key,
              "sha": extracted.sha256, "size": extracted.size, "content_type": "text/plain"})
        db.execute(sql_text("UPDATE file_versions SET state = 'ready', error_code = NULL, extracted_artifact_id = :artifact WHERE id = :id"),
                   {"artifact": extracted_artifact_id, "id": file_id})
    else:
        db.execute(sql_text("UPDATE file_versions SET state = 'failed', error_code = 'preparation_failed' WHERE id = :id"),
                   {"id": file_id})


def tombstone(db: Session, owner: Principal, file_id: UUID) -> None:
    """Hide a shared version without removing its retained bytes or provenance."""
    if owner.kind != "owner":
        raise DomainError("forbidden", 403)
    row = db.execute(sql_text("SELECT project_id, tombstoned_at FROM file_versions WHERE id = :id FOR UPDATE"),
                     {"id": file_id}).one_or_none()
    if row is None:
        raise DomainError("not_found", 404)
    if row.tombstoned_at is None:
        db.execute(sql_text("UPDATE file_versions SET tombstoned_at = now() WHERE id = :id"), {"id": file_id})
        db.execute(sql_text("UPDATE projects SET revision = revision + 1 WHERE id = :project"), {"project": row.project_id})


def publish(db: Session, owner: Principal, run_id: UUID, object_keys: list[str],
            expected_project_revision: int, publication_key: str) -> list[UUID]:
    if owner.kind != "owner":
        raise DomainError("forbidden", 403)
    if not 1 <= len(publication_key) <= 200 or len(object_keys) > 1000 or len(set(object_keys)) != len(object_keys):
        raise DomainError("idempotency_conflict", 409)
    run = db.execute(sql_text("SELECT project_id FROM runs WHERE id = :run AND state = 'completed'"), {"run": run_id}).one_or_none()
    if run is None:
        raise DomainError("not_found", 404)
    project_id = run.project_id
    payload_hash = sha256(json.dumps({"run": str(run_id), "keys": object_keys}, sort_keys=True).encode()).hexdigest()
    db.execute(sql_text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"), {"lock_key": f"publish:{project_id}:{publication_key}"})
    previous = db.execute(sql_text("SELECT payload_hash FROM publication_requests WHERE project_id = :project AND publication_key = :key"),
                           {"project": project_id, "key": publication_key}).scalar_one_or_none()
    if previous is not None:
        if previous.strip() != payload_hash:
            raise DomainError("idempotency_conflict", 409)
        return [uuid5(NAMESPACE_URL, f"scientist:{project_id}:{publication_key}:{key}") for key in object_keys]
    project = db.execute(sql_text("SELECT revision FROM projects WHERE id = :id FOR UPDATE"), {"id": project_id}).one()
    if project.revision != expected_project_revision:
        raise DomainError("revision_conflict", 409)
    records = db.execute(sql_text("""
        SELECT id, object_key, title, sha256, size, content_type FROM artifacts
        WHERE run_id = :run AND project_id = :project AND object_key = ANY(:keys)
    """), {"run": run_id, "project": project_id, "keys": object_keys}).mappings().all()
    if len(records) != len(object_keys):
        raise DomainError("not_found", 404)
    records_by_key = {item["object_key"]: item for item in records}
    ids: list[UUID] = []
    for key in object_keys:
        item = records_by_key[key]
        ref = ObjectRef(project_id=project_id, key=key, sha256=item["sha256"].strip(), size=item["size"], content_type="application/octet-stream")
        try:
            with open_verified(ref) as stream:
                stream.read()
        except Exception as exc:
            raise DomainError("storage_unavailable", 503) from exc
        version_id = uuid5(NAMESPACE_URL, f"scientist:{project_id}:{publication_key}:{key}")
        db.execute(sql_text("""
            INSERT INTO file_versions (id, project_id, filename, object_key, size, content_type, state, sha256, source_artifact_id)
            VALUES (:id, :project, :filename, :key, :size, :content_type, 'ready', :sha, :source_artifact)
        """), {"id": version_id, "project": project_id, "filename": item["title"][:255], "key": key,
              "size": item["size"], "content_type": item["content_type"], "sha": item["sha256"], "source_artifact": item["id"]})
        ids.append(version_id)
    db.execute(sql_text("INSERT INTO publication_requests (id, project_id, publication_key, payload_hash) VALUES (:id, :project, :key, :hash)"),
               {"id": uuid4(), "project": project_id, "key": publication_key, "hash": payload_hash})
    db.execute(sql_text("UPDATE projects SET revision = revision + 1 WHERE id = :id"), {"id": project_id})
    return ids
