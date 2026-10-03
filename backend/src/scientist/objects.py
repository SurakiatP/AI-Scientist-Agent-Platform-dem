"""Private, typed access to immutable MinIO/S3 objects."""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from hashlib import sha256
from typing import BinaryIO, Iterator
from uuid import UUID

import boto3
from botocore.exceptions import ClientError
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from scientist.auth import DomainError
from scientist.contracts import ObjectRef

BUCKET = os.environ.get("SCIENTIST_OBJECT_BUCKET", "scientist")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_CHUNK = 64 * 1024
_CANONICAL_CONTENT_TYPE = "application/octet-stream"
_configured_client = None


class StorageIntegrityError(Exception):
    pass


def _client():
    """Build the sole client; callers can use only this module's typed methods."""
    if _configured_client is not None:
        return _configured_client
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("SCIENTIST_S3_ENDPOINT", "http://127.0.0.1:9000"),
        aws_access_key_id=os.environ.get("SCIENTIST_S3_ACCESS_KEY"),
        aws_secret_access_key=os.environ.get("SCIENTIST_S3_SECRET_KEY"),
        region_name="us-east-1",
    )


def configure(client, bucket: str = BUCKET) -> None:
    """Inject an operator-created S3-compatible client for the isolated service harness."""
    global _configured_client, BUCKET
    if not bucket or len(bucket) > 63 or not bucket.replace("-", "").isalnum():
        raise ValueError("invalid storage bucket")
    _configured_client, BUCKET = client, bucket


def lock_object(db: Session, project_id: UUID, key: str) -> None:
    """Hold the same transaction lock used by upload and GC while adding a reference."""
    digest = key.removeprefix(f"{project_id}/")
    if key != f"{project_id}/{digest}" or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("invalid object key")
    db.execute(sql_text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"object:{project_id}:{key}"})


def put(db: Session, project_id: UUID, content: BinaryIO, content_type: str) -> ObjectRef:
    """Upload one bounded byte stream at its project-scoped SHA-256 key."""
    if not content_type or len(content_type) > 255:
        raise ValueError("invalid content type")
    digest = sha256()
    size = 0
    with tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024, mode="w+b") as staged:
        while chunk := content.read(_CHUNK):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise DomainError("request_too_large", 413)
            digest.update(chunk)
            staged.write(chunk)
        hash_hex = digest.hexdigest()
        key = f"{project_id}/{hash_hex}"
        lock_object(db, project_id, key)
        staged.seek(0)
        try:
            client = _client()
            registered = db.execute(sql_text("SELECT project_id, sha256, size, content_type FROM stored_objects WHERE key = :key"),
                                    {"key": key}).one_or_none()
            if registered is not None and (registered.project_id != project_id or registered.sha256.strip() != hash_hex
                                            or registered.size != size or registered.content_type != _CANONICAL_CONTENT_TYPE):
                raise StorageIntegrityError("object registry metadata differs from content")
            try:
                client.head_object(Bucket=BUCKET, Key=key)
                exists = True
            except ClientError as exc:
                if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                    raise
                exists = False
            if not exists:
                client.put_object(Bucket=BUCKET, Key=key, Body=staged.read(), ContentType=_CANONICAL_CONTENT_TYPE)
            with open_verified(ObjectRef(project_id=project_id, key=key, sha256=hash_hex, size=size,
                                         content_type=_CANONICAL_CONTENT_TYPE)) as check:
                check.read()
            db.execute(sql_text("""
                INSERT INTO stored_objects (key, project_id, sha256, size, content_type)
                VALUES (:key, :project, :sha, :size, :content_type) ON CONFLICT (key) DO NOTHING
            """), {"key": key, "project": project_id, "sha": hash_hex, "size": size, "content_type": _CANONICAL_CONTENT_TYPE})
            registered = db.execute(sql_text("SELECT project_id, sha256, size, content_type FROM stored_objects WHERE key = :key"),
                                    {"key": key}).one()
            if registered.project_id != project_id or registered.sha256.strip() != hash_hex or registered.size != size or registered.content_type != _CANONICAL_CONTENT_TYPE:
                raise StorageIntegrityError("object registry metadata differs from content")
        except StorageIntegrityError:
            raise
        except Exception as exc:
            raise DomainError("storage_unavailable", 503) from exc
    return ObjectRef(project_id=project_id, key=key, sha256=hash_hex, size=size, content_type=_CANONICAL_CONTENT_TYPE)


@contextmanager
def open_verified(ref: ObjectRef) -> Iterator[BinaryIO]:
    """Return bytes only after checking both recorded size and content digest."""
    if ref.key != f"{ref.project_id}/{ref.sha256}" or ref.size > MAX_UPLOAD_BYTES:
        raise StorageIntegrityError("invalid object reference")
    try:
        response = _client().get_object(Bucket=BUCKET, Key=ref.key)
        body = response["Body"]
        try:
            with tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024, mode="w+b") as staged:
                digest, size = sha256(), 0
                while chunk := body.read(_CHUNK):
                    size += len(chunk)
                    if size > ref.size or size > MAX_UPLOAD_BYTES:
                        raise StorageIntegrityError("stored object size differs from reference")
                    digest.update(chunk)
                    staged.write(chunk)
                if size != ref.size or digest.hexdigest() != ref.sha256:
                    raise StorageIntegrityError("stored object hash differs from reference")
                staged.seek(0)
                yield staged
        finally:
            body.close()
    except StorageIntegrityError:
        raise
    except Exception as exc:
        raise DomainError("storage_unavailable", 503) from exc


def delete_unreferenced(db: Session, key: str) -> bool:
    """Delete only when all durable file, artifact, snapshot, and checkpoint refs are absent."""
    try:
        project_text, digest = key.split("/", 1)
        project_id = UUID(project_text)
    except (AttributeError, ValueError) as exc:
        raise ValueError("invalid object key") from exc
    if len(key) > 2048 or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("invalid object key")
    lock_object(db, project_id, key)
    referenced = db.execute(sql_text("""
        SELECT EXISTS (SELECT 1 FROM file_versions WHERE object_key = :key)
            OR EXISTS (SELECT 1 FROM artifacts WHERE object_key = :key)
            OR EXISTS (SELECT 1 FROM input_snapshots WHERE position(:key in manifest::text) > 0)
            OR EXISTS (SELECT 1 FROM checkpoints WHERE position(:key in manifest::text) > 0)
            OR EXISTS (SELECT 1 FROM operations WHERE position(:key in COALESCE(result::text, '')) > 0)
    """), {"key": key}).scalar_one()
    if referenced:
        return False
    try:
        _client().delete_object(Bucket=BUCKET, Key=key)
    except Exception as exc:
        raise DomainError("storage_unavailable", 503) from exc
    db.execute(sql_text("DELETE FROM stored_objects WHERE key = :key AND project_id = :project"), {"key": key, "project": project_id})
    return True
