#!/usr/bin/env python3
"""Operator-run W2 encrypted PostgreSQL/object recovery acceptance.

No resources are provisioned by this harness. The operator must provide a
fresh local PostgreSQL database and empty MinIO bucket as the restore target,
plus a private JSON config. See ``--help`` and ``--self-check``. Credentials
for local MinIO are resolved by boto3's environment-only credential provider;
the config, evidence, and console output never contain credential values.
The JSON config has exactly these fields: source_database_url,
target_database_url, source_bucket, target_bucket, s3_endpoint_url, s3_region,
key_file, recovery_set, pg_dump_path, pg_restore_path, evidence_file. Use
passwordless local psycopg URLs, database names beginning
``scientist_w2_20261007`` (target distinct from source), and distinct bucket
names beginning ``scientist-w2-20261007``. ``evidence_file`` must be new and
inside an existing owner-only 0700 directory. Keep the config owner-only; do
not put credentials or key bytes in it. MinIO access credentials are supplied
through the operator's environment. The key file is passed by path to the
existing encrypted recovery-set implementation and is never opened here.

Example shape (replace every path and database name with the operator's
dedicated local values)::

    {
      "source_database_url": "postgresql+psycopg:///scientist_w2_20261007_source?host=/local/socket&port=54329",
      "target_database_url": "postgresql+psycopg:///scientist_w2_20261007_restore?host=/local/socket&port=54329",
      "source_bucket": "scientist-w2-20261007-source",
      "target_bucket": "scientist-w2-20261007-restore",
      "s3_endpoint_url": "http://127.0.0.1:9000",
      "s3_region": "us-east-1",
      "key_file": "/private/path/backup.key",
      "recovery_set": "/private/path/recovery-set",
      "pg_dump_path": "/absolute/path/pg_dump",
      "pg_restore_path": "/absolute/path/pg_restore",
      "evidence_file": "/private/evidence/w2-recovery.json"
    }
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import boto3
from botocore.config import Config as BotoConfig
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from scientist import deployment_backup as backup


_CONFIG_LIMIT = 32 * 1024
_MAX_TABLES = 512
_MAX_ROWS_PER_TABLE = 5_000_000
_MAX_ROW_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_MATERIAL_BYTES = 8 * 1024 * 1024 * 1024
_FETCH_ROWS = 256
_DB_NAMESPACE = "scientist_w2_20261007"
_BUCKET_NAMESPACE = "scientist-w2-20261007"
_CONFIG_KEYS = {
    "source_database_url",
    "target_database_url",
    "source_bucket",
    "target_bucket",
    "s3_endpoint_url",
    "s3_region",
    "key_file",
    "recovery_set",
    "pg_dump_path",
    "pg_restore_path",
    "evidence_file",
}
_REQUIRED_TABLES = {
    "schema_migrations",
    "projects",
    "runs",
    "operations",
    "owner_decisions",
    "run_budget_extensions",
    "checkpoints",
    "checkpoint_boundaries",
    "runtime_executors",
    "stored_objects",
    "artifacts",
    "scientific_artifact_receipts",
    "credentials",
    "access_tokens",
    "access_grants",
}
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")


class AcceptanceError(RuntimeError):
    """A safe operator-facing acceptance failure."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest_records(records: Iterable[bytes], *, limit: int = _MAX_TOTAL_MATERIAL_BYTES) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    count = total = 0
    for record in records:
        count += 1
        if len(record) > _MAX_ROW_BYTES:
            raise AcceptanceError("a database row exceeds the verification bound")
        total += len(record)
        if count > _MAX_ROWS_PER_TABLE or total > limit:
            raise AcceptanceError("database verification exceeds its configured safety bound")
        digest.update(len(record).to_bytes(8, "big"))
        digest.update(record)
    return digest.hexdigest(), count, total


