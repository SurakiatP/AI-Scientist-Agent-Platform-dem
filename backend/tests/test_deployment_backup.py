from __future__ import annotations

import json
import hashlib
import io
import importlib.util
import os
import sys
from types import SimpleNamespace
from pathlib import Path
from uuid import uuid4

import pytest

from scientist.deployment_backup import RecoverySetError, restore_recovery_set


@pytest.fixture(scope="module")
def w2_recovery_acceptance():
    path = Path(__file__).parent / "live" / "w2_recovery_set_acceptance.py"
    spec = importlib.util.spec_from_file_location("w2_recovery_set_acceptance_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SOURCE_CHECK = (
    "CHECK (((kind)::text = ANY ((ARRAY["
    "'report''s'::character varying, 'table'::character varying"
    "])::text[])))"
)
_RESTORED_CHECK = (
    "CHECK (((kind)::text = ANY (ARRAY["
    "('report''s'::character varying)::text, ('table'::character varying)::text"
    "])))"
)
_SOURCE_INDEX = (
    "CREATE UNIQUE INDEX artifacts_kind_partial ON public.artifacts USING btree (run_id) "
    "WHERE ((kind)::text = ANY ((ARRAY["
    "'report''s'::character varying, 'table'::character varying"
    "])::text[]))"
)
_RESTORED_INDEX = (
    "CREATE UNIQUE INDEX artifacts_kind_partial ON public.artifacts USING btree (run_id) "
    "WHERE ((kind)::text = ANY (ARRAY["
    "('report''s'::character varying)::text, ('table'::character varying)::text"
    "]))"
)


class _SchemaRows:
    def __init__(self, rows):
        self.rows = rows
        self.fetched = False

    def fetchmany(self, _count):
        if self.fetched:
            return []
        self.fetched = True
        return self.rows


class _SchemaConnection:
    def __init__(self, check_definition, index_definition):
        self.check_definition = check_definition
        self.index_definition = index_definition

    def execution_options(self, **_options):
        return self

    def execute(self, statement):
        sql = str(statement)
        if "FROM pg_constraint" in sql:
            return _SchemaRows([("artifacts", "artifacts_kind_check", "c", self.check_definition,
                                 False, False, True)])
        if "FROM pg_indexes" in sql:
            return _SchemaRows([("artifacts", "artifacts_kind_partial", self.index_definition)])
        return _SchemaRows([])


def _schema_snapshot(w2_recovery_acceptance, check_definition, index_definition):
    return w2_recovery_acceptance._schema_snapshot(
        _SchemaConnection(check_definition, index_definition)
    )


class _Result:
    def __init__(self, scalar=True, row=None, rows=()):
        self.scalar = scalar
        self.row = row
        self.rows = rows

    def scalar_one(self):
        return self.scalar

    def one(self):
        return self.row

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class _SourceConnection:
    def __init__(self, object_rows=(), identity=("source", "127.0.0.1", "5432", "/var/lib/postgresql/data"),
                 relations=0, active_runs=0):
        self.object_rows = object_rows
        self.identity = identity
        self.relations = relations
        self.active_runs = active_runs

    def execution_options(self, **kwargs):
        return self

    def execute(self, statement):
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            return _Result(True)
        if "FROM runs" in sql:
            return _Result(self.active_runs)
        if "current_database()" in sql:
            return _Result(row=self.identity)
        if "pg_class" in sql:
            return _Result(self.relations)
        if "schema_migrations" in sql:
            from scientist.db import MIGRATION

            rows = [SimpleNamespace(version=path.stem, checksum=hashlib.sha256(path.read_bytes()).hexdigest())
                    for path in sorted(MIGRATION.parent.glob("[0-9]*.sql"))]
            return _Result(rows=rows)
        if "WITH object_refs" in sql:
            return _Result(rows=())
        if "stored_objects" in sql:
            return _Result(rows=self.object_rows)
        return _Result(0)

    def close(self):
        pass


class _Engine:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return self.connection

    def dispose(self):
        pass


