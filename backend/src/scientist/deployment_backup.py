"""Encrypted, quiesced PostgreSQL + object-store recovery sets.

This module is an operator utility, not an application API. Callers must provide
the existing target bucket/client, a dedicated 32-byte backup key file, and the
reviewed PostgreSQL client binary paths. It never creates a database or bucket.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, BinaryIO, Iterator
from uuid import UUID

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool

FORMAT = "scientist-recovery-set"
SCHEMA_VERSION = 1
_CHUNK = 64 * 1024
_MAX_KEY_BYTES = 32
_MAX_OBJECT_BYTES = 25 * 1024 * 1024
_MAX_SET_BYTES = 100 * 1024 * 1024 * 1024
_MAX_OBJECTS = 100_000
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_OBJECT_PATH = re.compile(r"^objects/[0-9]{8}\.bin\.enc$")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


class RecoverySetError(RuntimeError):
    """A recovery set is invalid, unsafe to create, or unsafe to restore."""


def _fail(message: str) -> None:
    raise RecoverySetError(message)


def _validate_bucket(value: str) -> str:
    if not isinstance(value, str) or not _BUCKET.fullmatch(value) or ".." in value:
        _fail("invalid bucket name")
    return value


def _private_directory(path: Path) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RecoverySetError("private directory unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        _fail("private directory must be owned mode 0700")
    return path


def _private_file(path: Path, *, limit: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise RecoverySetError("private file unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            _fail("private file must be owned mode 0600")
        if info.st_size > limit:
            _fail("private file exceeds its size limit")
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(limit + 1)
        if len(data) != info.st_size:
            _fail("private file changed while reading")
        return data
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _backup_key(path: Path) -> bytes:
    key = _private_file(path, limit=_MAX_KEY_BYTES)
    if len(key) != _MAX_KEY_BYTES:
        _fail("backup key must contain exactly 32 bytes")
    return key


def _database_url(value: str) -> URL:
    try:
        url = make_url(value)
    except Exception as exc:
        raise RecoverySetError("invalid PostgreSQL URL") from exc
    if url.drivername != "postgresql+psycopg" or not url.database:
        _fail("PostgreSQL URL must name a database and use psycopg")
    if url.password is not None or url.username is not None:
        _fail("use the host's passwordless local PostgreSQL configuration")
    if url.query.keys() - {"host", "port"}:
        _fail("unsupported PostgreSQL URL options")
    hosts = url.query.get("host", url.host)
    if isinstance(hosts, tuple):
        host_values = tuple(str(part) for part in hosts)
    elif hosts is None:
        host_values = ("",)
    else:
        host_values = tuple(part for item in (hosts if isinstance(hosts, tuple) else (str(hosts),))
                            for part in item.split(","))
    if any(host and not (host.startswith("/") or host in {"localhost", "127.0.0.1", "::1"}) for host in host_values):
        _fail("backup and restore support local PostgreSQL only")
    return url


def _connection_env(url: URL) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PGPASSWORD", None)
    env["PGDATABASE"] = str(url.database)
    hosts = url.query.get("host", url.host)
    if hosts:
        env["PGHOST"] = str(hosts[0] if isinstance(hosts, tuple) else hosts)
    port = url.query.get("port", url.port)
    if port:
        env["PGPORT"] = str(port[0] if isinstance(port, tuple) else port)
    return env


def _binary(path: Path, label: str) -> Path:
    if not path.is_absolute():
        _fail(f"{label} path must be absolute")
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise RecoverySetError(f"{label} binary unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        _fail(f"{label} path must be an executable file")
    return resolved


@contextmanager
def _host_gate(database_url: str) -> Iterator[Any]:
    """Hold the host's process-wide advisory lock through capture or restore."""
    url = _database_url(database_url)
    engine = create_engine(url, poolclass=NullPool, connect_args={"connect_timeout": 5})
    connection = None
    try:
        try:
            connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            acquired = connection.execute(text("SELECT pg_try_advisory_lock(hashtext('scientist.host'))")).scalar_one()
            if acquired is not True:
                _fail("host is active; stop it before backup or restore")
        except RecoverySetError:
            raise
        except Exception as exc:
            raise RecoverySetError("PostgreSQL gate unavailable") from exc
        yield connection
    finally:
        if connection is not None:
            connection.close()
        engine.dispose()


