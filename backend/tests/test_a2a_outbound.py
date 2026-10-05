from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from a2a.types import a2a_pb2 as p

from scientist.contracts import PeerReleaseSpec, canonical_peer_parameters_bytes
from scientist.a2a_outbound import (
    AccountingEvidence,
    PeerOutboundCallbacks,
    PeerOutboundError,
    reconcile_peer_task,
    submit_peer_release,
)


def _release(*, allow_get_task: bool = True) -> PeerReleaseSpec:
    parameters = {
        "message": {
            "messageId": "stable-peer-message",
            "role": "ROLE_USER",
            "parts": [{"text": "Review this selected claim."}],
        }
    }
    return PeerReleaseSpec(
        release_id=uuid4(),
        peer_id=uuid4(),
        endpoint_fingerprint=sha256(b"https://peer.example").hexdigest(),
        purpose="Review the selected claim.",
        input_snapshot_digest="b" * 64,
        data_refs=[],
        approved_parameters=parameters,
        parameters_sha256=sha256(canonical_peer_parameters_bytes(parameters)).hexdigest(),
        message_id="stable-peer-message",
        method="SendMessage",
        allow_get_task=allow_get_task,
        request_bytes_limit=4096,
        timeout_ms=1000,
        reserved_tokens=100,
        reconciliation_limit=2,
    )


def _task_body(task_id: str = "remote-task", context_id: str = "remote-context") -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "sdk-generated-request-id",
            "result": {
                "task": {
                    "id": task_id,
                    "contextId": context_id,
                    "status": {"state": "TASK_STATE_WORKING"},
                }
            },
        }
    ).encode()


def _callbacks(events: list, *, prepare_result: bool = True) -> PeerOutboundCallbacks:
    def prepare(run_id, operation_id, release_id):
        events.append(("prepare", run_id, operation_id, release_id))
        return prepare_result

    def record(run_id, operation_id, task_id, context_id):
        events.append(("identity", run_id, operation_id, task_id, context_id))

    def unknown(run_id, operation_id, reason):
        events.append(("unknown", run_id, operation_id, reason))

    def persist(run_id, operation_id, task, evidence):
        events.append(("persist", run_id, operation_id, task.id, evidence))

    return PeerOutboundCallbacks(prepare, record, unknown, persist)


def test_submission_uses_sdk_once_and_records_identity_before_result_storage():
    events: list = []
    run_id, operation_id = uuid4(), "peer-operation-1"
    request_count = 0

    async def exchange(url, method, headers, body, timeout_ms, max_response_bytes):
        nonlocal request_count
        request_count += 1
        events.append(("exchange", url, method, headers, json.loads(body)))
        return httpx.Response(200, headers={"content-type": "application/json"}, content=_task_body())

    async def run():
        return await submit_peer_release(
            _release(), run_id, operation_id,
            endpoint_url="https://peer.example/a2a",
            expected_authority="peer.example",
            callbacks=_callbacks(events),
            exchange=exchange,
        )

    result = asyncio.run(run())

    assert request_count == 1
    assert result.remote_task_id == "remote-task"
    assert result.remote_context_id == "remote-context"
    assert result.accounting.status == "unsupported"
    assert result.accounting.usage_tokens is None
    assert [event[0] for event in events] == ["prepare", "exchange", "identity", "persist"]
    sent = events[1]
    assert sent[1:3] == ("https://peer.example/a2a", "POST")
    assert sent[3]["A2A-Version"] == "1.0"
    assert "cookie" not in {key.lower() for key in sent[3]}
    assert sent[4]["method"] == "SendMessage"
    assert sent[4]["params"]["message"]["messageId"] == "stable-peer-message"


def test_lost_acceptance_marks_unknown_and_never_retries_or_resends():
    events: list = []
    calls = 0

    async def exchange(*_args):
        nonlocal calls
        calls += 1
        raise TimeoutError("peer did not return a response")

    async def run():
        await submit_peer_release(
            _release(), uuid4(), "peer-operation-2",
            endpoint_url="https://peer.example/a2a",
            expected_authority="peer.example",
            callbacks=_callbacks(events),
            exchange=exchange,
        )

    with pytest.raises(PeerOutboundError, match="outcome is unknown"):
        asyncio.run(run())

    assert calls == 1
    assert [event[0] for event in events] == ["prepare", "unknown"]