def test_restore_rejects_manifest_with_unapproved_fields_before_target_changes(tmp_path, monkeypatch):
    recovery_set = tmp_path / "recovery"
    recovery_set.mkdir(mode=0o700)
    manifest_path = recovery_set / "manifest.json"
    manifest_path.write_text(
        json.dumps({"format": "scientist-recovery-set", "schema_version": 1, "unexpected": "value"}),
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)
    key_file = tmp_path / "backup.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    class S3:
        def __init__(self):
            self.calls = []

        def list_objects_v2(self, **kwargs):
            self.calls.append(kwargs)
            raise AssertionError("invalid manifest must fail before target bucket access")

    s3 = S3()
    monkeypatch.setattr("scientist.deployment_backup.create_engine", lambda *args, **kwargs: _Engine(_SourceConnection()))

    with pytest.raises(RecoverySetError, match="manifest"):
        restore_recovery_set(
            "postgresql+psycopg:///must_not_connect",
            target_bucket="fresh-target",
            s3_client=s3,
            recovery_set=recovery_set,
            key_file=key_file,
            pg_restore_path=Path(sys.executable),
        )
    assert s3.calls == []


def test_backup_rejects_object_bytes_that_disagree_with_registry(tmp_path, monkeypatch):
    project_id = str(uuid4())
    expected = b"recorded immutable object"
    digest = hashlib.sha256(expected).hexdigest()
    connection = _SourceConnection([(f"{project_id}/{digest}", project_id, digest, len(expected), "application/octet-stream")])
    monkeypatch.setattr("scientist.deployment_backup.create_engine", lambda *args, **kwargs: _Engine(connection))

    class S3:
        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(b"different object bytes")}

    def dump(binary, args, env, **kwargs):
        Path(args[args.index("--file") + 1]).write_bytes(b"test database archive")

    monkeypatch.setattr("scientist.deployment_backup._run_pg", dump)
    key_file = tmp_path / "backup.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)
    destination = tmp_path / "recovery"

    from scientist.deployment_backup import create_recovery_set

    with pytest.raises(RecoverySetError, match="object bytes"):
        create_recovery_set(
            "postgresql+psycopg:///source",
            bucket="source-bucket",
            s3_client=S3(),
            destination=destination,
            key_file=key_file,
            pg_dump_path=Path(sys.executable),
        )
    assert not destination.exists()


def test_backup_refuses_active_or_recovering_runs(tmp_path, monkeypatch):
    from scientist.deployment_backup import create_recovery_set

    connection = _SourceConnection(active_runs=1)
    monkeypatch.setattr("scientist.deployment_backup.create_engine", lambda *args, **kwargs: _Engine(connection))
    calls = []
    monkeypatch.setattr("scientist.deployment_backup._run_pg", lambda *args, **kwargs: calls.append(args))
    key_file = tmp_path / "backup.key"
    key_file.write_bytes(b"q" * 32)
    key_file.chmod(0o600)
    destination = tmp_path / "recovery"

    with pytest.raises(RecoverySetError, match="active runs"):
        create_recovery_set("postgresql+psycopg:///source", bucket="source-bucket", s3_client=object(),
                            destination=destination, key_file=key_file, pg_dump_path=Path(sys.executable))
    assert calls == []
    assert not destination.exists()


def _make_empty_recovery_set(tmp_path, monkeypatch):
    from scientist.deployment_backup import create_recovery_set

    key_file = tmp_path / "backup.key"
    key_file.write_bytes(b"z" * 32)
    key_file.chmod(0o600)
    monkeypatch.setattr("scientist.deployment_backup.create_engine", lambda *args, **kwargs: _Engine(_SourceConnection()))

    class S3:
        def list_objects_v2(self, **kwargs):
            return {"KeyCount": 0, "Contents": [], "IsTruncated": False}

    def dump(binary, args, env, **kwargs):
        Path(args[args.index("--file") + 1]).write_bytes(b"test database archive")

    monkeypatch.setattr("scientist.deployment_backup._run_pg", dump)
    recovery_set = tmp_path / "recovery"
    create_recovery_set("postgresql+psycopg:///source", bucket="source-bucket", s3_client=S3(),
                        destination=recovery_set, key_file=key_file, pg_dump_path=Path(sys.executable))
    return recovery_set, key_file