def _quiescent(connection: Any) -> None:
    active_runs = connection.execute(text(
        "SELECT count(*) FROM runs WHERE state IN ('running','recovering','stopping')"
    )).scalar_one()
    if active_runs:
        _fail("active runs must be reconciled before backup or restore")
    active_executors = connection.execute(text(
        "SELECT count(*) FROM runtime_executors WHERE state IN ('starting','active','fencing','unknown')"
    )).scalar_one()
    if active_executors:
        _fail("runtime executors must be reconciled before backup or restore")
    unfinished_uploads = connection.execute(text(
        "SELECT count(*) FROM file_versions WHERE state IN ('uploading','preparing')"
    )).scalar_one()
    if unfinished_uploads:
        _fail("file uploads must be reconciled before backup or restore")
    try:
        active_profiles = connection.execute(text(
            "SELECT count(*) FROM profile_preparations WHERE state IN ('queued','building','checking','unknown')"
        )).scalar_one()
    except Exception as exc:
        raise RecoverySetError("profile preparation state could not be checked") from exc
    if active_profiles:
        _fail("profile preparations must be reconciled before backup or restore")


def _source_identity(connection: Any) -> tuple[str, str]:
    row = connection.execute(text(
        "SELECT current_database(), COALESCE(inet_server_addr()::text,''), "
        "COALESCE(inet_server_port()::text,''), current_setting('data_directory')"
    )).one()
    database_name = str(row[0])
    identity = "\0".join(str(part) for part in row)
    return database_name, sha256(identity.encode("utf-8")).hexdigest()


def _derive(key: bytes, salt: bytes, purpose: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"scientist-recovery-set/v1/" + purpose).derive(key)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _encrypt_file(source: Path, destination: Path, key: bytes, salt: bytes, aad: bytes, *, limit: int) -> tuple[str, str]:
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(_derive(key, salt, aad)), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(aad)
    digest = sha256()
    total = 0
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as src:
            while chunk := src.read(_CHUNK):
                total += len(chunk)
                if total > limit:
                    _fail("recovery set content exceeds its size limit")
                ciphertext = encryptor.update(chunk)
                out.write(ciphertext)
                digest.update(ciphertext)
            final = encryptor.finalize()
            out.write(final)
            digest.update(final)
            out.write(encryptor.tag)
            digest.update(encryptor.tag)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return base64.b64encode(nonce).decode("ascii"), digest.hexdigest()


