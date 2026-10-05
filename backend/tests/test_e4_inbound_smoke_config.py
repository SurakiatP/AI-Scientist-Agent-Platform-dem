"""The operator smoke must distinguish missing prerequisites from acceptance."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent / "live/e4_inbound_http_smoke.py"
spec = importlib.util.spec_from_file_location("e4_inbound_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def config(tmp_path):
    fields = {}
    for key in ("database_url_file", "s3_access_key_file", "s3_secret_key_file", "image_verification"):
        path = tmp_path / key
        path.write_text("synthetic-secret-that-must-never-be-reported")
        path.chmod(0o600)
        fields[key] = str(path)
    fields.update(s3_endpoint="http://127.0.0.1:54332", bucket="scientist-b5",
                  postgres_container_id="a" * 64, minio_container_id="b" * 64)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(fields))
    path.chmod(0o600)
    return path, fields


def test_missing_config_is_not_run_and_does_not_create_evidence(tmp_path, capsys):
    evidence = tmp_path / "evidence"
    assert smoke.main(["--config", str(tmp_path / "missing"), "--evidence", str(evidence)]) == 77
    assert json.loads(capsys.readouterr().out)["status"] == "NOT RUN"
    assert not evidence.exists()


def test_readable_secret_rejects_before_any_engine_call(tmp_path, monkeypatch, capsys):
    path, fields = config(tmp_path)
    Path(fields["s3_secret_key_file"]).chmod(0o644)
    monkeypatch.setattr(smoke, "preflight", lambda _: pytest.fail("engine must not be called"))
    evidence = tmp_path / "evidence"
    assert smoke.main(["--config", str(path), "--evidence", str(evidence)]) == 77
    output = capsys.readouterr().out
    assert "synthetic-secret" not in output
    assert not evidence.exists()


def test_reused_evidence_is_never_overwritten_or_used_for_a_new_run(tmp_path, monkeypatch, capsys):
    path, _ = config(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    proof = evidence / "proof.json"
    proof.write_text('{"status":"FAIL","original":true}\n')
    monkeypatch.setattr(smoke, "preflight", lambda _: pytest.fail("engine must not be called"))
    assert smoke.main(["--config", str(path), "--evidence", str(evidence)]) == 77
    assert json.loads(capsys.readouterr().out)["status"] == "NOT RUN"
    assert proof.read_text() == '{"status":"FAIL","original":true}\n'


def test_preflight_failure_is_fail_and_does_not_echo_exception_secrets(tmp_path, monkeypatch, capsys):
    path, _ = config(tmp_path)
    evidence = tmp_path / "evidence"
    def failed(_):
        raise RuntimeError("synthetic-secret-that-must-never-be-reported")
    monkeypatch.setattr(smoke, "preflight", failed)
    assert smoke.main(["--config", str(path), "--evidence", str(evidence)]) == 1
    output = capsys.readouterr().out
    assert "synthetic-secret" not in output
    result = json.loads((evidence / "proof.json").read_text())
    assert result["status"] == "FAIL"
    assert result["failure_class"] == "RuntimeError"
    assert result["aggregate_e4"] == "NOT RUN"


@pytest.mark.parametrize("endpoint", ["https://foreign.invalid", "http://127.0.0.1:54332?secret=private",
                                     "http://user:private@127.0.0.1:54332"])
def test_storage_endpoint_cannot_be_changed_into_egress(tmp_path, endpoint):
    path, fields = config(tmp_path)
    fields["s3_endpoint"] = endpoint
    path.write_text(json.dumps(fields))
    with pytest.raises(smoke.NotRun, match="storage_endpoint"):
        smoke.load_config(path)
