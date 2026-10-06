from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from scientist import host, owner_settings, settings
from scientist import profile_preparation, scientific_authority
from scientist.dispatch_runtime import DispatchIdentity, DispatchTemplate, _parse_template, parse_dispatch_config
from scientist.private_dispatch_entrypoint import create_dispatch_app
from scientist.runtime_contracts import RUNTIME_COMMIT, RuntimeContextV1


PEER_ID = "abcdefab-cdef-4abc-8def-abcdefabcdef"
PEER_ORIGIN = "https://peer.example"
PROVIDER_ID = "22222222-2222-4222-8222-222222222222"
PROVIDER_ORIGIN = "https://research.example"
WORKER_IMAGE = "registry.local/worker@sha256:" + "a" * 64
DISPATCH_IMAGE = "registry.local/dispatch@sha256:" + "b" * 64


@pytest.fixture(autouse=True)
def restore_preparation_globals(monkeypatch):
    for module, names in ((profile_preparation, ("_builder", "_evidence_key")),
                          (scientific_authority, ("_bundle_root",))):
        for name in names:
            monkeypatch.setattr(module, name, getattr(module, name))


def _host_values(tmp_path: Path) -> dict[str, object]:
    secrets_dir = tmp_path / "secrets"
    state_dir = tmp_path / "state"
    secrets_dir.mkdir(mode=0o700)
    state_dir.mkdir(mode=0o700)
    for name in ("database_url", "broker_capability_key", "master_key", "s3_access_key", "s3_secret_key"):
        value = b"synthetic-config-capability-key-32-bytes" if name == "broker_capability_key" else b"synthetic-config-secret"
        (secrets_dir / name).write_bytes(value + b"\n")
    return {
        "schema_version": 1,
        "database_url": "postgresql+psycopg:///scientist?host=/tmp&port=54329",
        "listen_port": 18080,
        "expected_engine_id": "engine-1",
        "worker_image": WORKER_IMAGE,
        "dispatch_image": DISPATCH_IMAGE,
        "service_network": "scientist-platform-services",
        "egress_network": None,
        "skills_digest": "c" * 64,
        "environment_digest": "d" * 64,
        "s3_endpoint": "http://127.0.0.1:9000",
        "bucket": "scientist",
        "secrets_dir": secrets_dir,
        "state_dir": state_dir,
        "max_active": 3,
        "poll_seconds": 0.2,
        "provider_destinations": {PROVIDER_ID: PROVIDER_ORIGIN},
    }


def _identity(peer_destinations: dict[str, str]) -> DispatchIdentity:
    return DispatchIdentity(
        schema_version=1,
        run_id=uuid4(),
        generation=1,
        executor_id=uuid4(),
        process_incarnation=uuid4(),
        engine_id="engine-1",
        runtime_commit=RUNTIME_COMMIT,
        image_digest="sha256:" + "a" * 64,
        skills_digest="c" * 64,
        environment_digest="d" * 64,
        provider_destinations={},
        peer_destinations=peer_destinations,
        secret_files={
            name: name
            for name in ("database_url", "broker_capability_key", "master_key", "s3_access_key", "s3_secret_key")
        },
    )


def test_shared_peer_parser_is_canonical_bounded_and_reused_by_owner_settings(monkeypatch):
    expected = {PEER_ID: PEER_ORIGIN}
    raw = json.dumps(expected)
    monkeypatch.setenv("SCIENTIST_PEER_DESTINATIONS", raw)
    assert settings.parse_peer_destinations(raw) == expected
    assert owner_settings.configured_peers() == expected


@pytest.mark.parametrize(
    "raw",
    [
        '{"11111111-1111-4111-8111-111111111111":"https://one.example",'
        '"11111111-1111-4111-8111-111111111111":"https://two.example"}',
        json.dumps({PEER_ID.upper(): PEER_ORIGIN}),
        json.dumps({PEER_ID: "https://User@peer.example"}),
        json.dumps({PEER_ID: "https://peer.example:443"}),
        json.dumps({PEER_ID: "https://peer.example/path"}),
        json.dumps({PEER_ID: "https://peer.example?query=1"}),
        json.dumps({PEER_ID: "https://peer.example#fragment"}),
        json.dumps({PEER_ID: "http://peer.example"}),
        json.dumps({PEER_ID: "https://localhost"}),
        "{" + ",".join(json.dumps(str(UUID(int=index + 1))) + ":" + json.dumps(PEER_ORIGIN) for index in range(101)) + "}",
        " " * (16 * 1024 + 1),
    ],
)
def test_shared_peer_parser_fails_closed_on_invalid_or_oversized_maps(raw: str):
    assert settings.parse_peer_destinations(raw) == {}


