from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from botocore.exceptions import ClientError

from scientist import files, objects
from scientist.auth import DomainError
from scientist.contracts import Principal
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from runtime.prepare import PreparationError, prepare_local_file


class MemoryS3:
    def __init__(self):
        self.data = {}

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.data[(Bucket, Key)] = bytes(Body)

    def get_object(self, *, Bucket, Key):
        from io import BytesIO
        return {"Body": BytesIO(self.data[(Bucket, Key)])}

    def head_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.data:
            raise ClientError({"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
        return {"ContentLength": len(self.data[(Bucket, Key)])}

    def delete_object(self, *, Bucket, Key):
        self.data.pop((Bucket, Key), None)


@pytest.fixture
def setup_storage(monkeypatch):
    client = MemoryS3()
    monkeypatch.setattr(objects, "_client", lambda: client)
    return client


def owner() -> Principal:
    return Principal(identity=UUID(int=0), kind="owner")


def completed_run(db, project_id, session_id, key: str):
    from scientist.domain import submit_run
    run = submit_run(db, owner(), project_id, session_id, key, "test", [], uuid4(), "fixture")
    db.execute(files.sql_text("UPDATE runs SET state = 'completed' WHERE id = :run"), {"run": run.run_id})
    return run


def test_attach_transitions_from_uploading_to_preparing(db, project_session, setup_storage):
    project_id, _ = project_session
    file_id = files.attach(db, owner(), project_id, "notes.txt", BytesIO(b"hello"))
    row = db.execute(files.sql_text("SELECT state, sha256 FROM file_versions WHERE id = :id"), {"id": file_id}).one()
    assert row.state == "preparing"
    assert row.sha256.strip()


def test_mark_prepared_links_extracted_artifact_to_source_version(db, project_session, setup_storage):
    project_id, _ = project_session
    file_id = files.attach(db, owner(), project_id, "notes.txt", BytesIO(b"source"))
    extracted = objects.put(db, project_id, BytesIO(b"bounded extraction"), "text/plain")
    files.mark_prepared(db, file_id, extracted, "ready")
    row = db.execute(files.sql_text("SELECT state, extracted_artifact_id FROM file_versions WHERE id = :id"), {"id": file_id}).one()
    artifact = db.execute(files.sql_text("SELECT object_key FROM artifacts WHERE id = :id"), {"id": row.extracted_artifact_id}).scalar_one()
    assert row.state == "ready"
    assert artifact == extracted.key


def test_prepare_holds_object_lock_until_extracted_artifact_reference_commits(db, project_session, setup_storage, monkeypatch):
    from scientist.db import session
    project_id, _ = project_session
    file_id = files.attach(db, owner(), project_id, "notes.txt", BytesIO(b"source"))
    extracted = objects.put(db, project_id, BytesIO(b"race target"), "text/plain")
    db.commit()
    entered_verify, allow_verify, gc_started, gc_returned = Event(), Event(), Event(), Event()
    original_open = files.open_verified

    @contextmanager
    def paused_open(ref):
        entered_verify.set()
        if not allow_verify.wait(5):
            raise TimeoutError("test did not release verification barrier")
        with original_open(ref) as stream:
            yield stream

    monkeypatch.setattr(files, "open_verified", paused_open)

    def prepare():
        with session() as preparation_db:
            files.mark_prepared(preparation_db, file_id, extracted, "ready")
            preparation_db.commit()

    def collect():
        with session() as gc_db:
            gc_started.set()
            deleted = objects.delete_unreferenced(gc_db, extracted.key)
            gc_db.commit()
            gc_returned.set()
            return deleted

    with ThreadPoolExecutor(max_workers=2) as pool:
        preparing = pool.submit(prepare)
        assert entered_verify.wait(5)
        lock_name = f"object:{project_id}:{extracted.key}"
        with session() as probe_db:
            lock_available = probe_db.execute(files.sql_text(
                "SELECT pg_try_advisory_xact_lock(hashtext(:key))"
            ), {"key": lock_name}).scalar_one()
        assert lock_available is False, "preparation did not hold the object transaction lock"
        collecting = pool.submit(collect)
        assert gc_started.wait(5)
        try:
            assert not gc_returned.wait(0.15), "GC passed the preparation object's transaction lock"
        finally:
            allow_verify.set()
        preparing.result(timeout=5)
        assert collecting.result(timeout=5) is False


def test_external_attach_requires_project_grant(db, project_session, setup_storage):
    project_id, _ = project_session
    external = Principal(identity=uuid4(), kind="external")
    with pytest.raises(DomainError):
        files.attach(db, external, project_id, "notes.txt", BytesIO(b"hello"))


def test_interrupted_upload_leaves_failed_file_state(db, project_session, setup_storage, monkeypatch):
    project_id, _ = project_session

    class Interrupted(MemoryS3):
        def put_object(self, **kwargs):
            raise OSError("synthetic interrupted upload")

    monkeypatch.setattr(objects, "_client", lambda: Interrupted())
    with pytest.raises(DomainError, match="storage_unavailable"):
        files.attach(db, owner(), project_id, "notes.txt", BytesIO(b"hello"))
    row = db.execute(files.sql_text("SELECT state, error_code FROM file_versions WHERE project_id = :project ORDER BY created_at DESC LIMIT 1"),
                     {"project": project_id}).one()
    assert (row.state, row.error_code) == ("failed", "storage_unavailable")


def test_tombstone_hides_file_but_gc_retains_its_source_bytes(db, project_session, setup_storage):
    project_id, _ = project_session
    file_id = files.attach(db, owner(), project_id, "notes.txt", BytesIO(b"retained"))
    key = db.execute(files.sql_text("SELECT object_key FROM file_versions WHERE id = :id"), {"id": file_id}).scalar_one()
    files.tombstone(db, owner(), file_id)
    assert db.execute(files.sql_text("SELECT tombstoned_at IS NOT NULL FROM file_versions WHERE id = :id"), {"id": file_id}).scalar_one()
    assert objects.delete_unreferenced(db, key) is False


def test_publication_rejects_stale_project_revision(db, project_session, setup_storage):
    project_id, session_id = project_session
    run = completed_run(db, project_id, session_id, f"prepare-pub-{uuid4()}")
    with pytest.raises(DomainError, match="revision_conflict"):
        files.publish(db, owner(), run.run_id, [], expected_project_revision=99, publication_key="pub-1")


def test_concurrent_publication_replay_returns_same_version_ids(db, project_session, setup_storage):
    from scientist.db import session
    project_id, session_id = project_session
    run = completed_run(db, project_id, session_id, f"concurrent-pub-{uuid4()}")
    body = b"verified report bytes"
    ref = objects.put(db, project_id, BytesIO(body), "text/plain")
    artifact_id = uuid4()
    db.execute(files.sql_text("""
        INSERT INTO artifacts (id, project_id, run_id, title, kind, object_key, sha256, size, content_type)
        VALUES (:id, :project, :run, 'report.txt', 'report', :key, :sha, :size, 'text/plain')
    """), {"id": artifact_id, "project": project_id, "run": run.run_id, "key": ref.key,
          "sha": ref.sha256, "size": ref.size})
    db.commit()  # Make setup visible to independent PostgreSQL transactions.

    def publish_once():
        with session() as concurrent_db:
            result = files.publish(concurrent_db, owner(), run.run_id, [ref.key], 1, "same-key")
            concurrent_db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: publish_once(), range(2)))
    assert first == second
    assert db.execute(files.sql_text("SELECT count(*) FROM file_versions WHERE project_id = :p"), {"p": project_id}).scalar_one() == 1
    assert db.execute(files.sql_text("SELECT source_artifact_id FROM file_versions WHERE project_id = :p"), {"p": project_id}).scalar_one() == artifact_id
    assert db.execute(files.sql_text("SELECT revision FROM projects WHERE id = :p"), {"p": project_id}).scalar_one() == 2


