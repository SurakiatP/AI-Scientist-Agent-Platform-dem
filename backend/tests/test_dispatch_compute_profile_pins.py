from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from scientist import host, private_dispatch_entrypoint, profile_preparation
from scientist.dispatch_runtime import DispatchIdentity, DispatchTemplate


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    """This dispatch-composition unit test deliberately needs no database."""
    yield


def test_host_compute_image_pin_reaches_private_dispatch_profile_verifier(monkeypatch):
    worker_digest = "sha256:" + "a" * 64
    compute_digest = "sha256:" + "d" * 64
    payload = host._dispatch_template_payload(
        SimpleNamespace(
            skills_digest="b" * 64,
            environment_digest="c" * 64,
            bucket="scientist-test",
            provider_destinations={},
            peer_destinations={},
        ),
        worker_digest,
        compute_digest,
    )
    template = DispatchTemplate.model_validate(payload)
    identity = DispatchIdentity.model_validate(
        {
            **template.model_dump(mode="python"),
            "run_id": uuid4(),
            "generation": 1,
            "executor_id": uuid4(),
            "process_incarnation": uuid4(),
            "engine_id": "engine-test",
        }
    )

    configured = {}
    monkeypatch.setattr(
        private_dispatch_entrypoint.checkpoints,
        "configure_trusted_pins",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(private_dispatch_entrypoint.broker, "configure", lambda **kwargs: None)
    monkeypatch.setattr(private_dispatch_entrypoint, "_read_secret", lambda name: b"s" * 32)
    monkeypatch.setattr(private_dispatch_entrypoint.broker, "_key", lambda: b"k" * 32)
    monkeypatch.setattr(
        profile_preparation,
        "configure_builder",
        lambda callback, **kwargs: configured.update(callback=callback, **kwargs),
    )

    private_dispatch_entrypoint._configure_dispatch_runtime(identity)

    assert configured["callback"] is None
    assert configured["expected_image_digests"] == {
        "prof.worker-base@py3.14.7": worker_digest,
        "prof.csv-stdlib@py3.14.7": compute_digest,
    }
    assert configured["evidence_key"] == b"k" * 32
