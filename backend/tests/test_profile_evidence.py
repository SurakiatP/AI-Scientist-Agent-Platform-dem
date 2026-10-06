"""Synthetic signed receipts verify reuse boundaries; they are not actual acceptance."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
import os
from uuid import uuid4

import pytest

from scientist import profile_evidence as evidence, profile_preparation as prep

KEY = b"synthetic-test-receipt-key-only!!!"
IMAGE = "sha256:" + "a" * 64


def synthetic_proof(profile):
    now = datetime.now(timezone.utc)
    return dict(schema_version=1, job_id=str(uuid4()), profile_id=profile.profile_id,
                version=profile.version, manifest_sha256=profile.manifest_sha256,
                image_digest=IMAGE, runtime_commit=profile.runtime_commit,
                catalog_commit=profile.catalog_commit, python_version=profile.python_version,
                architecture=profile.architecture, lock_sha256=profile.lock_sha256,
                skills_manifest_sha256=profile.skills_manifest_sha256,
                built_at=(now-timedelta(days=30)).isoformat(), checked_at=now.isoformat(),
                source_manifest_sha256="b" * 64, build_network="none",
                compatibility_checks=profile.compatibility_checks,
                scan=dict(report_sha256="c" * 64, scanned_at=now.isoformat(),
                          database_updated_at=(now-timedelta(hours=1)).isoformat(),
                          database_next_update=(now+timedelta(hours=12)).isoformat(),
                          high=0, critical=0, os_packages=14, python_packages=69),
                sbom_sha256="d" * 64, sbom_packages=97, license_approved=True,
                containment=dict(uid=65532, read_only=True, network="none", cap_drop_all=True,
                                 memory_limit_bytes=profile.memory_limit_bytes,
                                 workspace_limit_bytes=profile.workspace_limit_bytes,
                                 pids_limit=128, docker_socket_absent=True, unrelated_host_mounts_absent=True,
                                 direct_egress_denied=True, unreviewed_code_disabled=True,
                                 isolation_test_sha256="e" * 64))


def sign(directory, payload, key=KEY):
    """Fixture signing only; production exposes no acceptance/sealing function."""
    signature = hmac.digest(key, evidence.DOMAIN + evidence._bytes(payload), "sha256").hex()
    (directory / "accepted.json").write_bytes(evidence._bytes({**payload, "signature": signature}))


@pytest.fixture
def accepted(tmp_path):
    root = tmp_path.resolve() / "source"
    directory = tmp_path.resolve() / "receipts"
    directory.mkdir(mode=0o700)
    sources = {}
    for relative in evidence.SOURCE_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        content = ("synthetic source: " + relative).encode()
        path.write_bytes(content)
        sources[relative] = sha256(content).hexdigest()
    files = {}
    for name in ("source.json", "scan.json", "sbom.json", "isolation.json", "compatibility.json"):
        content = json.dumps({"synthetic_evidence": name}).encode()
        (directory / name).write_bytes(content)
        files[name] = sha256(content).hexdigest()
    profile = next(iter(prep.load_profiles().values()))
    proof = synthetic_proof(profile)
    proof.update(source_manifest_sha256=files["source.json"], sbom_sha256=files["sbom.json"])
    proof["scan"]["report_sha256"] = files["scan.json"]
    proof["containment"]["isolation_test_sha256"] = files["isolation.json"]
    payload = dict(proof=proof, files=files, source_sha256=sources)
    sign(directory, payload)
    return directory, root, profile, payload


def verify(accepted, **options):
    directory, root, profile, _ = accepted
    callback = evidence.accepted_image_builder(directory, key=KEY, expected_image_digest=IMAGE,
                                               source_root=root, **options)
    return callback(profile, uuid4())


def test_reuses_signed_immutable_image_and_rebinds_only_job_id(accepted):
    directory, root, profile, payload = accepted
    before = (directory / "accepted.json").read_bytes()
    callback = evidence.accepted_image_builder(directory, key=KEY, expected_image_digest=IMAGE, source_root=root)
    first, second = uuid4(), uuid4()
    expected = prep.BuildProof.model_validate({**payload["proof"], "job_id": str(first)}).model_dump(mode="json")
    assert callback(profile, first) == expected
    assert callback(profile, second)["job_id"] == str(second)
    assert (directory / "accepted.json").read_bytes() == before


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(profile_id="unknown"),
    lambda p: p.update(version="2"),
    lambda p: p.update(manifest_sha256="0" * 64),
    lambda p: p.update(image_digest="sha256:" + "f" * 64),
    lambda p: p.update(runtime_commit="changed"),
    lambda p: p.update(catalog_commit="changed"),
    lambda p: p.update(python_version="3.13"),
    lambda p: p.update(architecture="linux/amd64"),
    lambda p: p.update(lock_sha256={}),
    lambda p: p.update(skills_manifest_sha256="0" * 64),
    lambda p: p.update(compatibility_checks=[]),
    lambda p: p.update(license_approved=False),
    lambda p: p.update(checked_at=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()),
    lambda p: p["scan"].update(scanned_at=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()),
    lambda p: p["scan"].update(database_next_update=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()),
    lambda p: p["scan"].update(high=1),
    lambda p: p["scan"].update(critical=1),
    lambda p: p["scan"].update(os_packages=0),
    lambda p: p["scan"].update(python_packages=0),
    lambda p: p["containment"].update(read_only=False),
    lambda p: p["containment"].update(network="bridge"),
    lambda p: p["containment"].update(unreviewed_code_disabled=False),
    lambda p: p["containment"].update(memory_limit_bytes=1073741825),
])
def test_signed_receipt_still_requires_current_exact_security_proof(accepted, mutate):
    directory, _, _, payload = accepted
    mutate(payload["proof"])
    sign(directory, payload)
    with pytest.raises(prep.BuildFailure, match="accepted_environment_unavailable"):
        verify(accepted)


@pytest.mark.parametrize("name", ["source_manifest_sha256", "scan", "sbom_sha256", "containment"])
def test_raw_security_evidence_identities_are_mandatory(accepted, name):
    directory, _, _, payload = accepted
    if name == "scan":
        payload["proof"][name]["report_sha256"] = "0" * 64
    elif name == "containment":
        payload["proof"][name]["isolation_test_sha256"] = "0" * 64
    else:
        payload["proof"][name] = "0" * 64
    sign(directory, payload)
    with pytest.raises(prep.BuildFailure):
        verify(accepted)


@pytest.mark.parametrize("action", ["change", "missing", "symlink", "writable", "fifo"])
def test_unsafe_or_changed_evidence_rejected(accepted, action):
    directory, _, _, _ = accepted
    path = directory / "scan.json"
    if action == "change":
        path.write_text("changed")
    elif action == "writable":
        path.chmod(0o666)
    else:
        content = path.read_bytes()
        path.unlink()
        if action == "symlink":
            target = directory / "target.json"
            target.write_bytes(content)
            path.symlink_to(target)
        elif action == "fifo":
            os.mkfifo(path)
    with pytest.raises(prep.BuildFailure):
        verify(accepted)


@pytest.mark.parametrize("action", ["change", "missing", "incomplete", "extra", "symlink_ancestor"])
def test_source_binding_is_complete_and_unchanged(accepted, action):
    directory, root, _, payload = accepted
    path = root / "runtime/entrypoint.py"
    if action == "change":
        path.write_text("changed")
    elif action == "missing":
        path.unlink()
    elif action == "incomplete":
        payload["source_sha256"].pop("runtime/entrypoint.py")
    elif action == "extra":
        payload["source_sha256"]["extra.py"] = "0" * 64
    else:
        actual = root / "runtime-real"
        path.parent.rename(actual)
        (root / "runtime").symlink_to(actual, target_is_directory=True)
    sign(directory, payload)
    with pytest.raises(prep.BuildFailure):
        verify(accepted)


@pytest.mark.parametrize("action", ["signature", "malformed", "nested", "duplicate", "traversal", "mode", "owner", "symlink_ancestor"])
def test_invalid_receipt_or_directory_rejected(accepted, action, monkeypatch):
    directory, root, profile, payload = accepted
    receipt = directory / "accepted.json"
    if action == "signature":
        sign(directory, payload, b"wrong" * 8)
    elif action == "malformed":
        receipt.write_text("{")
    elif action == "nested":
        receipt.write_text("[" * 2000 + "0" + "]" * 2000)
    elif action == "duplicate":
        receipt.write_bytes(receipt.read_bytes().replace(b'{', b'{"signature":"invalid",', 1))
    elif action == "traversal":
        payload["files"]["../escape.json"] = "0" * 64
        sign(directory, payload)
    elif action == "mode":
        directory.chmod(0o755)
    elif action == "owner":
        monkeypatch.setattr(evidence.os, "getuid", lambda: directory.stat().st_uid + 1)
    else:
        link = directory.parent / "linked"
        link.symlink_to(directory.parent, target_is_directory=True)
        directory = link / directory.name
    with pytest.raises(prep.BuildFailure):
        evidence.accepted_image_builder(directory, key=KEY, expected_image_digest=IMAGE, source_root=root)(profile, uuid4())


@pytest.mark.parametrize("key,image", [(b"short", IMAGE), ("x" * 32, IMAGE), (KEY, "mutable:latest"), (KEY, "sha256:" + "A" * 64)])
def test_factory_rejects_invalid_trusted_configuration(tmp_path, key, image):
    with pytest.raises(ValueError):
        evidence.accepted_image_builder(tmp_path, key=key, expected_image_digest=image)


def test_deployed_registry_fallback_is_fixed_and_hash_bound(accepted):
    _, root, _, _ = accepted
    registry = root / "docs/skills/capability-registry.json"
    fallback = root / "runtime/capability-registry.json"
    registry.rename(fallback)
    assert verify(accepted)["image_digest"] == IMAGE
    fallback.write_text("changed")
    with pytest.raises(prep.BuildFailure):
        verify(accepted)


@pytest.mark.parametrize("action", ["original_symlink", "fallback_symlink", "fallback_ancestor"])
def test_registry_fallback_does_not_bypass_symlink_checks(accepted, action):
    _, root, _, _ = accepted
    registry = root / "docs/skills/capability-registry.json"
    fallback = root / "runtime/capability-registry.json"
    content = registry.read_bytes()
    registry.unlink()
    fallback.write_bytes(content)
    if action == "original_symlink":
        registry.symlink_to(root / "missing")
    elif action == "fallback_symlink":
        fallback.unlink()
        target = root / "registry.json"
        target.write_bytes(content)
        fallback.symlink_to(target)
    else:
        actual = root / "runtime-real"
        fallback.parent.rename(actual)
        (root / "runtime").symlink_to(actual, target_is_directory=True)
    with pytest.raises(prep.BuildFailure):
        verify(accepted)


def test_receipt_is_read_once_and_bounded(accepted, monkeypatch):
    original = evidence._chunks
    receipt_reads = []

    def observed(path, maximum, **options):
        if path.name == "accepted.json":
            receipt_reads.append(maximum)
        yield from original(path, maximum, **options)

    monkeypatch.setattr(evidence, "_chunks", observed)
    verify(accepted)
    assert receipt_reads == [1024 * 1024]


@pytest.mark.parametrize("name", ["accepted.json", "scan.json"])
def test_private_receipt_and_evidence_require_current_file_owner(accepted, monkeypatch, name):
    directory, _, _, _ = accepted
    original = evidence.os.fstat
    inode = (directory / name).stat().st_ino

    def wrong_owner(descriptor):
        info = original(descriptor)
        if info.st_ino == inode:
            fields = list(info)
            fields[4] = info.st_uid + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(evidence.os, "fstat", wrong_owner)
    with pytest.raises(prep.BuildFailure):
        verify(accepted)


def test_digest_rejects_file_growth_after_stat_with_bounded_reads(tmp_path, monkeypatch):
    path = tmp_path.resolve() / "small.json"
    path.write_bytes(b"x")
    requested = []

    def growing_read(fd, count):
        requested.append(count)
        return b"x" * count

    monkeypatch.setattr(evidence.os, "read", growing_read)
    with pytest.raises(ValueError):
        evidence._digest(path, 10)
    assert requested == [11]
