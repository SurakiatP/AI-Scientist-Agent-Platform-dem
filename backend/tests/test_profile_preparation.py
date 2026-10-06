"""Trusted preparation uses actual PostgreSQL; these are engineering fixtures."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from scientist import profile_preparation as prep, domain, settings, db as database
from scientist.auth import DomainError
from scientist.contracts import PreparationSubmit, Principal
from scientist.db import create_project, create_session, session
from test_scientific_contracts import binding


@pytest.fixture(autouse=True)
def reset_builder():
    prep.configure_builder(None)
    yield
    prep.configure_builder(None)


@pytest.fixture
def owner():
    return Principal(identity=uuid4(), kind="owner")


@pytest.fixture
def db(migrated_database, monkeypatch):
    """Each committed-state case gets its own database; retained as synthetic evidence."""
    base = database.engine().url
    _, connection = base.get_dialect()().create_connect_args(base)
    name = "scientist_test_l1" + uuid4().hex
    with psycopg.connect(**{**connection, "dbname": "postgres"}, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    engine = create_engine(base.set(database=name), pool_pre_ping=True)
    database.migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(database, "_sessions", factory)
    with factory() as connection:
        yield connection
    engine.dispose()


def submission(**changes):
    profile = next(iter(prep.load_profiles().values()))
    return PreparationSubmit(**dict(profile_id=profile.profile_id, version=profile.version,
                                   manifest_sha256=profile.manifest_sha256, request_id=uuid4(), **changes))


def proof(profile, job_id):
    now = datetime.now(timezone.utc)
    return dict(schema_version=1, job_id=str(job_id), profile_id=profile.profile_id,
                version=profile.version, manifest_sha256=profile.manifest_sha256,
                image_digest="sha256:" + "a" * 64, runtime_commit=profile.runtime_commit,
                catalog_commit=profile.catalog_commit, python_version=profile.python_version,
                architecture=profile.architecture, lock_sha256=profile.lock_sha256,
                skills_manifest_sha256=profile.skills_manifest_sha256,
                built_at=now.isoformat(), checked_at=now.isoformat(),
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


def new_project(db):
    project = create_project(db, "Preparation fixture")
    db.commit()
    return project


def test_reads_do_not_prepare_or_capture_secret(db, owner, monkeypatch):
    project = new_project(db)
    called = []
    prep.configure_builder(lambda *_: called.append(True), evidence_key=b"k" * 32)
    monkeypatch.setattr(prep.secretstore, "list_connections", lambda *_: [])
    setup = prep.get_setup(db, owner, project)
    assert setup.profiles[0].state == "missing"
    assert setup.preparations == [] and called == []
    assert db.execute(text("SELECT count(*) FROM profile_preparations WHERE owner_identity=:o"),
                      {"o": owner.identity}).scalar_one() == 0


def test_idempotency_and_project_bound_readback(db, owner):
    project, other = new_project(db), new_project(db)
    body = submission()
    first = prep.request_preparation(db, owner, project, body)
    db.commit()
    assert prep.request_preparation(db, owner, project, body).id == first.id
    with pytest.raises(DomainError) as error:
        prep.request_preparation(db, owner, other, body)
    assert error.value.code == "idempotency_conflict"
    with pytest.raises(DomainError) as error:
        prep.get_job(db, owner, other, first.id)
    assert error.value.status == 404
    outsider = Principal(identity=uuid4(), kind="owner")
    with pytest.raises(DomainError):
        prep.get_job(db, outsider, project, first.id)


def test_concurrent_requests_share_one_recorded_job(db, owner):
    project = new_project(db)

    def request():
        with session() as connection:
            result = prep.request_preparation(connection, owner, project, submission())
            connection.commit()
            return result.id

    with ThreadPoolExecutor(max_workers=3) as pool:
        ids = list(pool.map(lambda _: request(), range(3)))
    assert len(set(ids)) == 1
    assert db.execute(text("SELECT count(*) FROM profile_preparations WHERE owner_identity=:o"),
                      {"o": owner.identity}).scalar_one() == 1


def test_cross_owner_builds_cannot_overlap(db, owner):
    other = Principal(identity=uuid4(), kind="owner")
    first_project, second_project = new_project(db), new_project(db)
    first = prep.request_preparation(db, owner, first_project, submission())
    second = prep.request_preparation(db, other, second_project, submission())
    db.commit()
    entered, release, calls = Event(), Event(), []

    def builder(profile, job_id):
        calls.append(job_id)
        if job_id == first.id:
            entered.set()
            assert release.wait(5)
        return proof(profile, job_id)

    def process(identity):
        with session() as connection:
            return prep.process_next_job(connection, owner_identity=identity)

    prep.configure_builder(builder, expected_image_digest="sha256:" + "a" * 64, evidence_key=b"k" * 32)
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(process, owner.identity)
        try:
            assert entered.wait(5)
            assert pool.submit(process, other.identity).result(timeout=5) is None
            assert calls == [first.id]
            assert prep.get_job(db, other, second_project, second.id).state == "queued"
        finally:
            release.set()
        assert running.result(timeout=5) == first.id
    assert process(other.identity) == second.id
    assert calls == [first.id, second.id]


def test_unknown_build_blocks_another_owner(db, owner):
    other = Principal(identity=uuid4(), kind="owner")
    first_project, second_project = new_project(db), new_project(db)
    first = prep.request_preparation(db, owner, first_project, submission())
    second = prep.request_preparation(db, other, second_project, submission())
    db.commit()
    calls = []

    def builder(_, job_id):
        calls.append(job_id)
        raise prep.BuildOutcomeUnknown()

    prep.configure_builder(builder, expected_image_digest="sha256:" + "a" * 64, evidence_key=b"k" * 32)
    assert prep.process_next_job(db, owner_identity=owner.identity) == first.id
    assert prep.process_next_job(db, owner_identity=other.identity) is None
    assert calls == [first.id]
    assert prep.get_job(db, owner, first_project, first.id).state == "unknown"
    assert prep.get_job(db, other, second_project, second.id).state == "queued"


@pytest.mark.parametrize("change", [{"profile_id": "unknown"}, {"version": "unknown"},
                                   {"manifest_sha256": "0" * 64}])
def test_unknown_profile_version_or_manifest_cannot_enqueue(db, owner, change):
    project = new_project(db)
    raw = submission().model_dump()
    raw.update(change)
    with pytest.raises(DomainError):
        prep.request_preparation(db, owner, project, PreparationSubmit.model_validate(raw))


def test_foreign_principal_cannot_read_or_submit(db, owner):
    project = new_project(db)
    foreign = Principal(identity=owner.identity, kind="external")
    for action in [lambda: prep.get_setup(db, foreign, project),
                   lambda: prep.request_preparation(db, foreign, project, submission())]:
        with pytest.raises(DomainError) as error:
            action()
        assert error.value.status == 403


def test_missing_trusted_builder_is_honestly_blocked(db, owner):
    project = new_project(db)
    job = prep.request_preparation(db, owner, project, submission())
    db.commit()
    assert prep.process_next_job(db, owner_identity=owner.identity) == job.id
    view = prep.get_job(db, owner, project, job.id)
    assert view.state == "blocked" and not view.evidence_verified
    assert prep.process_next_job(db, owner_identity=owner.identity) is None


def test_host_evidence_is_signed_current_and_not_public(db, owner):
    project = new_project(db)
    job = prep.request_preparation(db, owner, project, submission())
    db.commit()
    observed = []

    def builder(profile, job_id):
        with session() as readback:
            state = readback.execute(text("SELECT state FROM profile_preparations WHERE id=:id"),
                                     {"id": job_id}).scalar_one()
            observed.append(state)
        return proof(profile, job_id)

    prep.configure_builder(builder, expected_image_digest="sha256:" + "a" * 64, evidence_key=b"k" * 32)
    prep.process_next_job(db, owner_identity=owner.identity)
    view = prep.get_job(db, owner, project, job.id)
    assert observed == ["building"]
    assert view.state == "ready" and view.evidence_verified
    assert "evidence" not in view.model_dump() and "signature" not in view.model_dump()
    prep.configure_builder(None, expected_image_digest="sha256:" + "a" * 64, evidence_key=b"k" * 32)
    assert prep.get_job(db, owner, project, job.id).state == "ready"
    prep.configure_builder(None, evidence_key=b"k" * 32)
    assert prep.get_job(db, owner, project, job.id).state == "blocked"
    prep.configure_builder(None, expected_image_digest="sha256:" + "a" * 64, evidence_key=b"k" * 32)
    prep.configure_evidence_key(b"x" * 32)
    assert prep.get_job(db, owner, project, job.id).state == "blocked"
    prep.configure_evidence_key(b"k" * 32)
    prep.validate_binding_for_run(db, project, binding(image_digest="sha256:" + "a" * 64), owner=owner)
    with pytest.raises(DomainError):
        prep.validate_binding_for_run(db, project, binding(image_digest="sha256:" + "f" * 64), owner=owner)
    db.execute(text("UPDATE profile_preparations SET evidence=jsonb_set(evidence,'{proof,image_digest}',to_jsonb(CAST(:image AS text))) WHERE id=:id"),
               {"id": job.id, "image": "sha256:" + "f" * 64})
    db.commit()
    assert prep.get_job(db, owner, project, job.id).state == "blocked"


def test_unprepared_binding_can_be_drafted_but_not_executed(db, owner):
    project = new_project(db)
    prep.validate_binding_for_run(db, project, binding(), owner=owner, require_ready=False)
    with pytest.raises(DomainError):
        prep.validate_binding_for_run(db, project, binding(), owner=owner)
    with pytest.raises(DomainError):
        prep.validate_binding_for_run(db, project, binding(capability_ids=["paper-lookup"]),
                                      owner=owner, require_ready=False)


def test_readiness_rechecks_actual_masked_connection_after_revocation(db, owner, monkeypatch):
    from cryptography.fernet import Fernet

    project = new_project(db)
    session_id, provider = create_session(db, project, "Readiness fixture"), uuid4()
    monkeypatch.setattr(settings, "provider_destinations", lambda: {str(provider): "https://research.example"})
    cipher = Fernet(Fernet.generate_key())
    monkeypatch.setattr(prep.secretstore, "_fernet", lambda: cipher)
    prep.secretstore.create_connection(db, owner, provider, "Fixture connection", "fixture-model", "synthetic-secret")
    run = domain.submit_run(db, owner, project, session_id, str(uuid4()), "Measure resources", [], provider, "fixture-model")
    readiness = prep.get_readiness(db, owner, run.run_id)
    assert readiness.state == "ready" and readiness.plan_digest == run.plan_digest
    setup = prep.get_setup(db, owner, project)
    assert setup.connections[0].has_secret and "synthetic-secret" not in setup.model_dump_json()
    prep.secretstore.revoke_connection(db, owner, provider)
    assert prep.get_readiness(db, owner, run.run_id).state == "missing"


def test_verification_configuration_never_enables_a_builder():
    prep.configure_evidence_key(b"k" * 32)
    assert prep._builder is None
    with pytest.raises(ValueError):
        prep.configure_builder(lambda *_: {}, evidence_key=b"short")


def test_trusted_progress_cannot_skip_evidence_or_move_backwards(db, owner):
    project = new_project(db)
    job = prep.request_preparation(db, owner, project, submission())
    db.commit()

    def builder(profile, job_id):
        prep.record_stage(db, job_id, "image")
        prep.record_stage(db, job_id, "security")
        assert prep.get_job(db, owner, project, job_id).state == "checking"
        with pytest.raises(DomainError):
            prep.record_stage(db, job_id, "image")
        with pytest.raises(ValueError):
            prep.record_stage(db, job_id, "complete")
        return proof(profile, job_id)

    prep.configure_builder(builder, expected_image_digest="sha256:" + "a" * 64, evidence_key=b"k" * 32)
    prep.process_next_job(db, owner_identity=owner.identity)
    assert prep.get_job(db, owner, project, job.id).state == "ready"


def test_immutable_prebuilt_image_can_be_rechecked_without_rebuilding(db, owner):
    project = new_project(db)
    job = prep.request_preparation(db, owner, project, submission())
    db.commit()

    def verifier(profile, job_id):
        result = proof(profile, job_id)
        result["built_at"] = (datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
        return result

    prep.configure_builder(verifier, expected_image_digest="sha256:" + "a" * 64, evidence_key=b"k" * 32)
    prep.process_next_job(db, owner_identity=owner.identity)
    assert prep.get_job(db, owner, project, job.id).state == "ready"


def test_stale_ready_image_is_not_reused_but_exact_replay_is_idempotent(db, owner):
    project = new_project(db)
    key = b"k" * 32
    image_a = "sha256:" + "a" * 64
    image_b = "sha256:" + "b" * 64
    first_body = submission()
    first = prep.request_preparation(db, owner, project, first_body)
    db.commit()
    prep.configure_builder(
        lambda profile, job_id: {**proof(profile, job_id), "image_digest": image_a},
        expected_image_digest=image_a,
        evidence_key=key,
    )
    prep.process_next_job(db, owner_identity=owner.identity)
    old_evidence = db.execute(
        text("SELECT evidence FROM profile_preparations WHERE id=:id"), {"id": first.id}
    ).scalar_one()
    assert prep.get_job(db, owner, project, first.id).state == "ready"

    builder_calls = []
    prep.configure_builder(
        lambda profile, job_id: builder_calls.append(job_id)
        or {**proof(profile, job_id), "image_digest": image_b},
        expected_image_digest=image_b,
        evidence_key=key,
    )
    replacement_body = submission()
    replacement = prep.request_preparation(db, owner, project, replacement_body)
    assert replacement.id != first.id
    assert replacement.state == "queued"
    assert prep.get_job(db, owner, project, first.id).state == "blocked"
    assert prep.get_setup(db, owner, project).profiles[0].state == "preparing"
    assert builder_calls == []

    replay = prep.request_preparation(db, owner, project, first_body)
    assert replay.id == first.id and replay.state == "blocked"
    assert builder_calls == []
    assert db.execute(
        text("SELECT evidence FROM profile_preparations WHERE id=:id"), {"id": first.id}
    ).scalar_one() == old_evidence

    assert prep.process_next_job(db, owner_identity=owner.identity) == replacement.id
    assert prep.get_job(db, owner, project, replacement.id).state == "ready"
    reused = prep.request_preparation(db, owner, project, submission())
    assert reused.id == replacement.id and reused.state == "ready"
    assert builder_calls == [replacement.id]

    with pytest.raises(DomainError):
        prep.validate_binding_for_run(
            db, project, binding(image_digest=image_a), owner=owner
        )


def test_wrong_image_builder_output_never_has_a_ready_view_or_reuse(db, owner):
    project = new_project(db)
    expected = "sha256:" + "a" * 64
    wrong = "sha256:" + "b" * 64
    job = prep.request_preparation(db, owner, project, submission())
    db.commit()
    calls = []
    prep.configure_builder(
        lambda profile, job_id: calls.append(job_id)
        or {**proof(profile, job_id), "image_digest": wrong},
        expected_image_digest=expected,
        evidence_key=b"k" * 32,
    )

    assert prep.process_next_job(db, owner_identity=owner.identity) == job.id
    assert prep.get_job(db, owner, project, job.id).state == "blocked"
    assert prep.get_setup(db, owner, project).profiles[0].state == "blocked"
    replacement = prep.request_preparation(db, owner, project, submission())
    assert replacement.id != job.id and replacement.state == "queued"
    assert calls == [job.id]


def test_malformed_worker_image_pin_does_not_change_trusted_builder_configuration():
    callback = lambda *_: {}
    image = "sha256:" + "a" * 64
    prep.configure_builder(callback, expected_image_digest=image, evidence_key=b"k" * 32)
    with pytest.raises(ValueError):
        prep.configure_builder(lambda *_: {}, expected_image_digest="sha256:bad", evidence_key=b"x" * 32)
    assert prep._builder is callback
    assert prep._expected_image_digest == image
    assert prep._evidence_key == b"k" * 32

    prep.configure_builder(None)
    assert prep._builder is None
    assert prep._expected_image_digest is None


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(python_version="3.13"),
    lambda p: p.update(architecture="linux/amd64"),
    lambda p: p.update(lock_sha256={}),
    lambda p: p.update(compatibility_checks=[]),
    lambda p: p.update(license_approved=False),
    lambda p: p["scan"].update(high=1),
    lambda p: p["scan"].update(os_packages=0),
    lambda p: p["scan"].update(database_next_update=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()),
    lambda p: p["containment"].update(network="bridge"),
    lambda p: p["containment"].update(unreviewed_code_disabled=False),
])
def test_failed_or_unproven_build_cannot_become_ready(db, owner, mutate):
    project = new_project(db)
    job = prep.request_preparation(db, owner, project, submission())
    db.commit()

    def builder(profile, job_id):
        result = proof(profile, job_id)
        mutate(result)
        return result

    prep.configure_builder(builder, expected_image_digest="sha256:" + "a" * 64, evidence_key=b"k" * 32)
    prep.process_next_job(db, owner_identity=owner.identity)
    view = prep.get_job(db, owner, project, job.id)
    assert view.state == "failed" and not view.evidence_verified


def test_unknown_launch_is_never_replayed(db, owner):
    project = new_project(db)
    job = prep.request_preparation(db, owner, project, submission())
    db.commit()
    calls = []

    def builder(*_):
        calls.append(1)
        raise prep.BuildOutcomeUnknown()

    prep.configure_builder(builder, expected_image_digest="sha256:" + "a" * 64, evidence_key=b"k" * 32)
    prep.process_next_job(db, owner_identity=owner.identity)
    assert prep.get_job(db, owner, project, job.id).state == "unknown"
    assert prep.process_next_job(db, owner_identity=owner.identity) is None and calls == [1]


def test_interrupted_started_job_requires_owner_decision(db, owner):
    project = new_project(db)
    job = prep.request_preparation(db, owner, project, submission())
    db.execute(text("UPDATE profile_preparations SET state='building',stage='image' WHERE id=:id"), {"id": job.id})
    db.commit()
    calls = []
    prep.configure_builder(lambda *_: calls.append(1), evidence_key=b"k" * 32)
    assert prep.process_next_job(db, owner_identity=owner.identity) is None
    assert calls == [] and prep.get_job(db, owner, project, job.id).state != "ready"