def _local_endpoint(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise AcceptanceError("invalid local object-store endpoint")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise AcceptanceError("invalid local object-store endpoint") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.port is None
    ):
        raise AcceptanceError("object-store endpoint must be an explicit loopback HTTP URL")
    return value


def _private_config(path: Path) -> dict[str, Any]:
    if not path.is_absolute():
        raise AcceptanceError("config path must be absolute")
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > _CONFIG_LIMIT
        ):
            raise AcceptanceError("config must be a small owner-only regular file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(_CONFIG_LIMIT + 1)
    except OSError as exc:
        raise AcceptanceError("config file is unavailable") from exc
    if len(raw) > _CONFIG_LIMIT:
        raise AcceptanceError("config exceeds the size limit")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError("config must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != _CONFIG_KEYS:
        raise AcceptanceError("config fields do not match the accepted schema")
    return value


def _absolute_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise AcceptanceError(f"invalid {field} path")
    path = Path(value)
    if not path.is_absolute():
        raise AcceptanceError(f"{field} path must be absolute")
    return path


def _db_scope(url: URL) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    host_value = url.query.get("host", url.host)
    hosts = host_value if isinstance(host_value, tuple) else (host_value or "",)
    hosts = tuple(str(item) for value in hosts for item in value.split(","))
    port_value = url.query.get("port", url.port)
    ports = port_value if isinstance(port_value, tuple) else (port_value or "",)
    return str(url.database), hosts, tuple(str(port) for port in ports)


def _validated_config(raw: dict[str, Any]) -> dict[str, Any]:
    source_url = backup._database_url(raw["source_database_url"])
    target_url = backup._database_url(raw["target_database_url"])
    if _db_scope(source_url) == _db_scope(target_url):
        raise AcceptanceError("source and target PostgreSQL identities must differ")
    if any(
        name != _DB_NAMESPACE and not name.startswith(_DB_NAMESPACE + "_")
        for name in (source_url.database, target_url.database)
    ):
        raise AcceptanceError("database names must use the dedicated W2 acceptance namespace")

    source_bucket = backup._validate_bucket(raw["source_bucket"])
    target_bucket = backup._validate_bucket(raw["target_bucket"])
    if source_bucket == target_bucket:
        raise AcceptanceError("source and target buckets must differ")
    if any(
        name != _BUCKET_NAMESPACE and not name.startswith(_BUCKET_NAMESPACE + "-")
        for name in (source_bucket, target_bucket)
    ):
        raise AcceptanceError("bucket names must use the dedicated W2 acceptance namespace")
    if not isinstance(raw["s3_region"], str) or not _SAFE_NAME.fullmatch(raw["s3_region"]):
        raise AcceptanceError("invalid local object-store region")

    paths = {
        key: _absolute_path(raw[key], key)
        for key in ("key_file", "recovery_set", "pg_dump_path", "pg_restore_path", "evidence_file")
    }
    for field in ("pg_dump_path", "pg_restore_path"):
        try:
            resolved = paths[field].resolve(strict=True)
            mode = resolved.stat().st_mode
        except OSError as exc:
            raise AcceptanceError(f"{field} is unavailable") from exc
        if not stat.S_ISREG(mode) or not os.access(resolved, os.X_OK):
            raise AcceptanceError(f"{field} must name an executable regular file")
        paths[field] = resolved
    if paths["recovery_set"].exists() or paths["recovery_set"].is_symlink():
        raise AcceptanceError("recovery-set destination must not already exist")
    recovery_parent = paths["recovery_set"].parent
    try:
        recovery_parent_info = recovery_parent.lstat()
        key_info = paths["key_file"].lstat()
    except OSError as exc:
        raise AcceptanceError("recovery parent or key path is unavailable") from exc
    if (
        not stat.S_ISDIR(recovery_parent_info.st_mode)
        or recovery_parent_info.st_uid != os.geteuid()
        or stat.S_IMODE(recovery_parent_info.st_mode) != 0o700
    ):
        raise AcceptanceError("recovery-set parent must be an owner-only 0700 directory")
    if (
        not stat.S_ISREG(key_info.st_mode)
        or key_info.st_uid != os.geteuid()
        or stat.S_IMODE(key_info.st_mode) != 0o600
        or key_info.st_size != 32
    ):
        raise AcceptanceError("key path must name a private 32-byte regular file")
    evidence_parent = paths["evidence_file"].parent
    try:
        parent_info = evidence_parent.lstat()
    except OSError as exc:
        raise AcceptanceError("evidence directory must already exist") from exc
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.geteuid()
        or stat.S_IMODE(parent_info.st_mode) != 0o700
        or paths["evidence_file"].exists()
        or paths["evidence_file"].is_symlink()
    ):
        raise AcceptanceError("evidence destination requires a private directory and a new file")

    endpoint = _local_endpoint(raw["s3_endpoint_url"])
    return {
        "source_url": source_url,
        "target_url": target_url,
        "source_bucket": source_bucket,
        "target_bucket": target_bucket,
        "endpoint": endpoint,
        "region": raw["s3_region"],
        **paths,
    }


def _query_digest(connection: Any, statement: str) -> dict[str, Any]:
    result = connection.execution_options(stream_results=True).execute(text(statement))

    def records():
        while batch := result.fetchmany(_FETCH_ROWS):
            for row in batch:
                yield _canonical(list(row))

    digest, count, size = _digest_records(records())
    return {"sha256": digest, "rows": count, "canonical_bytes": size}


def _schema_snapshot(connection: Any) -> dict[str, Any]:
    queries = {
        "columns": """
            SELECT table_schema, table_name, column_name, ordinal_position, data_type,
                   udt_name, is_nullable, column_default
            FROM information_schema.columns WHERE table_schema='public'
            ORDER BY table_name, ordinal_position
        """,
        "constraints": """
            SELECT rel.relname, con.conname, con.contype, pg_get_constraintdef(con.oid),
                   con.condeferrable, con.condeferred, con.convalidated
            FROM pg_constraint con JOIN pg_class rel ON rel.oid=con.conrelid
            JOIN pg_namespace ns ON ns.oid=rel.relnamespace
            WHERE ns.nspname='public' ORDER BY rel.relname, con.conname
        """,
        "indexes": """
            SELECT tablename, indexname, indexdef FROM pg_indexes
            WHERE schemaname='public' ORDER BY tablename, indexname
        """,
        "triggers": """
            SELECT event_object_table, trigger_name, action_timing, event_manipulation,
                   action_statement FROM information_schema.triggers
            WHERE trigger_schema='public' ORDER BY event_object_table, trigger_name, event_manipulation
        """,
        "functions": """
            SELECT p.proname, pg_get_functiondef(p.oid) FROM pg_proc p
            JOIN pg_namespace ns ON ns.oid=p.pronamespace
            WHERE ns.nspname='public' AND p.prokind='f' ORDER BY p.proname
        """,
        "sequences": """
            SELECT sequence_name, data_type, numeric_precision, numeric_scale,
                   start_value, minimum_value, maximum_value, increment, cycle_option
            FROM information_schema.sequences WHERE sequence_schema='public'
            ORDER BY sequence_name
        """,
        "sequence_state": """
            SELECT sequencename, last_value FROM pg_sequences
            WHERE schemaname='public' ORDER BY sequencename
        """,
        "extensions": """
            SELECT extname, extversion FROM pg_extension ORDER BY extname
        """,
        "views": """
            SELECT table_name, view_definition FROM information_schema.views
            WHERE table_schema='public' ORDER BY table_name
        """,
        "row_security_policies": """
            SELECT cls.relname, pol.polname, pol.polcmd, pol.polpermissive,
                   pg_get_expr(pol.polqual, pol.polrelid),
                   pg_get_expr(pol.polwithcheck, pol.polrelid)
            FROM pg_policy pol JOIN pg_class cls ON cls.oid=pol.polrelid
            JOIN pg_namespace ns ON ns.oid=cls.relnamespace
            WHERE ns.nspname='public' ORDER BY cls.relname, pol.polname
        """,
    }
    parts = {name: _query_digest(connection, query) for name, query in queries.items()}
    digest, _, _ = _digest_records(_canonical([name, parts[name]["sha256"]]) for name in sorted(parts))
    return {"sha256": digest, "parts": parts}


def _table_snapshots(connection: Any) -> dict[str, dict[str, Any]]:
    rows = connection.execute(text("""
        SELECT cls.relname FROM pg_class cls
        JOIN pg_namespace ns ON ns.oid=cls.relnamespace
        WHERE ns.nspname='public' AND cls.relkind IN ('r','p') AND NOT cls.relispartition
        ORDER BY cls.relname
    """)).scalars().all()
    names = [str(name) for name in rows]
    if len(names) > _MAX_TABLES or not _REQUIRED_TABLES.issubset(names):
        raise AcceptanceError("database schema is missing required W2 tables or exceeds the table bound")
    quote = connection.dialect.identifier_preparer.quote
    snapshots: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    for name in names:
        qualified = f"{quote('public')}.{quote(name)}"
        statement = text(
            f"SELECT to_jsonb(record)::text AS row_json FROM {qualified} AS record ORDER BY row_json"
        )
        result = connection.execution_options(stream_results=True).execute(statement)

        def records():
            nonlocal total_bytes
            while batch := result.fetchmany(_FETCH_ROWS):
                for row in batch:
                    payload = str(row[0]).encode("utf-8")
                    total_bytes += len(payload)
                    if total_bytes > _MAX_TOTAL_MATERIAL_BYTES:
                        raise AcceptanceError("database verification exceeds its total byte bound")
                    yield payload

        digest, count, size = _digest_records(records())
        snapshots[name] = {"sha256": digest, "rows": count, "canonical_bytes": size}
    return snapshots


def _migration_checksums(connection: Any) -> list[list[str]]:
    rows = connection.execute(text(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    )).all()
    result = [[str(version), str(checksum).strip()] for version, checksum in rows]
    if "016_scientific_compute" not in {version for version, _ in result}:
        raise AcceptanceError("W2 compute migration is absent from the source database")
    return result


def _aggregate_rows(connection: Any, statement: str) -> list[list[Any]]:
    rows = connection.execute(text(statement)).all()
    if len(rows) > 10_000:
        raise AcceptanceError("database evidence summary exceeds its row bound")
    return [
        [int(value) if isinstance(value, Decimal) and value == value.to_integral_value() else value for value in row]
        for row in rows
    ]


def _journal_summary(connection: Any) -> dict[str, Any]:
    return {
        "runs_by_state": _aggregate_rows(connection, """
            SELECT state, count(*), COALESCE(sum(usage_tokens),0), COALESCE(sum(reserved_tokens),0),
                   COALESCE(sum(planning_tokens),0), COALESCE(sum(elapsed_used_ms),0)
            FROM runs GROUP BY state ORDER BY state
        """),
        "operations_by_state_kind": _aggregate_rows(connection, """
            SELECT state, kind, count(*), COALESCE(sum(reserve_tokens),0), COALESCE(sum(usage_tokens),0)
            FROM operations GROUP BY state, kind ORDER BY state, kind
        """),
        "owner_decisions_by_reason_state": _aggregate_rows(connection, """
            SELECT reason, state, count(*) FROM owner_decisions
            GROUP BY reason, state ORDER BY reason, state
        """),
        "executor_counts_by_state_kind": _aggregate_rows(connection, """
            SELECT state, kind, count(*) FROM runtime_executors
            GROUP BY state, kind ORDER BY state, kind
        """),
        "artifact_receipts_by_output_index": _aggregate_rows(connection, """
            SELECT output_index, count(*) FROM scientific_artifact_receipts
            GROUP BY output_index ORDER BY output_index
        """),
        "artifacts_by_kind_partial": _aggregate_rows(connection, """
            SELECT kind, partial, count(*) FROM artifacts GROUP BY kind, partial ORDER BY kind, partial
        """),
        "credential_grant_counts": _aggregate_rows(connection, """
            SELECT 'credentials', count(*) FROM credentials
            UNION ALL SELECT 'access_tokens', count(*) FROM access_tokens
            UNION ALL SELECT 'access_grants', count(*) FROM access_grants
        """),
        "checkpoint_counts": _aggregate_rows(connection, """
            SELECT 'checkpoints', count(*) FROM checkpoints
            UNION ALL SELECT 'checkpoint_boundaries', count(*) FROM checkpoint_boundaries
        """),
    }


@contextmanager
def _quiescent_snapshot_connection(url: URL):
    engine = create_engine(url, poolclass=NullPool)
    lock_connection = None
    try:
        lock_connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        acquired = lock_connection.execute(
            text("SELECT pg_try_advisory_lock(hashtext('scientist.host'))")
        ).scalar_one()
        if acquired is not True:
            raise AcceptanceError("host must be stopped before recovery acceptance")
        connection = engine.connect().execution_options(isolation_level="REPEATABLE READ")
        try:
            with connection.begin():
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                backup._quiescent(connection)
                yield connection
        finally:
            connection.close()
    except AcceptanceError:
        raise
    except Exception as exc:
        raise AcceptanceError("could not acquire a consistent quiescent database snapshot") from exc
    finally:
        if lock_connection is not None:
            lock_connection.close()
        engine.dispose()


def _database_snapshot(url: URL) -> dict[str, Any]:
    with _quiescent_snapshot_connection(url) as connection:
        application_schemas = connection.execute(text("""
            SELECT nspname FROM pg_namespace
            WHERE nspname NOT IN ('pg_catalog','information_schema','pg_toast')
              AND nspname !~ '^pg_temp_[0-9]+$' AND nspname !~ '^pg_toast_temp_[0-9]+$'
            ORDER BY nspname
        """)).scalars().all()
        unsupported_relations = connection.execute(text("""
            SELECT cls.relname FROM pg_class cls
            JOIN pg_namespace ns ON ns.oid=cls.relnamespace
            WHERE ns.nspname='public' AND cls.relkind IN ('m','f') ORDER BY cls.relname
        """)).scalars().all()
        if list(application_schemas) != ["public"] or unsupported_relations:
            raise AcceptanceError("database contains schemas or relations outside the verified recovery scope")
        identity_name, identity_sha = backup._source_identity(connection)
        schemas = _schema_snapshot(connection)
        tables = _table_snapshots(connection)
        migrations = _migration_checksums(connection)
        journal = _journal_summary(connection)
        object_rows = backup._reference_rows(connection)
    return {
        "database": identity_name,
        "server_identity_sha256": identity_sha,
        "schema": schemas,
        "migration_checksums": migrations,
        "tables": tables,
        "journal": journal,
        "objects": [
            {"key": key, "project_id": project, "sha256": digest, "size": size, "content_type": mime}
            for key, project, digest, size, mime in object_rows
        ],
    }


def _list_keys(client: Any, bucket: str) -> set[str]:
    keys: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for item in page.get("Contents", []):
            key = item.get("Key")
            if not isinstance(key, str) or len(key.encode("utf-8")) > 1024:
                raise AcceptanceError("object-store listing returned an invalid key")
            keys.add(key)
            if len(keys) > 100_000:
                raise AcceptanceError("object-store listing exceeds the recovery-set bound")
    return keys


def _verify_objects(client: Any, bucket: str, objects: list[dict[str, Any]], *, exact_listing: bool) -> dict[str, Any]:
    expected = {item["key"] for item in objects}
    if exact_listing and _list_keys(client, bucket) != expected:
        raise AcceptanceError("restored bucket keys differ from the database object index")

    def verified_records():
        for item in objects:
            response = client.get_object(Bucket=bucket, Key=item["key"])
            body = response["Body"]
            digest = hashlib.sha256()
            size = 0
            try:
                while chunk := body.read(64 * 1024):
                    size += len(chunk)
                    if size > item["size"]:
                        raise AcceptanceError("object readback exceeds its database size")
                    digest.update(chunk)
            finally:
                body.close()
            if (
                size != item["size"]
                or digest.hexdigest() != item["sha256"]
                or response.get("ContentType") != item["content_type"]
            ):
                raise AcceptanceError("object readback differs from its immutable database reference")
            yield _canonical({"key": item["key"], "sha256": digest.hexdigest(), "size": size})

    fingerprint, count, _ = _digest_records(verified_records())
    return {"sha256": fingerprint, "objects": count, "exact_bucket_listing": exact_listing}


def _manifest_summary(path: Path, source: dict[str, Any], bucket: str) -> dict[str, Any]:
    manifest_path = path / "manifest.json"
    try:
        info = manifest_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 32 * 1024 * 1024:
            raise AcceptanceError("recovery manifest has an invalid type or size")
        raw = manifest_path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceError("recovery manifest is unreadable") from exc
    if value.get("source_bucket") != bucket or not isinstance(value.get("objects"), list):
        raise AcceptanceError("recovery manifest does not match the selected source bucket")
    expected = [
        {key: item[key] for key in ("key", "project_id", "sha256", "size", "content_type")}
        for item in source["objects"]
    ]
    actual = [
        {key: item.get(key) for key in ("key", "project_id", "sha256", "size", "content_type")}
        for item in value["objects"]
    ]
    if actual != expected:
        raise AcceptanceError("recovery manifest object index differs from the source database")
    allowed = {"manifest.json", "database.dump.enc"} | {item.get("path") for item in value["objects"]}
    present = {
        item.relative_to(path).as_posix()
        for item in path.rglob("*")
        if item.is_file()
    }
    if present != allowed or any(".raw" in item or item.endswith(".dump") for item in present):
        raise AcceptanceError("recovery set contains unexpected or plaintext staging files")
    return {"manifest_sha256": hashlib.sha256(raw).hexdigest(), "object_count": len(actual)}


def _stage_dirs(parent: Path) -> set[str]:
    return {
        path.name for path in parent.iterdir()
        if path.name.startswith((".scientist-recovery-", ".scientist-restore-"))
    }


def _write_evidence(path: Path, report: dict[str, Any]) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(_canonical(report) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def run_acceptance(config: dict[str, Any]) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    source = _database_snapshot(config["source_url"])
    set_parent = config["recovery_set"].parent
    stage_before = _stage_dirs(set_parent)
    # Keep boto3 on its environment credential path; this harness never reads
    # or records credential values and disables fallback to remote providers.
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    os.environ["AWS_CONFIG_FILE"] = os.devnull
    os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    for name in (
        "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    ):
        os.environ.pop(name, None)
    client = boto3.client(
        "s3",
        endpoint_url=config["endpoint"],
        region_name=config["region"],
        config=BotoConfig(connect_timeout=5, read_timeout=30, retries={"max_attempts": 1}),
    )
    recovery_path = backup.create_recovery_set(
        str(config["source_url"]), bucket=config["source_bucket"], s3_client=client,
        destination=config["recovery_set"], key_file=config["key_file"],
        pg_dump_path=config["pg_dump_path"],
    )
    manifest = _manifest_summary(recovery_path, source, config["source_bucket"])
    source_object_readback = _verify_objects(
        client, config["source_bucket"], source["objects"], exact_listing=False
    )
    restore_stage_before = _stage_dirs(recovery_path)
    if restore_stage_before:
        raise AcceptanceError("fresh recovery set already contains a restore staging directory")
    backup.restore_recovery_set(
        str(config["target_url"]), target_bucket=config["target_bucket"], s3_client=client,
        recovery_set=recovery_path, key_file=config["key_file"],
        pg_restore_path=config["pg_restore_path"],
    )
    restored = _database_snapshot(config["target_url"])
    restore_stage_clean = restore_stage_before == _stage_dirs(recovery_path)
    target_object_readback = _verify_objects(
        client, config["target_bucket"], restored["objects"], exact_listing=True
    )
    source_after = _database_snapshot(config["source_url"])
    compare = {
        "schema": source["schema"] == restored["schema"],
        "migration_checksums": source["migration_checksums"] == restored["migration_checksums"],
        "all_public_table_hashes": source["tables"] == restored["tables"],
        "held_journal_and_authority_summaries": source["journal"] == restored["journal"],
        "registered_object_index": source["objects"] == restored["objects"],
        "source_unchanged_during_capture": source == source_after,
        "source_readback": source_object_readback["objects"] == len(source["objects"]),
        "restored_object_readback": target_object_readback["objects"] == len(restored["objects"]),
        "source_and_target_object_hashes_equal": source_object_readback["sha256"] == target_object_readback["sha256"],
        "target_bucket_exact": target_object_readback["exact_bucket_listing"],
        "no_new_plaintext_stage_directories": (
            stage_before == _stage_dirs(set_parent) and restore_stage_clean
        ),
    }
    if not all(compare.values()):
        raise AcceptanceError("database, journal, or object-store recovery comparison failed")
    return {
        "schema_version": 1,
        "status": "PASS",
        "started_at": started_at,
        "source_database": source["database"],
        "target_database": restored["database"],
        "source_bucket": config["source_bucket"],
        "target_bucket": config["target_bucket"],
        "source_server_identity_sha256": source["server_identity_sha256"],
        "target_server_identity_sha256": restored["server_identity_sha256"],
        "schema_sha256": source["schema"]["sha256"],
        "migration_checksums": source["migration_checksums"],
        "table_hashes": source["tables"],
        "journal_summaries": source["journal"],
        "registered_objects": source["objects"],
        "source_object_readback": source_object_readback,
        "target_object_readback": target_object_readback,
        "recovery_manifest": manifest,
        "checks": compare,
        "paid_calls": 0,
    }


def _self_check() -> None:
    first = _digest_records([b"row-1", b"row-2"])
    repeated = _digest_records([b"row-1", b"row-2"])
    distinct_framing = _digest_records([b"row-", b"1row-2"])
    assert first == repeated
    assert first[0] != distinct_framing[0]
    assert _local_endpoint("http://127.0.0.1:54332") == "http://127.0.0.1:54332"
    for unsafe in (
        "https://127.0.0.1:443",
        "http://example.invalid:9000",
        "http://user:pass@127.0.0.1:9000",
        "http://127.0.0.1:9000/path",
    ):
        try:
            _local_endpoint(unsafe)
        except AcceptanceError:
            continue
        raise AssertionError("non-local object-store endpoint accepted")
    print("self-check PASS (no database, storage, Docker, or provider calls)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--config", type=Path, help="absolute private JSON config file")
    selection.add_argument("--self-check", action="store_true", help="run offline validation checks only")
    args = parser.parse_args(argv)
    if args.self_check:
        _self_check()
        return 0

    stage = "config"
    report_path: Path | None = None
    try:
        raw = _private_config(args.config)
        config = _validated_config(raw)
        report_path = config["evidence_file"]
        stage = "capture-and-restore"
        report = run_acceptance(config)
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        _write_evidence(report_path, report)
        print(f"PASS; evidence written to {report_path}")
        return 0
    except Exception as exc:
        safe_report = {
            "schema_version": 1,
            "status": "FAIL",
            "stage": stage,
            "error_type": type(exc).__name__,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "paid_calls": 0,
        }
        if report_path is not None:
            try:
                _write_evidence(report_path, safe_report)
            except Exception:
                pass
        print(f"FAIL at {stage}: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
