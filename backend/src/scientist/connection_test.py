"""Bounded, authenticated connection checks; never sends a model request."""
from __future__ import annotations

import http.client
import json
import queue
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from scientist import broker, provider_catalog, secrets, settings
from scientist.auth import DomainError, Principal

_MAX_TOTAL_SECONDS = 10.0
_MAX_RESPONSE_BYTES = 64 * 1024


def test_connection(db: Session, owner: Principal, connection_id: UUID) -> dict[str, str]:
    if owner.kind != "owner":
        raise DomainError("forbidden", 403)

    configured_origin = settings.provider_destinations().get(str(connection_id))
    row = db.execute(
        text(
            "SELECT provider, encrypted_value FROM credentials "
            "WHERE id = :id AND model IS NOT NULL"
        ),
        {"id": connection_id},
    ).one_or_none()
    if row is None:
        raise DomainError("not_found", 404)

    # Compare configured authority before decrypting. An ID whose configured
    # destination changed can never cause a request to its stored old origin.
    if configured_origin is None or row.provider != configured_origin:
        return _view(connection_id, "unavailable", "unverified")
    probe = provider_catalog.connection_test_probe(configured_origin)
    if probe is None:
        return _view(connection_id, "unsupported", "unverified")

    try:
        credential = secrets.read_secret(db, connection_id)
    except DomainError as exc:
        if exc.code == "not_found":
            raise
        return _view(connection_id, "unavailable", "unverified")
    if not credential or len(credential) > 16_384 or any(
        ord(char) < 32 or ord(char) == 127 for char in credential
    ):
        return _view(connection_id, "unavailable", "unverified")

    status = _exchange(configured_origin, probe, credential)
    if status == 200:
        return _view(connection_id, "reachable", "accepted")
    if status == 401:
        return _view(connection_id, "credentials_rejected", "rejected")
    if status == 403:
        return _view(connection_id, "denied", "unverified")
    if status == 429:
        return _view(connection_id, "rate_limited", "unverified")
    return _view(connection_id, "unavailable", "unverified")


def _view(connection_id: UUID, status: str, credential_status: str) -> dict[str, str]:
    return {
        "connection_id": str(connection_id),
        "status": status,
        "credential_status": credential_status,
        "model_status": "not_tested",
    }


def _exchange(origin: str, probe: tuple[str, str], credential: str) -> int | None:
    path, response_kind = probe
    deadline = time.monotonic() + _MAX_TOTAL_SECONDS
    try:
        host, port, _, ip = _resolve_origin(origin, deadline)
    except Exception:
        return None

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    connection = broker._PinnedHTTPSConnection(host, ip, port, remaining)
    connection._deadline = deadline
    watchdog = threading.Timer(max(0.0, remaining), broker._abort_connection, args=(connection,))
    watchdog.daemon = True
    watchdog.start()
    try:
        if time.monotonic() >= deadline:
            return None
        connection.request(
            "GET",
            path,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {credential}",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        status = response.status
        if status != 200:
            # Redirects are never followed; they are not proof of connectivity.
            if 300 <= status < 400:
                return None
            return status
        if response.getheader("Location") or response.getheader("Content-Encoding", "identity").lower() not in {"", "identity"}:
            return None
        if response.length is not None and response.length > _MAX_RESPONSE_BYTES:
            return None
        body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES or time.monotonic() > deadline:
            return None
        payload = _strict_json(body)
        if not _valid_response(response_kind, payload):
            return None
        return status
    except Exception:
        return None
    finally:
        watchdog.cancel()
        broker._abort_connection(connection)


def _resolve_origin(origin: str, deadline: float) -> tuple[str, int, str, str]:
    """Run broker URL/IP checks with DNS bounded by the same total deadline."""
    host = urlsplit(origin).hostname
    if host is None or time.monotonic() >= deadline:
        raise TimeoutError("connection test deadline")
    result: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def lookup() -> None:
        try:
            result.put((True, broker._resolver(host, 443)))
        except Exception as exc:
            result.put((False, exc))

    thread = threading.Thread(target=lookup, name="connection-test-dns", daemon=True)
    thread.start()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("connection test deadline")
    try:
        ok, addresses = result.get(timeout=remaining)
    except queue.Empty as exc:
        raise TimeoutError("connection test DNS deadline") from exc
    if not ok:
        raise OSError("connection test DNS failed")
    value = broker._validate_url(
        origin + "/", [origin], allow_lan=False, resolver=lambda *_: addresses  # type: ignore[arg-type]
    )
    host, port, _, ip = value  # type: ignore[misc]
    return host, port, "/", ip


def _strict_json(body: bytes) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_: str) -> None:
        raise ValueError("invalid JSON number")

    return json.loads(body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant)


def _valid_response(kind: str, payload: object) -> bool:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), (dict, list)):
        return False
    if kind == "openrouter_key":
        return isinstance(payload["data"], dict)
    if kind == "openai_models":
        return isinstance(payload["data"], list) and all(isinstance(item, dict) for item in payload["data"])
    return False
