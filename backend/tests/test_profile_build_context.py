from pathlib import Path

import pytest

from scientist import profile_preparation as prep
from scientist.auth import DomainError


def test_profile_pins_existing_actual_interpreter_and_hash_locked_sources():
    profiles = prep.load_profiles()
    assert list(profiles) == ["prof.worker-base@py3.14.7"]
    profile = next(iter(profiles.values()))
    assert profile.python_version == "3.14.7"
    assert set(profile.lock_sha256) == {"runtime/requirements.lock"}
    assert profile.compatibility_checks == ["python_version", "native_agent_import", "scientific_resource_tool"]
    assert not {"command", "packages", "environment", "credentials"}.intersection(profile.model_dump())


def test_profile_lock_drift_or_symlink_fails_closed(tmp_path, monkeypatch):
    root = Path(prep.__file__).resolve().parents[3]
    profile_dir = tmp_path / "runtime/profiles"
    profile_dir.mkdir(parents=True)
    (profile_dir / "worker-base.json").write_bytes((root / "runtime/profiles/worker-base.json").read_bytes())
    (tmp_path / "runtime/skills-manifest.json").write_bytes((root / "runtime/skills-manifest.json").read_bytes())
    (tmp_path / "runtime/requirements.lock").write_text("unreviewed package\n")
    with pytest.raises(DomainError):
        prep.load_profiles(tmp_path)
    (tmp_path / "runtime/requirements.lock").unlink()
    (tmp_path / "runtime/requirements.lock").symlink_to(root / "runtime/requirements.lock")
    with pytest.raises(DomainError):
        prep.load_profiles(tmp_path)