def test_existing_receipt_cannot_be_used_to_resend_send_message():
    events: list = []

    async def exchange(*_args):
        pytest.fail("existing receipt must stop before network I/O")

    async def run():
        await submit_peer_release(
            _release(), uuid4(), "peer-operation-3",
            endpoint_url="https://peer.example/a2a",
            expected_authority="peer.example",
            callbacks=_callbacks(events, prepare_result=False),
            exchange=exchange,
        )

    with pytest.raises(PeerOutboundError, match="already prepared"):
        asyncio.run(run())

    assert [event[0] for event in events] == ["prepare"]


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "http://peer.example/a2a",
        "https://peer.example/other",
        "https://peer.example/a2a?redirect=https://evil.example",
        "https://peer.example.evil/a2a",
    ],
)
def test_endpoint_must_match_https_origin_fingerprint_and_exact_rpc_path(endpoint_url):
    events: list = []

    async def exchange(*_args):
        pytest.fail("invalid endpoint must be rejected before network I/O")

    async def run():
        await submit_peer_release(
            _release(), uuid4(), "peer-operation-invalid-endpoint",
            endpoint_url=endpoint_url,
            expected_authority="peer.example",
            callbacks=_callbacks(events),
            exchange=exchange,
        )

    with pytest.raises(PeerOutboundError, match="endpoint"):
        asyncio.run(run())

    assert events == []


def test_peer_result_with_no_remote_identity_is_unknown():
    events: list = []

    async def exchange(*_args):
        body = json.dumps({"jsonrpc": "2.0", "id": "x", "result": {"message": {"messageId": "m", "role": "ROLE_AGENT", "parts": [{"text": "accepted"}]}}}).encode()
        return httpx.Response(200, headers={"content-type": "application/json"}, content=body)

    async def run():
        await submit_peer_release(
            _release(), uuid4(), "peer-operation-4",
            endpoint_url="https://peer.example/a2a",
            expected_authority="peer.example",
            callbacks=_callbacks(events),
            exchange=exchange,
        )

    with pytest.raises(PeerOutboundError, match="no durable remote task identity"):
        asyncio.run(run())

    assert [event[0] for event in events] == ["prepare", "unknown"]


@pytest.mark.parametrize(
    "task_id,context_id",
    [
        (" remote-task", "remote-context"),
        ("", "remote-context"),
        ("remote-task", "remote-context "),
        ("remote-task", " "),
        ("remote-task", ""),
    ],
)
def test_submission_rejects_padded_or_blank_remote_ids_before_identity_persistence(task_id, context_id):
    events: list = []

    async def exchange(*_args):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=_task_body(task_id, context_id),
        )

    async def run():
        await submit_peer_release(
            _release(), uuid4(), "peer-operation-padded-response",
            endpoint_url="https://peer.example/a2a",
            expected_authority="peer.example",
            callbacks=_callbacks(events),
            exchange=exchange,
        )

    with pytest.raises(PeerOutboundError, match="identity"):
        asyncio.run(run())

    assert [event[0] for event in events] == ["prepare", "unknown"]


@pytest.mark.parametrize(
    "task_id,context_id",
    [(" remote-task", "remote-context"), ("remote-task", "remote-context ")],
)
def test_get_task_rejects_padded_response_ids_before_identity_persistence(task_id, context_id):
    events: list = []

    async def exchange(*_args):
        body = {
            "jsonrpc": "2.0",
            "id": "sdk-generated-request-id",
            "result": {
                "id": task_id,
                "contextId": context_id,
                "status": {"state": "TASK_STATE_WORKING"},
            },
        }
        return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(body).encode())

    async def run():
        await reconcile_peer_task(
            _release(), uuid4(), "peer-operation-padded-get-task",
            "remote-task", "remote-context", attempt=1,
            endpoint_url="https://peer.example/a2a",
            expected_authority="peer.example",
            callbacks=_callbacks(events),
            exchange=exchange,
        )

    with pytest.raises(PeerOutboundError, match="identity"):
        asyncio.run(run())

    assert events == []


@pytest.mark.parametrize(
    "task_id,context_id",
    [(" remote-task", "remote-context"), ("remote-task", " remote-context"), ("remote-task", "")],
)
def test_get_task_rejects_padded_or_blank_known_identity_before_network_io(task_id, context_id):
    events: list = []

    async def exchange(*_args):
        pytest.fail("padded known identity must not be sent")

    async def run():
        await reconcile_peer_task(
            _release(), uuid4(), "peer-operation-padded-known-id",
            task_id, context_id, attempt=1,
            endpoint_url="https://peer.example/a2a",
            expected_authority="peer.example",
            callbacks=_callbacks(events),
            exchange=exchange,
        )

    with pytest.raises(PeerOutboundError, match="identity"):
        asyncio.run(run())

    assert events == []