def test_restore_rejects_corrupt_database_ciphertext_before_pg_restore(tmp_path, monkeypatch):
    from scientist.deployment_backup import restore_recovery_set

    recovery_set, key_file = _make_empty_recovery_set(tmp_path, monkeypatch)
    database_file = recovery_set / "database.dump.enc"
    ciphertext = database_file.read_bytes()
    database_file.write_bytes(ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]))
    calls = []

    class S3:
        def list_objects_v2(self, **kwargs):
            return {"KeyCount": 0, "Contents": [], "IsTruncated": False}

    monkeypatch.setattr(
        "scientist.deployment_backup.create_engine",
        lambda *args, **kwargs: _Engine(_SourceConnection(identity=("target", "127.0.0.1", "5432", "/var/lib/postgresql/target"))),
    )
    monkeypatch.setattr("scientist.deployment_backup._run_pg", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(RecoverySetError, match="checksum mismatch|authentication failed"):
        restore_recovery_set("postgresql+psycopg:///target", target_bucket="target-bucket", s3_client=S3(),
                             recovery_set=recovery_set, key_file=key_file, pg_restore_path=Path(sys.executable))
    assert calls == []


def test_restore_rejects_source_database_and_bucket_as_targets(tmp_path, monkeypatch):
    from scientist.deployment_backup import restore_recovery_set

    recovery_set, key_file = _make_empty_recovery_set(tmp_path, monkeypatch)
    bucket_calls = []

    class S3:
        def list_objects_v2(self, **kwargs):
            bucket_calls.append(kwargs)
            return {"KeyCount": 0, "Contents": [], "IsTruncated": False}

    source_identity = ("source", "127.0.0.1", "5432", "/var/lib/postgresql/data")
    monkeypatch.setattr(
        "scientist.deployment_backup.create_engine",
        lambda *args, **kwargs: _Engine(_SourceConnection(identity=source_identity)),
    )
    with pytest.raises(RecoverySetError, match="source database"):
        restore_recovery_set("postgresql+psycopg:///source", target_bucket="other-bucket", s3_client=S3(),
                             recovery_set=recovery_set, key_file=key_file, pg_restore_path=Path(sys.executable))

    target = _SourceConnection(identity=("target", "127.0.0.1", "5432", "/var/lib/postgresql/target"), relations=1)
    monkeypatch.setattr("scientist.deployment_backup.create_engine", lambda *args, **kwargs: _Engine(target))
    with pytest.raises(RecoverySetError, match="empty application schema"):
        restore_recovery_set("postgresql+psycopg:///target", target_bucket="other-bucket", s3_client=S3(),
                             recovery_set=recovery_set, key_file=key_file, pg_restore_path=Path(sys.executable))
    target.relations = 0
    with pytest.raises(RecoverySetError, match="bucket must differ"):
        restore_recovery_set("postgresql+psycopg:///target", target_bucket="source-bucket", s3_client=S3(),
                             recovery_set=recovery_set, key_file=key_file, pg_restore_path=Path(sys.executable))
    assert bucket_calls == []


def test_restore_rejects_nonempty_target_bucket_before_database_restore(tmp_path, monkeypatch):
    from scientist.deployment_backup import restore_recovery_set

    recovery_set, key_file = _make_empty_recovery_set(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "scientist.deployment_backup.create_engine",
        lambda *args, **kwargs: _Engine(_SourceConnection(identity=("target", "127.0.0.1", "5432", "/var/lib/postgresql/target"))),
    )
    restore_calls = []
    monkeypatch.setattr("scientist.deployment_backup._run_pg", lambda *args, **kwargs: restore_calls.append(args))

    class S3:
        def list_objects_v2(self, **kwargs):
            return {"KeyCount": 1, "Contents": [{"Key": "existing"}], "IsTruncated": False}

    with pytest.raises(RecoverySetError, match="bucket must be empty"):
        restore_recovery_set("postgresql+psycopg:///target", target_bucket="target-bucket", s3_client=S3(),
                             recovery_set=recovery_set, key_file=key_file, pg_restore_path=Path(sys.executable))
    assert restore_calls == []


def test_restore_populates_verified_objects_after_database_restore(tmp_path, monkeypatch):
    from scientist.deployment_backup import create_recovery_set, restore_recovery_set

    project_id = str(uuid4())
    object_bytes = b"verified immutable result"
    digest = hashlib.sha256(object_bytes).hexdigest()
    object_key = f"{project_id}/{digest}"
    row = (object_key, project_id, digest, len(object_bytes), "application/octet-stream")
    source = _SourceConnection([row])
    target = _SourceConnection([row], identity=("target", "127.0.0.1", "5432", "/var/lib/postgresql/target"))
    monkeypatch.setattr("scientist.deployment_backup.create_engine", lambda *args, **kwargs: _Engine(source))

    class S3:
        def __init__(self):
            self.objects = {"source-bucket": {object_key: object_bytes}, "target-bucket": {}}

        def get_object(self, *, Bucket, Key):
            return {"Body": io.BytesIO(self.objects[Bucket][Key])}

        def list_objects_v2(self, *, Bucket, MaxKeys):
            items = self.objects[Bucket]
            return {"KeyCount": min(len(items), MaxKeys), "Contents": list(items)[:MaxKeys], "IsTruncated": len(items) > MaxKeys}

        def put_object(self, *, Bucket, Key, Body, ContentType):
            self.objects[Bucket][Key] = Body.read()

    s3 = S3()
    commands = []

    def pg(binary, args, env, **kwargs):
        commands.append(args)
        if "--file" in args:
            Path(args[args.index("--file") + 1]).write_bytes(b"test database archive")
        elif "--dbname" in args:
            restored_archives.append(Path(args[-1]).read_bytes())

    monkeypatch.setattr("scientist.deployment_backup._run_pg", pg)
    restored_archives = []
    key_file = tmp_path / "backup.key"
    key_file.write_bytes(b"r" * 32)
    key_file.chmod(0o600)
    recovery_set = tmp_path / "recovery"
    create_recovery_set("postgresql+psycopg:///source", bucket="source-bucket", s3_client=s3,
                        destination=recovery_set, key_file=key_file, pg_dump_path=Path(sys.executable))

    monkeypatch.setattr("scientist.deployment_backup.create_engine", lambda *args, **kwargs: _Engine(target))
    restore_recovery_set("postgresql+psycopg:///target", target_bucket="target-bucket", s3_client=s3,
                         recovery_set=recovery_set, key_file=key_file, pg_restore_path=Path(sys.executable))

    assert s3.objects["target-bucket"] == {object_key: object_bytes}
    restore_args = next(args for args in commands if "--dbname" in args)
    assert restore_args[restore_args.index("--dbname") + 1] == "target"
    assert restored_archives == [b"test database archive"]


@pytest.mark.skipif(
    not os.environ.get("SCIENTIST_BACKUP_TEST_DATABASE_URL"),
    reason="requires the dedicated pristine native PostgreSQL backup regression database",
)
def test_fresh_database_ignores_internal_namespaces_and_rejects_user_schema_objects():
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    from scientist.deployment_backup import RecoverySetError, _ensure_quiet_target

    class EmptyBucket:
        def list_objects_v2(self, **kwargs):
            return {"KeyCount": 0, "Contents": [], "IsTruncated": False}

    engine = create_engine(os.environ["SCIENTIST_BACKUP_TEST_DATABASE_URL"], poolclass=NullPool)
    schema = None
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                _ensure_quiet_target(connection, EmptyBucket(), "backup-target", "f" * 64, "source-bucket")
                schema = f"codex_backup_ns_{uuid4().hex}"
                connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                connection.execute(text(f'CREATE TABLE "{schema}".marker (id integer)'))
                with pytest.raises(RecoverySetError, match="empty application schema"):
                    _ensure_quiet_target(connection, EmptyBucket(), "backup-target", "f" * 64, "source-bucket")
            finally:
                if transaction.is_active:
                    transaction.rollback()
        assert schema is not None
        with engine.connect() as verification:
            assert verification.execute(text("SELECT to_regnamespace(:schema)"), {"schema": schema}).scalar_one() is None
    finally:
        engine.dispose()


def test_recovery_schema_snapshot_matches_postgres_array_cast_rewrite(w2_recovery_acceptance):
    source = _schema_snapshot(w2_recovery_acceptance, _SOURCE_CHECK, _SOURCE_INDEX)
    restored = _schema_snapshot(w2_recovery_acceptance, _RESTORED_CHECK, _RESTORED_INDEX)

    assert source == restored


@pytest.mark.parametrize(
    "definition",
    [
        "CHECK (description = 'ANY ((ARRAY[''report''::character varying])::text[])')",
        r"CHECK (description = E'ANY ((ARRAY[\'report\'::character varying])::text[])')",
        "CHECK (description = $fmt$ANY ((ARRAY['report'::character varying])::text[])$fmt$)",
        'CHECK ("ANY ((ARRAY[\'report\'::character varying])::text[])" = description)',
        "CHECK (description /* ANY ((ARRAY['report'::character varying])::text[]) */ = 'x')",
        "-- ANY ((ARRAY['report'::character varying])::text[])\nCHECK (description = 'x')",
    ],
    ids=("single-quoted", "escaped-e-string", "dollar-quoted", "quoted-identifier", "block-comment", "line-comment"),
)
def test_recovery_schema_normalization_preserves_quoted_and_comment_text(
    w2_recovery_acceptance, definition
):
    assert w2_recovery_acceptance._canonicalize_pg_varchar_text_array_cast(definition) == definition


def test_recovery_schema_normalization_preserves_unicode_dollar_quoted_values(w2_recovery_acceptance):
    canonicalize = w2_recovery_acceptance._canonicalize_pg_varchar_text_array_cast
    before = "CHECK (note = $é$before$é$)"
    after = "CHECK (note = $é$after$é$)"
    thai = "CHECK (note = $ไทย$before$ไทย$)"

    assert canonicalize(before) == before
    assert canonicalize(after) == after
    assert canonicalize(thai) == thai
    assert canonicalize(before) != canonicalize(after)
    assert canonicalize(before) != canonicalize(thai)


def test_recovery_schema_normalization_preserves_nested_comments(w2_recovery_acceptance):
    definition = "CHECK (note /* outer /* inner */ ANY ((ARRAY['report'::character varying])::text[]) */ = 'x')"

    assert w2_recovery_acceptance._canonicalize_pg_varchar_text_array_cast(definition) == definition


def test_recovery_schema_normalization_finds_code_after_ordinary_trailing_backslash(w2_recovery_acceptance):
    definition = r"CHECK (note = 'ends with backslash\' AND kind = ANY ((ARRAY['report'::character varying])::text[]))"
    expected = r"CHECK (note = 'ends with backslash\' AND kind = ANY (ARRAY[('report'::character varying)::text]))"

    assert w2_recovery_acceptance._canonicalize_pg_varchar_text_array_cast(definition) == expected


@pytest.mark.parametrize(
    ("check_definition", "index_definition"),
    [
        (_SOURCE_CHECK.replace("'report''s'", "'summary'", 1), _SOURCE_INDEX),
        (_SOURCE_CHECK.replace("'report''s'::character varying, 'table'", "'table'::character varying, 'report''s'", 1), _SOURCE_INDEX),
        (_SOURCE_CHECK.replace("(kind)::text", "(status)::text", 1), _SOURCE_INDEX),
        (_SOURCE_CHECK.replace(" = ANY", " <> ANY", 1), _SOURCE_INDEX),
        (_SOURCE_CHECK.replace("::character varying", "::varchar", 1), _SOURCE_INDEX),
        (_SOURCE_CHECK.replace("])::text[]", "])::varchar[]", 1), _SOURCE_INDEX),
        (_SOURCE_CHECK, _RESTORED_INDEX + " AND (project_id IS NOT NULL)"),
    ],
    ids=("literal", "literal-order", "column", "operator", "element-type", "array-type", "index-predicate"),
)
def test_recovery_schema_snapshot_rejects_non_cast_schema_changes(
    w2_recovery_acceptance, check_definition, index_definition
):
    changed = _schema_snapshot(w2_recovery_acceptance, check_definition, index_definition)
    restored = _schema_snapshot(w2_recovery_acceptance, _RESTORED_CHECK, _RESTORED_INDEX)

    assert changed != restored
