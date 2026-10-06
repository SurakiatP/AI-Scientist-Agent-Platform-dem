from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import text

from scientist import domain
from scientist.auth import DomainError
from scientist.contracts import (
    ComputeProfilePin,
    CsvDescribeGrantV1,
    ObjectRef,
    RuntimePins,
    ScientificBindingV2,
    Principal,
)
from scientist.scientific_authority import _validate_csv_snapshot_membership


PROJECT_ID = UUID("00000000-0000-0000-0000-000000000001")
RUN_ID = UUID("00000000-0000-0000-0000-000000000002")


class SnapshotDB:
    def __init__(self, rows, stored=None):
        self.rows = rows
        self.stored = stored
        self.params = None
        self.query = None
        self.calls = []

    def execute(self, query, params):
        self.query = str(query)
        self.params = params
        self.calls.append((self.query, params))
        if "FROM stored_objects" in self.query:
            return SimpleNamespace(one_or_none=lambda: self.stored)
        return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: self.rows))


def _binding(project_id=PROJECT_ID, key=None, digest="3" * 64, size=128, snapshot="2" * 64):
    input_ref = ObjectRef(
        project_id=project_id,
        key=key or f"{project_id}/{digest}",
        sha256=digest,
        size=size,
        content_type="application/octet-stream",
    )
    grant = CsvDescribeGrantV1(
        recipe_id="csv.describe.v1",
        recipe_version="1",
        recipe_manifest_sha256="a" * 64,
        profile_id="prof.csv-stdlib@py3.14.7",
        profile_version="1",
        image_digest="sha256:" + "b" * 64,
        input_ref=input_ref,
        input_sha256=input_ref.sha256,
        numeric_columns=["measurement"],
    )
    return ScientificBindingV2(
        binding_version=2,
        catalog_commit="154988403bb5a18e9d3c0ce4e6d5e2e4b184a298",
        registry_sha256="c" * 64,
        capability_ids=["exploratory-data-analysis"],
        instruction_fingerprint="d" * 64,
        agent_runtime_pins=RuntimePins(
            runtime_commit="bd0affe5e5f723579df8902852f5d0c47795f355",
            image_digest="sha256:" + "e" * 64,
            skills_digest="f" * 64,
            environment_digest="1" * 64,
        ),
        input_snapshot_digest=snapshot,
        required_compute_profiles=[
            ComputeProfilePin(
                profile_id=grant.profile_id,
                version=grant.profile_version,
                image_digest=grant.image_digest,
            )
        ],
        csv_describe_grants={"grant-1": grant},
    )


def _file(**changes):
    return {
        "object_key": f"{PROJECT_ID}/{'3' * 64}",
        "sha256": "3" * 64,
        "size": 128,
        "content_type": "text/csv",
        **changes,
    }


def test_csv_grant_must_match_ready_file_in_exact_bound_run_snapshot():
    stored = SimpleNamespace(project_id=PROJECT_ID, sha256="3" * 64, size=128,
                             content_type="application/octet-stream")
    db = SnapshotDB([{"manifest": {"files": [_file()]}}], stored)

    _validate_csv_snapshot_membership(db, PROJECT_ID, _binding(), run_id=RUN_ID)

    assert db.calls[0][1] == {"project": PROJECT_ID, "digest": "2" * 64, "run": RUN_ID}
    assert "run_id = :run" in db.calls[0][0]


@pytest.mark.parametrize(
    "manifest",
    [
        {"files": [_file(object_key="other/3")]},
        {"files": [_file(sha256="4" * 64)]},
        {"files": [_file(size=127)]},
        {"files": [_file(content_type="application/octet-stream")]},
        {"files": [_file(state="preparing")]},
        {"files": [_file(state="failed")]},
        {"findings": [], "files": []},
    ],
)
def test_csv_grant_rejects_input_outside_exact_ready_snapshot(manifest):
    stored = SimpleNamespace(project_id=PROJECT_ID, sha256="3" * 64, size=128,
                             content_type="application/octet-stream")
    db = SnapshotDB([{"manifest": manifest}], stored)

    with pytest.raises(DomainError, match="scientific_input_unavailable"):
        _validate_csv_snapshot_membership(db, PROJECT_ID, _binding(), run_id=RUN_ID)


def test_csv_grant_requires_the_exact_project_snapshot():
    db = SnapshotDB([])

    with pytest.raises(DomainError, match="scientific_input_unavailable"):
        _validate_csv_snapshot_membership(db, PROJECT_ID, _binding(), run_id=RUN_ID)


@pytest.mark.parametrize(
    "stored",
    [
        None,
        SimpleNamespace(project_id=PROJECT_ID, sha256="3" * 64, size=128, content_type="text/csv"),
        SimpleNamespace(project_id=UUID("00000000-0000-0000-0000-000000000099"),
                        sha256="3" * 64, size=128, content_type="application/octet-stream"),
        SimpleNamespace(project_id=PROJECT_ID, sha256="4" * 64, size=128,
                        content_type="application/octet-stream"),
        SimpleNamespace(project_id=PROJECT_ID, sha256="3" * 64, size=127,
                        content_type="application/octet-stream"),
    ],
)
def test_csv_grant_requires_canonical_stored_object_identity(stored):
    db = SnapshotDB([{"manifest": {"files": [_file()]}}], stored)

    with pytest.raises(DomainError, match="scientific_input_unavailable"):
        _validate_csv_snapshot_membership(db, PROJECT_ID, _binding(), run_id=RUN_ID)


def test_submitted_run_snapshot_accepts_canonical_ready_csv_ref(db, project_session):
    project_id, session_id = project_session
    owner = Principal(
        identity=UUID("00000000-0000-0000-0000-000000000099"), kind="owner"
    )
    digest = sha256(b"measurement\n1\n").hexdigest()
    size = len(b"measurement\n1\n")
    key = f"{project_id}/{digest}"
    file_id = UUID("00000000-0000-0000-0000-000000000098")
    db.execute(
        text("""
            INSERT INTO stored_objects (key, project_id, sha256, size, content_type)
            VALUES (:key, :project, :sha, :size, 'application/octet-stream')
        """), {"key": key, "project": project_id, "sha": digest, "size": size}
    )
    db.execute(
        text("""
            INSERT INTO file_versions (id, project_id, filename, object_key, size,
                                       content_type, state, sha256)
            VALUES (:id, :project, 'data.csv', :key, :size, 'text/csv', 'ready', :sha)
        """), {"id": file_id, "project": project_id, "key": key, "size": size, "sha": digest}
    )

    run = domain.submit_run(db, owner, project_id, session_id, "csv-snapshot", "Describe CSV",
                            [file_id], UUID("00000000-0000-0000-0000-000000000097"), "fixture")
    snapshot = db.execute(
        text("SELECT digest, manifest FROM input_snapshots WHERE run_id = :run"),
        {"run": run.run_id},
    ).one()
    assert "state" not in snapshot.manifest["files"][0]
    binding = _binding(project_id, key, digest, size, snapshot.digest)

    _validate_csv_snapshot_membership(db, project_id, binding, run_id=run.run_id)