def test_result_storage_failure_keeps_known_identity_and_never_marks_unknown():
    events: list = []
    run_id, operation_id = uuid4(), "peer-operation-5"
    callbacks = _callbacks(events)

    def fail_persist(run_id, operation_id, task, evidence):
        events.append(("persist-failed", task.id))
        raise OSError("object store unavailable")

    callbacks = PeerOutboundCallbacks(
        callbacks.prepare,
        callbacks.record_remote_identity,
        callbacks.mark_unknown,
        fail_persist,
    )

    async def exchange(*_args):
        return httpx.Response(200, headers={"content-type": "application/json"}, content=_task_body())

    async def run():
        await submit_peer_release(
            _release(), run_id, operation_id,
            endpoint_url="https://peer.example/a2a",
            expected_authority="peer.example",
            callbacks=callbacks,
            exchange=exchange,
        )

    with pytest.raises(OSError, match="object store unavailable"):
        asyncio.run(run())

    assert [event[0] for event in events] == ["prepare", "identity", "persist-failed"]


def test_identity_persistence_failure_parks_receipt_as_unknown_before_result_storage():
    events: list = []
    run_id, operation_id = uuid4(), "peer-operation-identity-failed"
    callbacks = _callbacks(events)

    def fail_record(run_id, operation_id, task_id, context_id):
        events.append(("identity-failed", task_id, context_id))
        raise OSError("database unavailable")

    callbacks = PeerOutboundCallbacks(
        callbacks.prepare,
        fail_record,
        callbacks.mark_unknown,
        callbacks.persist_result,
    )

    async def exchange(*_args):
        return httpx.Response(200, headers={"content-type": "application/json"}, content=_task_body())

    async def run():
        await submit_peer_release(
            _release(), run_id, operation_id,
            endpoint_url="https://peer.example/a2a",
            expected_authority="peer.example",
            callbacks=callbacks,
            exchange=exchange,
        )

    with pytest.raises(PeerOutboundError, match="identity could not be persisted"):
        asyncio.run(run())

    assert [event[0] for event in events] == ["prepare", "identity-failed", "unknown"]


@pytest.mark.parametrize("approved,attempt", [(False, 1), (True, 3)])
def test_get_task_reconciliation_requires_approved_method_and_bounded_attempt(approved, attempt):
    events: list = []
    calls = 0

    async def exchange(*_args):
        nonlocal calls
        calls += 1
        pytest.fail("unapproved or exhausted reconciliation must stop before network I/O")

    async def run():
        await reconcile_peer_task(
            _release(allow_get_task=approved), uuid4(), "peer-operation-6",
            "remote-task", "remote-context", attempt=attempt,
            endpoint_url="https://peer.example/a2a",
            expected_authority="peer.example",
            callbacks=_callbacks(events), exchange=exchange,
        )

    with pytest.raises(PeerOutboundError, match="not approved or its limit is exhausted"):
        asyncio.run(run())
    assert calls == 0
    assert events == []


def test_approved_get_task_uses_only_the_known_remote_identity():
    events: list = []
    run_id, operation_id = uuid4(), "peer-operation-7"
    request_payloads: list = []

    async def exchange(url, method, headers, body, timeout_ms, max_response_bytes):
        request_payloads.append(json.loads(body))
        result = {
            "jsonrpc": "2.0",
            "id": "sdk-generated-request-id",
            "result": {
                "id": "remote-task",
                "contextId": "remote-context",
                "status": {"state": "TASK_STATE_COMPLETED"},
            },
        }
        return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(result).encode())

    async def run():
        return await reconcile_peer_task(
            _release(), run_id, operation_id, "remote-task", "remote-context", attempt=2,
            endpoint_url="https://peer.example/a2a",
            expected_authority="peer.example",
            callbacks=_callbacks(events), exchange=exchange,
        )

    result = asyncio.run(run())
    assert result.remote_task_id == "remote-task"
    assert request_payloads[0]["method"] == "GetTask"
    assert request_payloads[0]["params"] == {"id": "remote-task"}
    assert [event[0] for event in events] == ["identity", "persist"]
