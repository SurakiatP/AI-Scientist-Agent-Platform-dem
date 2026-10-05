from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from scientist import broker
from scientist.auth import DomainError


def exchange_module():
    from scientist import peer_http_exchange
    return peer_http_exchange


class Response:
    status = 200
    length = 0
    def __init__(self, data=b'{"jsonrpc":"2.0"}'):
        self.data = data
    def getheader(self, name):
        return None
    def read(self, limit):
        return self.data[:limit]


@pytest.fixture
def socket_fixture(monkeypatch):
    calls = []
    response = Response()
    class Connection:
        def __init__(self, host, ip, port, timeout):
            self.host, self.ip, self.port, self.timeout = host, ip, port, timeout
            self.closed = False
            calls.append(self)
        def request(self, method, path, body=None, headers=None):
            self.sent = (method, path, body, headers)
        def getresponse(self):
            return response
        def close(self):
            self.closed = True
    monkeypatch.setattr(broker, "_PinnedHTTPSConnection", Connection)
    monkeypatch.setattr(broker, "_resolver", lambda host, port: ["8.8.8.8"])
    return calls, response


def call_peer(exchange, **changes):
    values = dict(url="https://peer.example/a2a", method="POST", headers=httpx.Headers({"Content-Type": "application/json", "A2A-Version": "1.0"}), body=b'{"jsonrpc":"2.0"}', timeout_ms=1000, max_response_bytes=4096)
    values.update(changes)
    return asyncio.run(exchange(**values))


def test_exchange_uses_pinned_ip_and_only_dedicated_auth(socket_fixture):
    calls, _ = socket_fixture
    exchange = exchange_module().pinned_exchange("https://peer.example", "dedicated-fixture", request_bytes_limit=4096)
    response = call_peer(exchange, headers=httpx.Headers({"Content-Type": "application/json", "A2A-Version": "1.0", "X-Unapproved": "discard"}))
    assert response.status_code == 200
    connection = calls[0]
    assert (connection.host, connection.ip, connection.port) == ("peer.example", "8.8.8.8", 443)
    method, path, body, headers = connection.sent
    assert (method, path, body) == ("POST", "/a2a", b'{"jsonrpc":"2.0"}')
    assert headers == {"Content-Type": "application/json", "A2A-Version": "1.0", "Host": "peer.example", "Authorization": "Bearer dedicated-fixture"}
    assert connection.closed


@pytest.mark.parametrize("changes", [
    {"url": "https://attacker.example/a2a"}, {"url": "https://peer.example/other"},
    {"url": "https://peer.example/a2a?query=x"}, {"method": "GET"},
    {"headers": httpx.Headers({"Cookie": "ambient"})},
    {"headers": httpx.Headers({"Authorization": "Bearer worker-choice"})},
    {"headers": httpx.Headers({"Proxy-Authorization": "ambient"})},
    {"body": b"x" * 4097}, {"timeout_ms": 0}, {"timeout_ms": True},
    {"max_response_bytes": 0}, {"max_response_bytes": 4097},
])
def test_unapproved_exchange_inputs_never_open_socket(socket_fixture, changes):
    calls, _ = socket_fixture
    exchange = exchange_module().pinned_exchange("https://peer.example", "fixture", request_bytes_limit=4096)
    with pytest.raises((DomainError, ValueError)):
        call_peer(exchange, **changes)
    assert calls == []


@pytest.mark.parametrize("addresses", [["169.254.169.254"], ["8.8.8.8", "127.0.0.1"], ["10.0.0.2", "169.254.169.254"], ["8.8.8.8", "10.0.0.2"]])
def test_mixed_or_unsafe_dns_fails_before_socket(socket_fixture, monkeypatch, addresses):
    calls, _ = socket_fixture
    monkeypatch.setattr(broker, "_resolver", lambda host, port: addresses)
    exchange = exchange_module().pinned_exchange("https://peer.example", "fixture", request_bytes_limit=4096)
    with pytest.raises(DomainError):
        call_peer(exchange)
    assert calls == []


@pytest.mark.parametrize("fault", ["redirect", "location", "empty_location", "oversize", "truncated"])
def test_peer_response_fault_never_retries(socket_fixture, fault):
    calls, response = socket_fixture
    if fault == "redirect": response.status = 302
    if fault == "location": response.getheader = lambda name: "https://attacker.example" if name == "Location" else None
    if fault == "empty_location": response.getheader = lambda name: "" if name == "Location" else None
    if fault == "oversize": response.data = b"x" * 4097
    if fault == "truncated": response.length = 1
    exchange = exchange_module().pinned_exchange("https://peer.example", "fixture", request_bytes_limit=4096)
    with pytest.raises(DomainError):
        call_peer(exchange)
    assert len(calls) == 1 and calls[0].closed


def test_exchange_preserves_canonical_public_ipv6_authority(socket_fixture):
    calls, _ = socket_fixture
    origin = "https://[2001:4860:4860::8888]"
    exchange = exchange_module().pinned_exchange(origin, "fixture", request_bytes_limit=4096)
    call_peer(exchange, url=origin + "/a2a")
    assert calls[0].host == "2001:4860:4860::8888"
    assert calls[0].ip == "2001:4860:4860::8888"
    assert calls[0].sent[3]["Host"] == "[2001:4860:4860::8888]"


def test_peer_exchange_accepts_frozen_release_upper_bounds(socket_fixture):
    calls,_=socket_fixture
    exchange=exchange_module().pinned_exchange('https://peer.example','fixture',request_bytes_limit=1048576)
    response=call_peer(exchange,body=b'x'*1048576,timeout_ms=30000,max_response_bytes=1048576)
    assert response.status_code==200 and len(calls)==1


def test_peer_dns_timeout_opens_no_socket_and_discards_late_answer(socket_fixture,monkeypatch):
    from threading import Event
    from time import monotonic
    entered,release,finished=Event(),Event(),Event()
    calls,_=socket_fixture
    def delayed_resolver(host,port):
        entered.set()
        release.wait(0.2)
        finished.set()
        return ['8.8.8.8']
    monkeypatch.setattr(broker,'_resolver',delayed_resolver)
    exchange=exchange_module().pinned_exchange('https://peer.example','fixture',request_bytes_limit=4096)
    started=monotonic()
    try:
        with pytest.raises(DomainError):call_peer(exchange,timeout_ms=20)
        assert entered.is_set() and monotonic()-started<0.15 and calls==[]
    finally:
        release.set();assert finished.wait(1)
    assert calls==[]
