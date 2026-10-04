import json
import re
import subprocess
import sys
from pathlib import Path

LIVE = Path(__file__).parent / "live"
REPO = Path(__file__).resolve().parents[2]
SHA = re.compile(r"sha256:[0-9a-f]{64}")
CRED = re.compile(r"""(?i)(password|secret_key|access_key)\s*=\s*["'][^"'{]|AKIA[0-9A-Z]{16}""")
SHA_OK_FILES = {"b5_live.example.json", "README.md", "Dockerfile"}
SHA_OK_LINES = ("HISTORICAL_BOOTSTRAP_IMAGE", '"image_id": "sha256:8cbde14c')


def run(args, env_extra):
    import os
    env = {**os.environ, **env_extra}
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, env=env, cwd=REPO)


def test_loader_missing_config_is_not_run(tmp_path):
    out = run([str(LIVE / "b5_live_config.py")], {"B5_LIVE_CONFIG": str(tmp_path / "none.json"),
                                                   "B5_LIVE_EVIDENCE_DIR": str(tmp_path)})
    assert out.returncode == 77 and "NOT RUN" in out.stdout


def test_evidence_dir_inside_backend_is_not_run(tmp_path):
    out = run([str(LIVE / "b5_live_config.py")], _valid_config_env(tmp_path, evidence=str(LIVE)))
    assert out.returncode == 77 and "evidence dir must be outside" in out.stdout


def test_database_url_with_password_is_not_run(tmp_path):
    env = _valid_config_env(tmp_path, evidence=str(tmp_path), database_url="postgresql+psycopg://u:pw@127.0.0.1:1/scientist_b5")
    out = run([str(LIVE / "b5_live_config.py")], env)
    assert out.returncode == 77 and "must not contain a password" in out.stdout


def _valid_config_env(tmp_path, evidence, **overrides):
    private = tmp_path / "private"
    private.mkdir()
    for name in ("broker_capability_key", "database_url", "master_key", "s3_access_key", "s3_secret_key"):
        (private / name).touch()
    hashes = tmp_path / "hashes.json"
    hashes.write_text("{}")
    cfg = json.loads((LIVE / "b5_live.example.json").read_text())
    cfg.update(private_dir=str(private), server_source_hashes=str(hashes), **overrides)
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg))
    return {"B5_LIVE_CONFIG": str(cfg_path), "B5_LIVE_EVIDENCE_DIR": evidence}


def test_runner_missing_config_is_not_run(tmp_path):
    out = run([str(LIVE / "run_b5_live.py"), "--evidence-dir", str(tmp_path / "ev")],
              {"B5_LIVE_CONFIG": str(tmp_path / "none.json")})
    assert out.returncode == 77 and "NOT RUN" in out.stdout


def test_live_tree_hygiene():
    for path in LIVE.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            where = f"{path.name}:{n}"
            assert "/Users/" not in line and "/home/" not in line.replace("/home/scientist", ""), where  # container user home is fine
            assert ".local/security" not in line, where
            assert not CRED.search(line), where
            if SHA.search(line) and path.name not in SHA_OK_FILES:
                assert any(tok in line for tok in SHA_OK_LINES), where
