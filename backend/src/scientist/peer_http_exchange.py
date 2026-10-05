"""Bounded peer-only HTTPS exchange beneath durable dispatch authority."""
from __future__ import annotations

import time
from threading import Thread, Timer
from queue import Empty, Queue
from urllib.parse import urlsplit

import httpx

from scientist.auth import DomainError


def pinned_exchange(origin: str, credential: str, *, request_bytes_limit: int):
    from scientist import broker

    if (not isinstance(credential, str) or not credential or
            any(ord(c) < 32 or ord(c) == 127 for c in credential) or
            isinstance(request_bytes_limit, bool) or not isinstance(request_bytes_limit, int) or
            not 1 <= request_bytes_limit <= 1048576):
        raise DomainError("forbidden", 403)
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.netloc != (f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname) or
            parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise DomainError("forbidden", 403)
    endpoint = origin + "/a2a"

    async def exchange(url: str, method: str, headers: httpx.Headers, body: bytes,
                       timeout_ms: int, max_response_bytes: int) -> httpx.Response:
        if (url != endpoint or method != "POST" or not isinstance(body, bytes) or
                len(body) > request_bytes_limit or
                any(name in headers for name in ("cookie", "authorization", "proxy-authorization")) or
                headers.get("A2A-Version") != "1.0" or headers.get("Content-Type") != "application/json" or
                isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1 <= timeout_ms <= 30000 or
                isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or
                not 1 <= max_response_bytes <= request_bytes_limit):
            raise DomainError("forbidden", 403)
        deadline = time.monotonic() + timeout_ms / 1000
        resolver = broker._resolver
        def bounded_resolver(host, port):
            results = Queue(maxsize=1)
            def resolve():
                try:
                    results.put((True, resolver(host, port)))
                except Exception:
                    results.put((False, None))
            Thread(target=resolve, name="scientist-peer-dns", daemon=True).start()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DomainError("provider_unavailable", 502)
            try:
                success, addresses = results.get(timeout=remaining)
            except Empty as exc:
                raise DomainError("provider_unavailable", 502) from exc
            if not success or time.monotonic() >= deadline:
                raise DomainError("provider_unavailable", 502)
            return addresses
        host, port, path, ip = broker._validate_url(endpoint, [origin], allow_lan=True, resolver=bounded_resolver)
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise DomainError("provider_unavailable", 502)
        connection = broker._PinnedHTTPSConnection(host, ip, port, timeout)
        connection._deadline = deadline
        outbound_headers = {"Content-Type": "application/json", "A2A-Version": "1.0",
                            "Host": parsed.netloc, "Authorization": "Bearer " + credential}
        watchdog = Timer(timeout, broker._abort_connection, args=(connection,))
        watchdog.daemon = True
        watchdog.start()
        try:
            if time.monotonic() >= deadline:
                raise DomainError("provider_unavailable", 502)
            connection.request("POST", path, body=body, headers=outbound_headers)
            response = connection.getresponse()
            if not 200 <= response.status < 300 or response.getheader("Location") is not None:
                raise DomainError("provider_unavailable", 502)
            data = response.read(max_response_bytes + 1)
            if (len(data) > max_response_bytes or getattr(response, "length", None) or
                    time.monotonic() > deadline):
                raise DomainError("provider_unavailable", 502)
            return httpx.Response(response.status, content=data, headers={"Content-Type": "application/json"})
        finally:
            watchdog.cancel()
            connection.close()

    return exchange