def test_host_config_defaults_to_empty_and_requires_environment_map_equality(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "provider_destinations", lambda: {PROVIDER_ID: PROVIDER_ORIGIN})
    monkeypatch.setenv("SCIENTIST_PEER_DESTINATIONS", "")
    values = _host_values(tmp_path)
    config = host.HostConfig.model_validate(values)
    assert config.peer_destinations == {}

    expected = {PEER_ID: PEER_ORIGIN}
    monkeypatch.setenv("SCIENTIST_PEER_DESTINATIONS", json.dumps(expected))
    accepted = host.HostConfig.model_validate({**values, "peer_destinations": expected})
    assert accepted.peer_destinations == expected
    with pytest.raises(ValidationError):
        host.HostConfig.model_validate({**values, "peer_destinations": {PEER_ID: "https://other.example"}})


def test_host_compose_writes_only_trusted_peer_destinations_to_dispatch_template(tmp_path, monkeypatch):
    expected = {PEER_ID: PEER_ORIGIN}
    monkeypatch.setattr(settings, "provider_destinations", lambda: {PROVIDER_ID: PROVIDER_ORIGIN})
    monkeypatch.setenv("SCIENTIST_PEER_DESTINATIONS", json.dumps(expected))
    config = host.HostConfig.model_validate({**_host_values(tmp_path), "peer_destinations": expected})
    monkeypatch.setattr(host.supervisor, "configure", lambda **_kwargs: None)
    monkeypatch.setattr(host.objects, "configure", lambda *_args, **_kwargs: None)
    configured: dict[str, object] = {}
    monkeypatch.setattr(host.broker, "configure", lambda **kwargs: configured.update(kwargs))

    class FakeEngine:
        def engine_id(self):
            return "engine-1"

    class FakeS3:
        def head_bucket(self, *, Bucket):
            assert Bucket == "scientist"

    host.compose(config, engine=FakeEngine(), s3=FakeS3())
    template_bytes = (config.state_dir / "dispatch-template.json").read_bytes()
    template = _parse_template(template_bytes)
    assert dict(template.peer_destinations) == {UUID(PEER_ID): PEER_ORIGIN}
    assert configured["peer_destinations"] == expected
    assert PEER_ORIGIN in template_bytes.decode("utf-8")


def test_dispatch_template_identity_json_and_private_configuration_preserve_peer_map(monkeypatch):
    expected = {UUID(PEER_ID): PEER_ORIGIN}
    template = DispatchTemplate(
        schema_version=1,
        runtime_commit=RUNTIME_COMMIT,
        image_digest="sha256:" + "a" * 64,
        skills_digest="c" * 64,
        environment_digest="d" * 64,
        provider_destinations={UUID(PROVIDER_ID): PROVIDER_ORIGIN},
        peer_destinations=expected,
        secret_files={
            name: name
            for name in ("database_url", "broker_capability_key", "master_key", "s3_access_key", "s3_secret_key")
        },
    )
    with pytest.raises(TypeError):
        template.peer_destinations[UUID(PEER_ID)] = "https://changed.example"
    template_bytes = template.model_dump_json().encode()
    parsed_template = _parse_template(template_bytes)
    identity = _identity({str(key): value for key, value in parsed_template.peer_destinations.items()})
    with pytest.raises(TypeError):
        identity.peer_destinations[UUID(PEER_ID)] = "https://changed.example"
    identity_bytes = identity.model_dump_json().encode()
    assert json.loads(identity_bytes)["peer_destinations"] == {PEER_ID: PEER_ORIGIN}
    parsed_identity = parse_dispatch_config(identity_bytes)
    assert dict(parsed_identity.peer_destinations) == expected
    with pytest.raises(TypeError):
        parsed_template.peer_destinations[UUID(PEER_ID)] = "https://changed.example"
    with pytest.raises(TypeError):
        parsed_identity.peer_destinations[UUID(PEER_ID)] = "https://changed.example"
    assert "peer_destinations" not in RuntimeContextV1.model_fields

    import scientist.private_dispatch_entrypoint as private_entrypoint

    configured: dict[str, object] = {}
    monkeypatch.setattr(private_entrypoint.broker, "configure", lambda **kwargs: configured.update(kwargs))
    monkeypatch.setattr(private_entrypoint.checkpoints, "configure_trusted_pins", lambda **_kwargs: None)
    monkeypatch.setattr(private_entrypoint, "_read_secret", lambda _name: b"x" * 32)
    monkeypatch.setattr(private_entrypoint.broker, "_key", lambda: b"x" * 32)
    create_dispatch_app(parsed_identity)
    assert configured["peer_destinations"] == {PEER_ID: PEER_ORIGIN}
