"""Offline checks for the isolated live-service authentication migration."""
import importlib.util
from pathlib import Path

import pytest


def helper():
    spec = importlib.util.spec_from_file_location("b5_scram", Path(__file__).parent / "live" / "b5_scram.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scram_conversion_preserves_local_recovery_and_requires_auth_for_every_host():
    source = "# retained\nlocal all all trust\nhost all all 127.0.0.1/32 trust\nhost all all ::1/128 trust\nhost all all all trust\n"
    converted = helper().scram_hba(source)
    assert "local all all trust" in converted
    assert converted.count("scram-sha-256") == 3
    assert "host all all all trust" not in converted


def test_scram_conversion_rejects_unrecognized_auth_instead_of_guessing():
    with pytest.raises(ValueError, match="unsupported_hba"):
        helper().scram_hba("host all all all custom-auth\n")


def test_canonical_secret_write_is_private_and_rejects_symlinks(tmp_path):
    secret = tmp_path / "credential"
    helper().private_write(secret, b"synthetic-secret")
    assert secret.read_bytes() == b"synthetic-secret"
    assert secret.stat().st_mode & 0o777 == 0o600
    helper().private_write(secret, b"replacement-synthetic-secret")
    assert secret.read_bytes() == b"replacement-synthetic-secret"
    alias = tmp_path / "alias"
    alias.symlink_to(secret)
    with pytest.raises(ValueError, match="unsafe_secret_path"):
        helper().private_write(alias, b"must-not-overwrite")
    assert secret.read_bytes() == b"replacement-synthetic-secret"
import json
import os
from unittest.mock import Mock


def test_scram_rejects_replaced_reviewed_container(tmp_path):
    Operator = helper().Operator

    operator = Operator(tmp_path, {
        "docker_context": "colima-scientist-platform-test",
        "private_dir": ".local/private",
        "expected_engine_id": "engine",
        "expected_container_id": "a" * 64,
        "postgres_image": "postgres@sha256:owned",
    })
    operator.docker = Mock(side_effect=["engine", json.dumps([{"Id": "b" * 64}])])
    operator.sql = Mock()
    with pytest.raises(ValueError, match="wrong_container"):
        operator.inspect()
    operator.sql.assert_not_called()


@pytest.mark.parametrize("target,mode", [("database_url", 0o644), ("pgpass", 0o644), ("runtime", 0o600), ("directory", 0o755)])
def test_scram_verify_rejects_permission_drift_before_auth(tmp_path, target, mode):
    Operator = helper().Operator

    operator = Operator(tmp_path, {
        "docker_context": "colima-scientist-platform-test", "private_dir": ".local/private",
    })
    operator.directory.mkdir(parents=True, mode=0o700)
    operator.private.mkdir(mode=0o700)
    for name in ("database_url", "pgpass"):
        path = operator.directory / name
        path.write_text("synthetic")
        path.chmod(0o600)
    runtime = operator.private / "database_url"
    runtime.write_text("synthetic")
    runtime.chmod(0o444)
    path = {"runtime": runtime, "directory": operator.directory}.get(target, operator.directory / target)
    path.chmod(mode)
    operator.inspect = Mock(return_value={})
    operator.sql = Mock()
    with pytest.raises(ValueError, match="unsafe_secret_permissions"):
        operator.verify()
    operator.sql.assert_not_called()


def test_scram_checked_path_rejects_symlink_and_owner_drift(tmp_path, monkeypatch):
    checked_path = helper().checked_path

    path = tmp_path / "credential"
    path.write_text("synthetic")
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="unsafe_secret_path"):
        checked_path(link, mode=0o600)
    monkeypatch.setattr(os, "geteuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(ValueError, match="unsafe_secret_permissions"):
        checked_path(path, mode=0o600)
