from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from scientist import objects
from scientist.contracts import ObjectRef


class MemoryS3:
    def __init__(self):
        self.data: dict[tuple[str, str], bytes] = {}
        self.actions: list[str] = []

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.actions.append("put_object")
        slot = (Bucket, Key)
        self.data[slot] = bytes(Body)

    def get_object(self, *, Bucket, Key):
        self.actions.append("get_object")
        if (Bucket, Key) not in self.data:
            raise ClientError({"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "GetObject")
        return {"Body": BytesIO(self.data[(Bucket, Key)])}

    def head_object(self, *, Bucket, Key):
        self.actions.append("head_object")
        if (Bucket, Key) not in self.data:
            raise ClientError({"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
        body = self.data[(Bucket, Key)]
        return {"ContentLength": len(body)}

    def delete_object(self, *, Bucket, Key):
        self.actions.append("delete_object")
        self.data.pop((Bucket, Key), None)


@pytest.fixture
def object_fixture(monkeypatch):
    client = MemoryS3()
    monkeypatch.setattr(objects, "_client", lambda: client)
    return client


def test_put_creates_project_scoped_content_addressed_reference(db, project_session, object_fixture):
    project_id, _ = project_session
    body = b"immutable scientific input"
    ref = objects.put(db, project_id, BytesIO(body), "text/plain")
    assert ref.key == f"{project_id}/{sha256(body).hexdigest()}"
    assert ref.sha256 == sha256(body).hexdigest()
    assert ref.size == len(body)
    stored = db.execute(objects.sql_text("SELECT key, sha256, size, content_type FROM stored_objects WHERE key = :key"), {"key": ref.key}).one()
    assert (stored.sha256.strip(), stored.size, stored.content_type) == (ref.sha256, ref.size, "application/octet-stream")
    assert ref.content_type == "application/octet-stream"


def test_corrupt_object_is_rejected(db, project_session, object_fixture):
    project_id, _ = project_session
    ref = objects.put(db, project_id, BytesIO(b"original"), "text/plain")
    object_fixture.data[(objects.BUCKET, ref.key)] = b"tampered"
    with pytest.raises(objects.StorageIntegrityError):
        with objects.open_verified(ref) as stream:
            stream.read()


def test_delete_refuses_an_object_referenced_by_file_version(db, project_session, object_fixture):
    project_id, _ = project_session
    ref = objects.put(db, project_id, BytesIO(b"keep"), "text/plain")
    db.execute(
        objects.sql_text("INSERT INTO file_versions (id, project_id, filename, object_key, size, content_type, state, sha256) VALUES (:id, :project, 'x.txt', :key, :size, 'text/plain', 'ready', :sha)"),
        {"id": uuid4(), "project": project_id, "key": ref.key, "size": ref.size, "sha": ref.sha256},
    )
    assert objects.delete_unreferenced(db, ref.key) is False


def test_gc_deletes_unreferenced_object_and_registry_row(db, project_session, object_fixture):
    project_id, _ = project_session
    ref = objects.put(db, project_id, BytesIO(b"orphan"), "text/plain")
    assert objects.delete_unreferenced(db, ref.key) is True
    assert (objects.BUCKET, ref.key) not in object_fixture.data
    assert db.execute(objects.sql_text("SELECT 1 FROM stored_objects WHERE key = :key"), {"key": ref.key}).scalar_one_or_none() is None


def test_storage_boundary_uses_only_typed_basic_object_operations(db, project_session, object_fixture):
    project_id, _ = project_session
    ref = objects.put(db, project_id, BytesIO(b"allowlisted"), "text/plain")
    with objects.open_verified(ref) as stream:
        assert stream.read() == b"allowlisted"
    assert set(object_fixture.actions) <= {"head_object", "put_object", "get_object", "delete_object"}
