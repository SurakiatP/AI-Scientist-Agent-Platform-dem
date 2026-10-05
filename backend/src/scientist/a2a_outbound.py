"""One-shot, owner-approved outbound A2A calls through an injected pinned exchange.

The A2A SDK owns JSON-RPC/protobuf encoding. The injected exchange owns DNS/IP/TLS
pinning and bounded socket I/O; this module constrains what the SDK may send to it.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Awaitable, Callable, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.types import a2a_pb2 as p
from google.protobuf.json_format import MessageToDict, ParseDict

from scientist.contracts import PeerReleaseSpec, canonical_peer_parameters_bytes


class PeerOutboundError(RuntimeError):
    """A peer call failed or must remain parked for reconciliation."""


@dataclass(frozen=True)
class AccountingEvidence:
    """Explicit peer usage evidence; A2A itself supplies no token accounting."""

    status: Literal["known", "unsupported", "unknown"]
    usage_tokens: int | None = None
    contract: str | None = None

    def __post_init__(self) -> None:
        if self.status == "known":
            if self.usage_tokens is None or self.usage_tokens < 0 or not self.contract:
                raise ValueError("known usage requires nonnegative tokens and a named contract")
        elif self.usage_tokens is not None:
            raise ValueError("unsupported or unknown usage cannot carry a token count")


@dataclass(frozen=True)
class PeerOutboundResult:
    remote_task_id: str
    remote_context_id: str | None
    response: p.Task
    accounting: AccountingEvidence


@dataclass(frozen=True)
class PeerOutboundCallbacks:
    """Persistence seams. prepare returns true only for a new first-send receipt."""

    prepare: Callable[[UUID, str, UUID], bool | Awaitable[bool]]
    record_remote_identity: Callable[[UUID, str, str | None, str | None], Any]
    mark_unknown: Callable[[UUID, str, str], Any]
    persist_result: Callable[[UUID, str, p.Task, AccountingEvidence], Any]


class PinnedHTTPSExchange(Protocol):
    async def __call__(
        self,
        url: str,
        method: str,
        headers: httpx.Headers,
        body: bytes,
        timeout_ms: int,
        max_response_bytes: int,
    ) -> httpx.Response: ...


def _check_endpoint(endpoint_url: str, expected_authority: str, endpoint_fingerprint: str) -> str:
    try:
        parsed = urlsplit(endpoint_url)
        authority = parsed.netloc
        # Accessing .port also rejects malformed ports.
        _ = parsed.port
    except ValueError as exc:
        raise PeerOutboundError("configured peer endpoint is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/a2a"
        or authority.lower() != expected_authority.lower()
        or sha256(f"{parsed.scheme}://{authority}".encode("utf-8")).hexdigest()
        != endpoint_fingerprint.lower()
    ):
        raise PeerOutboundError("configured peer endpoint authority is not approved")
    return authority


def _approved_request(release: PeerReleaseSpec) -> p.SendMessageRequest:
    try:
        canonical = canonical_peer_parameters_bytes(release.approved_parameters)
        if sha256(canonical).hexdigest() != release.parameters_sha256.lower():
            raise ValueError("parameter digest mismatch")
        request = ParseDict(release.approved_parameters, p.SendMessageRequest())
        if canonical_peer_parameters_bytes(MessageToDict(request)) != canonical:
            raise ValueError("SDK parameters changed during parsing")
    except Exception as exc:
        raise PeerOutboundError("approved A2A parameters are invalid") from exc
    if (
        release.method != "SendMessage"
        or not request.HasField("message")
        or request.message.message_id != release.message_id
        or request.message.role != p.ROLE_USER
        or request.tenant
        or request.message.task_id
        or not request.message.parts
        or request.configuration.HasField("task_push_notification_config")
    ):
        raise PeerOutboundError("approved A2A request is outside the SendMessage profile")
    for part in request.message.parts:
        if part.WhichOneof("content") != "text" or part.metadata or part.filename or part.media_type:
            raise PeerOutboundError("approved A2A request contains unsupported content")
    return request


class _ExchangeTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        endpoint_url: str,
        exchange: PinnedHTTPSExchange,
        timeout_ms: int,
        request_bytes_limit: int,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.exchange = exchange
        self.timeout_ms = timeout_ms
        self.request_bytes_limit = request_bytes_limit

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url != httpx.URL(self.endpoint_url) or request.method != "POST":
            raise PeerOutboundError("A2A SDK attempted an unapproved URL or method")
        request.headers["A2A-Version"] = "1.0"
        if "cookie" in request.headers or "proxy-authorization" in request.headers:
            raise PeerOutboundError("ambient peer credentials are forbidden")
        body = await request.aread()
        if len(body) > self.request_bytes_limit:
            raise PeerOutboundError("serialized A2A request exceeds the approved byte limit")
        try:
            response = await self.exchange(
                str(request.url),
                request.method,
                request.headers,
                body,
                self.timeout_ms,
                self.request_bytes_limit,
            )
        except Exception:
            raise
        if 300 <= response.status_code < 400:
            raise PeerOutboundError("peer redirects are forbidden")
        try:
            response_body = await response.aread()
        finally:
            await response.aclose()
        if len(response_body) > self.request_bytes_limit:
            raise PeerOutboundError("peer response exceeds the approved byte limit")
        # Do not let a peer seed a cookie jar for a later SDK request.
        headers = httpx.Headers(response.headers)
        headers.pop("set-cookie", None)
        return httpx.Response(
            response.status_code,
            headers=headers,
            content=response_body,
            request=request,
        )


async def _call(callback: Callable[..., Any], *args: Any) -> Any:
    value = callback(*args)
    return await value if inspect.isawaitable(value) else value


def _task_and_ids(response: p.SendMessageResponse | p.Task) -> tuple[p.Task, str, str | None]:
    if isinstance(response, p.SendMessageResponse):
        if not response.HasField("task"):
            raise PeerOutboundError("peer response has no durable remote task identity")
        task = response.task
    else:
        task = response
    task_id = task.id
    if not task_id or task_id != task_id.strip() or len(task_id) > 200:
        raise PeerOutboundError("peer response has no durable remote task identity (blank or padded)")
    context_id = task.context_id
    if not context_id or context_id != context_id.strip() or len(context_id) > 200:
        raise PeerOutboundError("peer response context identity is invalid")
    return task, task_id, context_id


async def _sdk_call(
    endpoint_url: str,
    exchange: PinnedHTTPSExchange,
    timeout_ms: int,
    request_bytes_limit: int,
    callback: Callable[[JsonRpcTransport], Awaitable[Any]],
) -> Any:
    transport = _ExchangeTransport(endpoint_url, exchange, timeout_ms, request_bytes_limit)
    async with httpx.AsyncClient(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(timeout_ms / 1000),
    ) as client:
        sdk = JsonRpcTransport(client, p.AgentCard(), endpoint_url)
        return await callback(sdk)


async def submit_peer_release(
    release: PeerReleaseSpec,
    run_id: UUID,
    operation_id: str,
    *,
    endpoint_url: str,
    expected_authority: str,
    callbacks: PeerOutboundCallbacks,
    exchange: PinnedHTTPSExchange,
    accounting: Callable[[p.Task], AccountingEvidence] | None = None,
) -> PeerOutboundResult:
    """Prepare once, submit once, save returned identity, then persist the result."""
    _check_endpoint(endpoint_url, expected_authority, release.endpoint_fingerprint)
    request = _approved_request(release)
    if len(canonical_peer_parameters_bytes(release.approved_parameters)) > release.request_bytes_limit:
        raise PeerOutboundError("approved A2A parameters exceed the request byte limit")
    if not await _call(callbacks.prepare, run_id, operation_id, release.release_id):
        raise PeerOutboundError("peer receipt is already prepared; SendMessage will not be resent")

    try:
        response = await _sdk_call(
            endpoint_url,
            exchange,
            release.timeout_ms,
            release.request_bytes_limit,
            lambda sdk: sdk.send_message(request),
        )
        task, remote_task_id, remote_context_id = _task_and_ids(response)
    except Exception as exc:
        await _call(callbacks.mark_unknown, run_id, operation_id, type(exc).__name__)
        if isinstance(exc, PeerOutboundError) and "identity" in str(exc):
            raise
        raise PeerOutboundError("peer submission outcome is unknown; reservation remains parked") from exc

    try:
        await _call(callbacks.record_remote_identity, run_id, operation_id, remote_task_id, remote_context_id)
    except Exception as exc:
        await _call(callbacks.mark_unknown, run_id, operation_id, "remote_identity_persistence_failed")
        raise PeerOutboundError(
            "accepted peer task identity could not be persisted; reservation remains parked"
        ) from exc
    evidence = accounting(task) if accounting is not None else AccountingEvidence("unsupported")
    if not isinstance(evidence, AccountingEvidence):
        raise PeerOutboundError("peer usage contract returned invalid accounting evidence")
    await _call(callbacks.persist_result, run_id, operation_id, task, evidence)
    return PeerOutboundResult(remote_task_id, remote_context_id, task, evidence)


async def reconcile_peer_task(
    release: PeerReleaseSpec,
    run_id: UUID,
    operation_id: str,
    remote_task_id: str,
    remote_context_id: str | None,
    *,
    attempt: int,
    endpoint_url: str,
    expected_authority: str,
    callbacks: PeerOutboundCallbacks,
    exchange: PinnedHTTPSExchange,
    accounting: Callable[[p.Task], AccountingEvidence] | None = None,
) -> PeerOutboundResult:
    """Read a known remote task once; attempt counts are bounded by the approved release."""
    _check_endpoint(endpoint_url, expected_authority, release.endpoint_fingerprint)
    if not release.allow_get_task or not 1 <= attempt <= release.reconciliation_limit:
        raise PeerOutboundError("GetTask reconciliation is not approved or its limit is exhausted")
    if not remote_task_id or remote_task_id != remote_task_id.strip() or len(remote_task_id) > 200:
        raise PeerOutboundError("remote task identity is invalid")
    params = p.GetTaskRequest(id=remote_task_id)
    if remote_context_id is not None:
        if (
            not remote_context_id
            or remote_context_id != remote_context_id.strip()
            or len(remote_context_id) > 200
        ):
            raise PeerOutboundError("remote context identity is invalid")
    response = await _sdk_call(
        endpoint_url,
        exchange,
        release.timeout_ms,
        release.request_bytes_limit,
        lambda sdk: sdk.get_task(params),
    )
    task, task_id, context_id = _task_and_ids(response)
    if task_id != remote_task_id or (remote_context_id and context_id != remote_context_id):
        raise PeerOutboundError("peer changed a previously recorded remote identity")
    await _call(callbacks.record_remote_identity, run_id, operation_id, task_id, context_id)
    evidence = accounting(task) if accounting is not None else AccountingEvidence("unsupported")
    if not isinstance(evidence, AccountingEvidence):
        raise PeerOutboundError("peer usage contract returned invalid accounting evidence")
    await _call(callbacks.persist_result, run_id, operation_id, task, evidence)
    return PeerOutboundResult(task_id, context_id, task, evidence)