def test_concurrent_publications_cannot_both_use_same_project_revision(db, project_session):
    from scientist.db import session
    project_id, session_id = project_session
    run = completed_run(db, project_id, session_id, f"revision-race-{uuid4()}")
    db.commit()

    def publish_once(key):
        with session() as concurrent_db:
            try:
                result = files.publish(concurrent_db, owner(), run.run_id, [], 1, key)
                concurrent_db.commit()
                return result
            except DomainError as exc:
                concurrent_db.rollback()
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(publish_once, ("revision-a", "revision-b")))
    assert sorted([first == "revision_conflict", second == "revision_conflict"]) == [False, True]
    assert db.execute(files.sql_text("SELECT revision FROM projects WHERE id = :p"), {"p": project_id}).scalar_one() == 2


def test_publication_key_cannot_replay_changed_artifacts(db, project_session, setup_storage):
    project_id, session_id = project_session
    run = completed_run(db, project_id, session_id, f"changed-pub-{uuid4()}")
    first = files.publish(db, owner(), run.run_id, [], 1, "same-key")
    assert first == []
    with pytest.raises(DomainError, match="idempotency_conflict"):
        files.publish(db, owner(), run.run_id, ["different"], 1, "same-key")


def test_publication_replay_preserves_caller_object_order(db, project_session, setup_storage):
    project_id, session_id = project_session
    run = completed_run(db, project_id, session_id, f"ordered-pub-{uuid4()}")
    refs = [objects.put(db, project_id, BytesIO(body), "text/plain") for body in (b"first", b"second")]
    for index, ref in enumerate(refs):
        db.execute(files.sql_text("""
            INSERT INTO artifacts (id, project_id, run_id, title, kind, object_key, sha256, size, content_type)
            VALUES (:id, :project, :run, :title, 'report', :key, :sha, :size, 'text/plain')
        """), {"id": uuid4(), "project": project_id, "run": run.run_id, "title": f"{index}.txt",
              "key": ref.key, "sha": ref.sha256, "size": ref.size})
    keys = sorted((ref.key for ref in refs), reverse=True)
    first = files.publish(db, owner(), run.run_id, keys, 1, "ordered-key")
    second = files.publish(db, owner(), run.run_id, keys, 1, "ordered-key")
    assert first == second
    assert [
        db.execute(files.sql_text("SELECT source_artifact_id FROM file_versions WHERE id = :id"), {"id": version_id}).scalar_one()
        for version_id in first
    ] == [
        db.execute(files.sql_text("SELECT id FROM artifacts WHERE object_key = :key"), {"key": key}).scalar_one()
        for key in keys
    ]


