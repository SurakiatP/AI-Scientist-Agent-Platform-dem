#!/usr/bin/env python3
"""Scoped, reversible SCRAM migration for the owner's isolated b5 test service.

The operator runs plan first, then apply --owner-approved. Secret bytes are never
printed or placed in command arguments. Local socket authentication is retained
for recovery. This helper never recreates containers, deletes data, or restarts
PostgreSQL. Backups and credentials stay beneath an ignored private directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit


def scram_hba(source: str) -> str:
    lines = []
    hosts = 0
    for line in source.splitlines():
        fields = line.split()
        if not fields or fields[0].startswith("#") or fields[0] == "local":
            lines.append(line)
            continue
        if fields[0] not in {"host", "hostssl", "hostnossl"} or len(fields) < 5:
            raise ValueError("unsupported_hba")
        if fields[4] not in {"trust", "md5", "scram-sha-256", "reject"}:
            raise ValueError("unsupported_hba")
        if fields[4] != "reject":
            fields[4] = "scram-sha-256"
            hosts += 1
        lines.append(" ".join(fields))
    if hosts == 0:
        raise ValueError("unsupported_hba")
    return "\n".join(lines) + "\n"


def private_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("unsafe_secret_path")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def checked_path(path: Path, *, mode: int, directory: bool = False) -> None:
    """Reject credential path drift without reading or reporting secret bytes."""
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("unsafe_secret_path")
    metadata = path.lstat()
    kind_ok = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not kind_ok or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != mode:
        raise ValueError("unsafe_secret_permissions")


class Operator:
    CONTAINER = "scientist-b5-postgres"
    HBA = "/var/lib/postgresql/18/docker/pg_hba.conf"

    def __init__(self, root: Path, config: dict):
        self.root, self.config = root.resolve(), config
        self.context = config["docker_context"]
        if self.context != "colima-scientist-platform-test":
            raise ValueError("wrong_context")
        self.directory = self.root / ".local" / "security" / "b5-scram-20261005"
        candidate = self.root / config["private_dir"]
        if any(parent.is_symlink() for parent in (candidate, *candidate.parents)):
            raise ValueError("unsafe_secret_path")
        self.private = candidate.resolve()
        if not self.private.is_relative_to(self.root / ".local"):
            raise ValueError("unsafe_secret_path")

    def docker(self, *args: str, data: str | None = None) -> str:
        result = subprocess.run(["docker", "--context", self.context, *args], input=data,
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            # Docker/psql error text can echo secret input. Only a fixed code escapes.
            raise RuntimeError("owned_docker_command_failed")
        return result.stdout.strip()

    def sql(self, statement: str) -> str:
        return self.docker("exec", "-i", self.config["expected_container_id"], "psql", "-X", "-v", "ON_ERROR_STOP=1",
                           "-U", "postgres", "-d", "scientist_b5", "-At", data=statement)

    def inspect(self) -> dict:
        if self.docker("info", "--format", "{{.ID}}") != self.config["expected_engine_id"]:
            raise ValueError("wrong_engine")
        service = json.loads(self.docker("inspect", self.CONTAINER))[0]
        expected = self.config.get("expected_container_id", "")
        if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected) or service["Id"] != expected:
            raise ValueError("wrong_container")
        if (service["Image"] != self.config["postgres_image"].split("@", 1)[1]
                or service["Config"].get("Labels", {}).get("scientist.platform/purpose") != "b5-services-test"
                or service["Config"]["User"] != "70:70"
                or not service["HostConfig"]["ReadonlyRootfs"]):
            raise ValueError("wrong_service")
        volumes = {(m["Name"], m["Destination"]) for m in service["Mounts"] if m["Type"] == "volume"}
        if volumes != {("scientist-b5-pgdata", "/var/lib/postgresql")}:
            raise ValueError("wrong_volume")
        if self.sql("SELECT count(*) FROM runs WHERE state IN ('queued','running','recovering','stopping');") != "0":
            raise ValueError("active_runs")
        if self.sql("SHOW hba_file;") != self.HBA:
            raise ValueError("wrong_hba_path")
        return {"engine_id": self.config["expected_engine_id"], "container_id": service["Id"],
                "image_id": service["Image"], "volume": "scientist-b5-pgdata"}

    def write_hba(self, text: str) -> None:
        # This fixed path is inside the already-inspected named volume, under UID70.
        self.docker("exec", "-i", self.config["expected_container_id"], "sh", "-c",
                    f"cat > {self.HBA}.codex-tmp && chmod 0600 {self.HBA}.codex-tmp && mv {self.HBA}.codex-tmp {self.HBA}", data=text)
        if self.sql("SELECT pg_reload_conf();") != "t":
            raise RuntimeError("reload_failed")
        if self.sql("SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL;") != "0":
            raise RuntimeError("hba_invalid")

    def verify(self) -> dict:
        identity = self.inspect()
        self.check_credentials()
        if self.sql("SELECT count(*) FROM pg_hba_file_rules WHERE type LIKE 'host%' AND auth_method NOT IN ('scram-sha-256','reject');") != "0":
            raise RuntimeError("host_auth_not_scram")
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url
        public_url = make_url(self.config["database_url"])
        # Negative check explicitly excludes every password source, including .pgpass.
        denied = create_engine(public_url, connect_args={"password": "incorrect-fixture", "require_auth": "scram-sha-256", "connect_timeout": 3})
        try:
            try:
                with denied.connect() as db:
                    db.execute(text("SELECT 1"))
            except Exception:
                pass
            else:
                raise RuntimeError("wrong_password_accepted")
        finally:
            denied.dispose()
        # Read secret only inside the operator process; never output the URL or errors.
        stored_url = make_url((self.directory / "database_url").read_text().strip())
        connection = create_engine(public_url.set(password=stored_url.password),
                                   connect_args={"require_auth": "scram-sha-256", "connect_timeout": 3})
        try:
            with connection.connect() as db:
                if db.execute(text("SELECT 1")).scalar_one() != 1:
                    raise RuntimeError("positive_auth_failed")
                actual_system = str(db.execute(text("SELECT system_identifier FROM pg_control_system()")).scalar_one())
                if actual_system != self.sql("SELECT system_identifier FROM pg_control_system();"):
                    raise RuntimeError("forwarded_service_identity_mismatch")
        except Exception:
            raise RuntimeError("positive_auth_failed") from None
        finally:
            connection.dispose()
        return {**identity, "status": "PASS", "wrong_password_rejected": True, "authenticated_tcp": True,
                "local_recovery_socket": True, "pgpass_file": str(self.directory / "pgpass"),
                "canonical_secret_mode": "0600", "runtime_mount_copy": "0444 beneath owned 0700 directory, individually mounted readonly"}

    def check_credentials(self) -> None:
        checked_path(self.directory, mode=0o700, directory=True)
        checked_path(self.private, mode=0o700, directory=True)
        for name in ("database_url", "pgpass"):
            checked_path(self.directory / name, mode=0o600)
        checked_path(self.private / "database_url", mode=0o444)

    def apply(self) -> dict:
        identity = self.inspect()
        checked_path(self.private, mode=0o700, directory=True)
        checked_path(self.private / "database_url", mode=0o444)
        if self.directory.exists():
            checked_path(self.directory, mode=0o700, directory=True)
        elif any(parent.is_symlink() for parent in self.directory.parents):
            raise ValueError("unsafe_secret_path")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        marker = self.directory / "manifest.json"
        if marker.exists():
            previous = json.loads(marker.read_text())
            if any(previous.get(key) != value for key, value in identity.items()):
                raise ValueError("resume_identity_changed")
            if previous.get("status") == "PASS":
                return self.verify()
            raise ValueError("incomplete_migration_requires_rollback")
        if self.sql("SELECT rolpassword IS NOT NULL FROM pg_authid WHERE rolname='postgres';") != "f":
            raise ValueError("existing_password_not_owned")
        before = self.docker("exec", self.config["expected_container_id"], "cat", self.HBA) + "\n"
        after = scram_hba(before)
        url_file = self.private / "database_url"
        previous_url = url_file.read_bytes()
        if (url_file.is_symlink() or url_file.stat().st_mode & 0o022
                or self.private.stat().st_mode & 0o077 or url_file.stat().st_uid != os.geteuid()):
            raise ValueError("unsafe_secret_path")
        private_write(self.directory / "hba.before", before.encode())
        private_write(self.directory / "database_url.before", previous_url)
        private_write(marker, json.dumps({**identity, "status": "PREPARED", "hba_before_sha256": hashlib.sha256(before.encode()).hexdigest()}).encode())
        password = secrets.token_urlsafe(48)
        from sqlalchemy.engine import make_url
        old_url = make_url(previous_url.decode().strip())
        if old_url.username != "postgres" or old_url.database != "scientist_b5":
            raise ValueError("wrong_secret_database")
        try:
            self.sql(f"SET password_encryption = 'scram-sha-256'; ALTER ROLE postgres PASSWORD '{password}';")
            secret_url = old_url.set(password=password).render_as_string(hide_password=False).encode()
            private_write(self.directory / "database_url", secret_url)
            # Keep the existing, reviewed Colima nonroot bind-mount profile: the
            # canonical credential above is 0600. This transport copy is never
            # reachable by other host users (owned parent 0700), and Docker
            # exposes only this file read-only to the trusted dispatch role.
            private_write(url_file, secret_url, mode=0o444)
            port = urlsplit(self.config["database_url"].replace("postgresql+psycopg:", "postgresql:")).port
            private_write(self.directory / "pgpass", f"127.0.0.1:{port}:scientist_b5:postgres:{password}\nlocalhost:{port}:scientist_b5:postgres:{password}\n".encode())
            self.write_hba(after)
            result = self.verify()
            private_write(marker, json.dumps(result).encode())
            return result
        except Exception:
            try:
                self.rollback()
            except Exception:
                raise RuntimeError("migration_failed_rollback_failed") from None
            raise RuntimeError("migration_failed_rolled_back") from None

    def rollback(self) -> dict:
        identity = self.inspect()
        checked_path(self.directory, mode=0o700, directory=True)
        checked_path(self.private, mode=0o700, directory=True)
        for name in ("manifest.json", "hba.before", "database_url.before"):
            checked_path(self.directory / name, mode=0o600)
        previous = json.loads((self.directory / "manifest.json").read_text())
        if any(previous.get(key) != value for key, value in identity.items()):
            raise ValueError("rollback_identity_changed")
        before = (self.directory / "hba.before").read_text()
        self.write_hba(before)
        self.sql("ALTER ROLE postgres PASSWORD NULL;")
        private_write(self.private / "database_url", (self.directory / "database_url.before").read_bytes(), mode=0o444)
        private_write(self.directory / "manifest.json", json.dumps({**identity, "status": "ROLLED_BACK"}).encode())
        return {"status": "ROLLED_BACK", **identity}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("plan", "apply", "verify", "rollback"))
    parser.add_argument("--config", type=Path, default=Path(".local/b5-live.json"))
    parser.add_argument("--owner-approved", action="store_true")
    args = parser.parse_args()
    if args.phase in {"apply", "rollback"} and not args.owner_approved:
        print(json.dumps({"status": "NOT RUN", "reason": "owner_approval_required"}))
        return 77
    try:
        root = Path(__file__).resolve().parents[3]
        operator = Operator(root, json.loads(args.config.read_text()))
        if args.phase == "plan":
            result = {**operator.inspect(), "status": "PLAN", "scope": "b5-only", "restart": False,
                      "rollback": "restore HBA and private database URL; remove newly owned role password via retained local socket"}
        else:
            result = getattr(operator, args.phase)()
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps({"status": "FAIL", "reason": "operator_gate_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