def _decrypt_file(source: Path, destination: Path, key: bytes, salt: bytes, aad: bytes,
                  nonce_text: str, expected_ciphertext_sha: str, *, limit: int) -> None:
    try:
        info = source.lstat()
    except OSError as exc:
        raise RecoverySetError("recovery file missing") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size < 16 or info.st_size > limit + 16:
        _fail("recovery file has invalid type or size")
    nonce = _decode64(nonce_text, 12, "nonce")
    digest = sha256()
    with source.open("rb") as encrypted:
        encrypted.seek(-16, os.SEEK_END)
        tag = encrypted.read(16)
        encrypted.seek(0)
        ciphertext_size = info.st_size - 16
        decryptor = Cipher(algorithms.AES(_derive(key, salt, aad)), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(aad)
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb") as plain:
                remaining = ciphertext_size
                while remaining:
                    chunk = encrypted.read(min(_CHUNK, remaining))
                    if not chunk:
                        _fail("recovery file is truncated")
                    remaining -= len(chunk)
                    digest.update(chunk)
                    plain.write(decryptor.update(chunk))
                digest.update(tag)
                if not hmac.compare_digest(digest.hexdigest(), expected_ciphertext_sha):
                    _fail("recovery file checksum mismatch")
                try:
                    plain.write(decryptor.finalize())
                except Exception as exc:
                    raise RecoverySetError("recovery file authentication failed") from exc
                plain.flush()
                os.fsync(plain.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def _decode64(value: Any, expected_bytes: int, label: str) -> bytes:
    if not isinstance(value, str) or len(value) > 128:
        _fail(f"invalid {label}")
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise RecoverySetError(f"invalid {label}") from exc
    if len(raw) != expected_bytes:
        _fail(f"invalid {label}")
    return raw


def _manifest_mac(manifest: dict[str, Any], key: bytes) -> str:
    unsigned = dict(manifest)
    unsigned.pop("manifest_hmac", None)
    salt = _decode64(unsigned.get("key_salt"), 16, "key salt")
    mac_key = _derive(key, salt, b"manifest-hmac")
    return base64.b64encode(hmac.new(mac_key, _canonical(unsigned), sha256).digest()).decode("ascii")


def _validate_manifest(raw: bytes, key: bytes) -> dict[str, Any]:
    if len(raw) > _MAX_MANIFEST_BYTES:
        _fail("manifest exceeds its size limit")
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("duplicate manifest field")
            result[name] = value
        return result

    def reject_nonfinite(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    try:
        manifest = json.loads(raw, object_pairs_hook=reject_duplicates, parse_constant=reject_nonfinite)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise RecoverySetError("manifest is invalid JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "format", "schema_version", "created_at", "source_database", "source_identity",
        "source_bucket", "key_salt", "cipher", "kdf", "database", "objects", "manifest_hmac",
    }:
        _fail("manifest has unsupported fields")
    if manifest["format"] != FORMAT or type(manifest["schema_version"]) is not int or manifest["schema_version"] != SCHEMA_VERSION:
        _fail("manifest version is unsupported")
    if manifest["cipher"] != "AES-256-GCM" or manifest["kdf"] != "HKDF-SHA256":
        _fail("manifest encryption suite is unsupported")
    for name in ("created_at", "source_database", "source_bucket"):
        if not isinstance(manifest[name], str) or not manifest[name] or len(manifest[name]) > 256:
            _fail("manifest metadata is invalid")
    if not isinstance(manifest["source_identity"], str) or not _HEX.fullmatch(manifest["source_identity"]):
        _fail("manifest source identity is invalid")
    _validate_bucket(manifest["source_bucket"])
    salt = _decode64(manifest["key_salt"], 16, "key salt")
    del salt
    db = manifest["database"]
    if not isinstance(db, dict) or set(db) != {"path", "nonce", "ciphertext_sha256"}:
        _fail("manifest database entry is invalid")
    if (db["path"] != "database.dump.enc" or not isinstance(db["ciphertext_sha256"], str)
            or not _HEX.fullmatch(db["ciphertext_sha256"])):
        _fail("manifest database file binding is invalid")
    _decode64(db["nonce"], 12, "database nonce")
    objects = manifest["objects"]
    if not isinstance(objects, list) or len(objects) > _MAX_OBJECTS:
        _fail("manifest object list exceeds its limit")
    seen: set[str] = set()
    for index, item in enumerate(objects):
        if not isinstance(item, dict) or set(item) != {
            "key", "project_id", "sha256", "size", "content_type", "path", "nonce", "ciphertext_sha256",
        }:
            _fail("manifest object entry has unsupported fields")
        try:
            project_id = str(UUID(item["project_id"]))
        except (TypeError, ValueError, AttributeError) as exc:
            raise RecoverySetError("manifest object project is invalid") from exc
        digest = item["sha256"]
        if (not isinstance(digest, str) or not _HEX.fullmatch(digest)
                or not isinstance(item["key"], str) or item["key"] != f"{project_id}/{digest}"):
            _fail("manifest object identity is invalid")
        if item["key"] in seen:
            _fail("manifest contains duplicate objects")
        seen.add(item["key"])
        if type(item["size"]) is not int or not 0 <= item["size"] <= _MAX_OBJECT_BYTES:
            _fail("manifest object size is invalid")
        if not isinstance(item["content_type"], str) or not item["content_type"] or len(item["content_type"]) > 255:
            _fail("manifest object content type is invalid")
        if (not isinstance(item["path"], str) or item["path"] != f"objects/{index:08d}.bin.enc"
                or not _OBJECT_PATH.fullmatch(item["path"])):
            _fail("manifest object path is invalid")
        if not isinstance(item["ciphertext_sha256"], str) or not _HEX.fullmatch(item["ciphertext_sha256"]):
            _fail("manifest object checksum is invalid")
        _decode64(item["nonce"], 12, "object nonce")
    mac = manifest["manifest_hmac"]
    if not isinstance(mac, str) or not hmac.compare_digest(mac, _manifest_mac(manifest, key)):
        _fail("manifest authentication failed")
    return manifest


def _read_recovery_tree(root: Path, key: bytes) -> tuple[dict[str, Any], set[Path]]:
    if not root.is_absolute():
        _fail("recovery set path must be absolute")
    _private_directory(root)
    manifest_path = root / "manifest.json"
    raw = _private_file(manifest_path, limit=_MAX_MANIFEST_BYTES)
    manifest = _validate_manifest(raw, key)
    expected = {Path("manifest.json"), Path(manifest["database"]["path"])}
    expected.update(Path(item["path"]) for item in manifest["objects"])
    actual: set[Path] = set()
    expected_dirs = {Path("objects")}
    actual_dirs: set[Path] = set()
    total_size = 0
    for path in root.rglob("*"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            _fail("recovery set contains a symbolic link")
        if stat.S_ISDIR(info.st_mode):
            relative = path.relative_to(root)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                _fail("recovery set directories must be owned mode 0700")
            actual_dirs.add(relative)
            continue
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
            _fail("recovery set files must be owned mode 0600")
        relative = path.relative_to(root)
        total_size += info.st_size
        if total_size > _MAX_SET_BYTES + 16 * (len(manifest["objects"]) + 1) + _MAX_MANIFEST_BYTES:
            _fail("recovery set exceeds its total size limit")
        if relative in actual:
            _fail("recovery set contains duplicate paths")
        actual.add(relative)
    if actual != expected:
        _fail("recovery set files do not match the manifest")
    if actual_dirs != expected_dirs:
        _fail("recovery set directories do not match the manifest")
    return manifest, expected


def _ensure_quiet_target(connection: Any, s3_client: Any, bucket: str, source_identity: str,
                         source_bucket: str) -> None:
    database_name, target_identity = _source_identity(connection)
    del database_name
    if hmac.compare_digest(target_identity, source_identity):
        _fail("restore target is the source database")
    if bucket == source_bucket:
        _fail("restore target bucket must differ from the source bucket")
    relations = connection.execute(text("""
        WITH user_schemas AS (
            SELECT oid, nspname
            FROM pg_namespace
            WHERE nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
              AND nspname !~ '^pg_temp_[0-9]+$'
              AND nspname !~ '^pg_toast_temp_[0-9]+$'
        )
        SELECT
            (SELECT count(*) FROM pg_class c JOIN user_schemas n ON n.oid = c.relnamespace
             WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f'))
            + (SELECT count(*) FROM user_schemas WHERE nspname <> 'public')
    """)).scalar_one()
    if relations:
        _fail("restore database must have an empty application schema")
    try:
        response = s3_client.list_objects_v2(Bucket=bucket, MaxKeys=1)
    except Exception as exc:
        raise RecoverySetError("restore bucket could not be inspected") from exc
    if response.get("KeyCount") != 0 or response.get("Contents") or response.get("IsTruncated"):
        _fail("restore bucket must be empty")


def _reference_rows(connection: Any) -> list[tuple[str, str, str, int, str]]:
    rows = connection.execute(text(
        "SELECT key, project_id::text, sha256, size, content_type FROM stored_objects ORDER BY key"
    )).all()
    result: list[tuple[str, str, str, int, str]] = []
    for row in rows:
        key, project, digest, size, content_type = row
        digest = str(digest).strip()
        if key != f"{project}/{digest}" or not _HEX.fullmatch(digest) or type(size) is not int or size < 0:
            _fail("stored object metadata is inconsistent")
        if not isinstance(content_type, str) or not content_type or len(content_type) > 255:
            _fail("stored object content type is invalid")
        result.append((str(key), str(project), digest, size, content_type))
    return result


def _assert_registered_references(connection: Any) -> None:
    missing = connection.execute(text("""
        WITH object_refs(object_key) AS (
            SELECT object_key FROM file_versions WHERE object_key IS NOT NULL
            UNION SELECT object_key FROM artifacts
            UNION SELECT extracted.value #>> '{}' FROM input_snapshots
                CROSS JOIN LATERAL jsonb_path_query(manifest, '$.**.key') AS extracted(value)
            UNION SELECT extracted.value #>> '{}' FROM checkpoints
                CROSS JOIN LATERAL jsonb_path_query(manifest, '$.**.key') AS extracted(value)
            UNION SELECT extracted.value #>> '{}' FROM operations
                CROSS JOIN LATERAL jsonb_path_query(COALESCE(result, '{}'::jsonb), '$.**.key') AS extracted(value)
        )
        SELECT object_refs.object_key FROM object_refs
        LEFT JOIN stored_objects ON stored_objects.key = object_refs.object_key
        WHERE object_refs.object_key ~ '^[0-9a-f-]{36}/[0-9a-f]{64}$'
          AND stored_objects.key IS NULL
        LIMIT 1
    """)).first()
    if missing is not None:
        _fail("database contains a reference to an unregistered object")


def _verify_migrations(connection: Any) -> None:
    from scientist.db import MIGRATION

    expected = {
        path.stem: sha256(path.read_bytes()).hexdigest()
        for path in sorted(MIGRATION.parent.glob("[0-9]*.sql"))
    }
    actual = {
        str(row.version): str(row.checksum).strip()
        for row in connection.execute(text("SELECT version, checksum FROM schema_migrations ORDER BY version"))
    }
    if actual != expected:
        _fail("restored schema migration checksums do not match this application")


def _download_object(s3_client: Any, bucket: str, key: str, path: Path, size: int, digest: str) -> None:
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        body: BinaryIO = response["Body"]
    except Exception as exc:
        raise RecoverySetError("registered object is unavailable") from exc
    hasher = sha256()
    total = 0
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := body.read(_CHUNK):
                total += len(chunk)
                if total > size or total > _MAX_OBJECT_BYTES:
                    _fail("registered object exceeds recorded size")
                hasher.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if total != size or not hmac.compare_digest(hasher.hexdigest(), digest):
            _fail("registered object bytes do not match PostgreSQL metadata")
    except RecoverySetError:
        path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise RecoverySetError("registered object is unavailable") from exc
    finally:
        body.close()


def _run_pg(binary: Path, args: list[str], env: dict[str, str], *, timeout: int = 3600) -> None:
    try:
        result = subprocess.run([str(binary), *args], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                shell=False, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoverySetError("PostgreSQL utility failed") from exc
    if result.returncode != 0:
        _fail("PostgreSQL utility failed")


def create_recovery_set(
    database_url: str,
    *,
    bucket: str,
    s3_client: Any,
    destination: Path,
    key_file: Path,
    pg_dump_path: Path,
) -> Path:
    """Capture a quiesced database and every registered immutable object atomically."""
    url = _database_url(database_url)
    bucket = _validate_bucket(bucket)
    key = _backup_key(key_file)
    pg_dump = _binary(pg_dump_path, "pg_dump")
    destination = Path(destination)
    if not destination.is_absolute():
        _fail("recovery set destination must be absolute")
    parent = _private_directory(destination.parent)
    if os.path.lexists(destination):
        _fail("recovery set destination already exists")
    stage = Path(tempfile.mkdtemp(prefix=".scientist-recovery-", dir=parent))
    os.chmod(stage, 0o700)
    try:
        with _host_gate(database_url) as connection:
            _quiescent(connection)
            source_database, source_identity = _source_identity(connection)
            _assert_registered_references(connection)
            rows = _reference_rows(connection)
            if len(rows) > _MAX_OBJECTS:
                _fail("stored object count exceeds the recovery set limit")
            db_raw = stage / ".database.dump"
            fd = os.open(db_raw, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            _run_pg(pg_dump, ["--format=custom", "--no-owner", "--no-privileges", "--no-password", "--file", str(db_raw)],
                    _connection_env(url))
            if db_raw.stat().st_size > _MAX_SET_BYTES:
                _fail("database dump exceeds the recovery set size limit")
            salt = os.urandom(16)
            database_nonce, database_cipher_sha = _encrypt_file(
                db_raw, stage / "database.dump.enc", key, salt, b"database", limit=_MAX_SET_BYTES,
            )
            db_raw.unlink()
            objects_dir = stage / "objects"
            objects_dir.mkdir(mode=0o700)
            object_entries: list[dict[str, Any]] = []
            total_size = (stage / "database.dump.enc").stat().st_size
            for index, (object_key, project, digest, size, content_type) in enumerate(rows):
                raw_path = stage / f".object-{index:08d}.raw"
                _download_object(s3_client, bucket, object_key, raw_path, size, digest)
                relative = f"objects/{index:08d}.bin.enc"
                nonce, ciphertext_sha = _encrypt_file(
                    raw_path, stage / relative, key, salt, b"object:" + object_key.encode("ascii"),
                    limit=_MAX_OBJECT_BYTES,
                )
                raw_path.unlink()
                total_size += (stage / relative).stat().st_size
                if total_size > _MAX_SET_BYTES:
                    _fail("recovery set exceeds its total size limit")
                object_entries.append({
                    "key": object_key, "project_id": project, "sha256": digest, "size": size,
                    "content_type": content_type, "path": relative, "nonce": nonce,
                    "ciphertext_sha256": ciphertext_sha,
                })
            manifest = {
                "format": FORMAT,
                "schema_version": SCHEMA_VERSION,
                "created_at": datetime.now(UTC).isoformat(),
                "source_database": source_database,
                "source_identity": source_identity,
                "source_bucket": bucket,
                "key_salt": base64.b64encode(salt).decode("ascii"),
                "cipher": "AES-256-GCM",
                "kdf": "HKDF-SHA256",
                "database": {"path": "database.dump.enc", "nonce": database_nonce,
                             "ciphertext_sha256": database_cipher_sha},
                "objects": object_entries,
            }
            manifest["manifest_hmac"] = _manifest_mac(manifest, key)
            manifest_path = stage / "manifest.json"
            fd = os.open(manifest_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as out:
                encoded = _canonical(manifest)
                if len(encoded) > _MAX_MANIFEST_BYTES:
                    _fail("manifest exceeds its size limit")
                out.write(encoded)
                out.flush()
                os.fsync(out.fileno())
        if os.path.lexists(destination):
            _fail("recovery set destination already exists")
        os.rename(stage, destination)
        return destination
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def restore_recovery_set(
    database_url: str,
    *,
    target_bucket: str,
    s3_client: Any,
    recovery_set: Path,
    key_file: Path,
    pg_restore_path: Path,
) -> None:
    """Restore only a fully authenticated recovery set into an empty fresh target."""
    url = _database_url(database_url)
    target_bucket = _validate_bucket(target_bucket)
    key = _backup_key(key_file)
    pg_restore = _binary(pg_restore_path, "pg_restore")
    recovery_set = Path(recovery_set)
    with _host_gate(database_url) as connection:
        manifest_path = recovery_set / "manifest.json"
        try:
            raw = _private_file(manifest_path, limit=_MAX_MANIFEST_BYTES)
        except RecoverySetError:
            raise
        manifest = _validate_manifest(raw, key)
        _, expected_paths = _read_recovery_tree(recovery_set, key)
        del expected_paths
        _ensure_quiet_target(connection, s3_client, target_bucket, manifest["source_identity"],
                             manifest["source_bucket"])
        stage = Path(tempfile.mkdtemp(prefix=".scientist-restore-", dir=recovery_set))
        os.chmod(stage, 0o700)
        try:
            salt = _decode64(manifest["key_salt"], 16, "key salt")
            plain_db = stage / "database.dump"
            database = manifest["database"]
            _decrypt_file(recovery_set / database["path"], plain_db, key, salt, b"database",
                          database["nonce"], database["ciphertext_sha256"], limit=_MAX_SET_BYTES)
            plaintext_objects: list[tuple[dict[str, Any], Path]] = []
            for index, item in enumerate(manifest["objects"]):
                plain = stage / f"object-{index:08d}.bin"
                aad = b"object:" + item["key"].encode("ascii")
                _decrypt_file(recovery_set / item["path"], plain, key, salt, aad, item["nonce"],
                              item["ciphertext_sha256"], limit=_MAX_OBJECT_BYTES)
                hasher = sha256()
                total = 0
                with plain.open("rb") as stream:
                    while chunk := stream.read(_CHUNK):
                        total += len(chunk)
                        hasher.update(chunk)
                if total != item["size"] or not hmac.compare_digest(hasher.hexdigest(), item["sha256"]):
                    _fail("recovery object bytes do not match their manifest")
                plaintext_objects.append((item, plain))
            _run_pg(pg_restore, ["--exit-on-error", "--no-owner", "--no-privileges", "--no-password",
                                 "--dbname", str(url.database), str(plain_db)],
                    _connection_env(url))
            _verify_migrations(connection)
            _assert_registered_references(connection)
            restored_rows = _reference_rows(connection)
            expected_rows = sorted((item["key"], item["project_id"], item["sha256"], item["size"], item["content_type"])
                                   for item in manifest["objects"])
            if restored_rows != expected_rows:
                _fail("restored PostgreSQL object registry differs from the recovery set")
            for item, path in plaintext_objects:
                try:
                    with path.open("rb") as body:
                        s3_client.put_object(Bucket=target_bucket, Key=item["key"], Body=body,
                                             ContentType=item["content_type"])
                    _download_object(s3_client, target_bucket, item["key"], stage / f"verify-{item['sha256']}",
                                     item["size"], item["sha256"])
                    (stage / f"verify-{item['sha256']}").unlink()
                except RecoverySetError:
                    raise
                except Exception as exc:
                    raise RecoverySetError("restored object could not be verified") from exc
        finally:
            shutil.rmtree(stage, ignore_errors=True)