def test_json_depth_is_bounded(tmp_path):
    path = tmp_path / "deep.json"
    path.write_text("[" * 80 + "0" + "]" * 80)
    with pytest.raises(PreparationError, match="json_depth"):
        prepare_local_file(path, tmp_path / "out")


def test_xlsx_processing_fails_closed_until_isolated_runner_exists(tmp_path):
    from zipfile import ZipFile
    path = tmp_path / "sheet.xlsx"
    with ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    with pytest.raises(PreparationError, match="sandbox_required"):
        prepare_local_file(path, tmp_path / "out")


def test_mismatched_extension_mime_is_rejected(tmp_path):
    path = tmp_path / "data.csv"
    path.write_bytes(b"%PDF-1.7\n")
    with pytest.raises(PreparationError, match="type_mismatch"):
        prepare_local_file(path, tmp_path / "out")


def test_corrupt_pdf_is_rejected_before_sandbox_dispatch(tmp_path):
    path = tmp_path / "corrupt.pdf"
    path.write_bytes(b"%PDF-1.7\ntruncated")
    with pytest.raises(PreparationError, match="invalid_pdf"):
        prepare_local_file(path, tmp_path / "out")


def test_xlsx_traversal_member_is_rejected(tmp_path):
    from zipfile import ZipFile
    path = tmp_path / "hostile.xlsx"
    with ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("../outside.txt", "synthetic")
    with pytest.raises(PreparationError, match="unsafe_archive_path"):
        prepare_local_file(path, tmp_path / "out")


@pytest.mark.parametrize(
    ("member", "payload", "error"),
    [
        ("xl/vbaProject.bin", b"synthetic", "active_content"),
        ("xl/externalLinks/externalLink1.xml", b"synthetic", "active_content"),
    ],
)
def test_xlsx_active_content_is_rejected(tmp_path, member, payload, error):
    from zipfile import ZipFile
    path = tmp_path / "active.xlsx"
    with ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(member, payload)
    with pytest.raises(PreparationError, match=error):
        prepare_local_file(path, tmp_path / "out")


def test_xlsx_member_count_and_expanded_size_are_bounded(tmp_path, monkeypatch):
    from zipfile import ZipFile
    import runtime.prepare as preparation
    path = tmp_path / "many.xlsx"
    with ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/one.xml", "x" * 100)
    monkeypatch.setattr(preparation, "MAX_ZIP_MEMBERS", 1)
    with pytest.raises(PreparationError, match="archive_limit"):
        prepare_local_file(path, tmp_path / "out")
    monkeypatch.setattr(preparation, "MAX_ZIP_MEMBERS", 10)
    monkeypatch.setattr(preparation, "MAX_ZIP_EXPANDED", 50)
    with pytest.raises(PreparationError, match="archive_limit"):
        prepare_local_file(path, tmp_path / "out")


def test_xlsx_symlink_member_is_rejected(tmp_path):
    from stat import S_IFLNK
    from zipfile import ZipFile, ZipInfo
    path = tmp_path / "link.xlsx"
    link = ZipInfo("xl/link")
    link.external_attr = (S_IFLNK | 0o777) << 16
    with ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(link, "../../outside")
    with pytest.raises(PreparationError, match="unsafe_archive_path"):
        prepare_local_file(path, tmp_path / "out")


def test_csv_formula_cells_are_data_and_preserved_verbatim(tmp_path):
    path = tmp_path / "formulas.csv"
    path.write_text("value\n=1+1\n", encoding="utf-8")
    result = prepare_local_file(path, tmp_path / "out")
    assert json.loads(Path(result["path"]).read_text()) == [["value"], ["=1+1"]]


def test_existing_output_symlink_is_not_followed(tmp_path):
    path = tmp_path / "data.txt"
    path.write_text("new", encoding="utf-8")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("preserve", encoding="utf-8")
    (output_dir / "extracted.txt").symlink_to(victim)
    with pytest.raises(PreparationError, match="output_exists"):
        prepare_local_file(path, output_dir)
    assert victim.read_text() == "preserve"


def test_csv_rejects_pdf_signature(tmp_path):
    path = tmp_path / "data.csv"
    path.write_bytes(b"%PDF-1.7\n")
    with pytest.raises(PreparationError, match="type_mismatch"):
        prepare_local_file(path, tmp_path / "out")


def test_csv_preserves_newlines_inside_quoted_fields(tmp_path):
    path = tmp_path / "quoted.csv"
    path.write_text('title,notes\nexample,"first line\nsecond line"\n', encoding="utf-8")
    result = prepare_local_file(path, tmp_path / "out")
    assert json.loads(Path(result["path"]).read_text()) == [["title", "notes"], ["example", "first line\nsecond line"]]


def test_same_bytes_can_have_distinct_logical_file_types(db, project_session, setup_storage):
    project_id, _ = project_session
    body = b"same UTF-8 bytes"
    plain_id = files.attach(db, owner(), project_id, "same.txt", BytesIO(body))
    markdown_id = files.attach(db, owner(), project_id, "same.md", BytesIO(body))
    rows = db.execute(files.sql_text("SELECT object_key, content_type FROM file_versions WHERE id = ANY(:ids) ORDER BY filename"),
                      {"ids": [plain_id, markdown_id]}).all()
    assert rows[0].object_key == rows[1].object_key
    assert [row.content_type for row in rows] == ["text/markdown", "text/plain"]
