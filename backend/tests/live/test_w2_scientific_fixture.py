from __future__ import annotations

import ast
import importlib.util
import sys
import types

import pytest
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from scientist.auth import DomainError


def _load_fixture():
    path = Path(__file__).parent / "fixtures" / "scientific" / "sitecustomize.py"
    spec = importlib.util.spec_from_file_location("w2_fixture_test", path)
    assert spec is not None and spec.loader is not None
    fixture = importlib.util.module_from_spec(spec)
    previous = sys.modules.get("uvicorn")
    sys.modules["uvicorn"] = types.SimpleNamespace(run=lambda *args, **kwargs: None)
    try:
        spec.loader.exec_module(fixture)
    finally:
        if previous is None:
            sys.modules.pop("uvicorn", None)
        else:
            sys.modules["uvicorn"] = previous
    return fixture


def _tools():
    specs = {
        "instruction_view": ("capability_id", ["paper-lookup", "exploratory-data-analysis"]),
        "scientific_search": ("request_id", "crossref"),
        "scientific_csv_describe": ("grant_id", "csv_describe"),
    }
    result = []
    for name, (arg, identifier) in specs.items():
        result.append({"type": "function", "function": {
            "name": name,
            "parameters": {
                "type": "object", "additionalProperties": False, "required": [arg],
                "properties": {arg: {"type": "string", "enum": identifier if isinstance(identifier, list) else [identifier]}},
            },
        }})
    return result


def test_w2_instruction_tool_matches_native_approved_capability_ids():
    fixture = _load_fixture()
    definitions = fixture._w2_tool_ids(SimpleNamespace(payload={"tools": _tools()}))
    instruction_schema = definitions["instruction_view"]["parameters"]
    assert instruction_schema["properties"]["capability_id"]["enum"] == [
        "paper-lookup",
        "exploratory-data-analysis",
    ]


def test_w2_transcript_reads_eda_instruction_before_search_and_compute():
    fixture = _load_fixture()
    run_id = UUID("c5f6b6a7-7f53-4a40-9c38-1151bc6eb7d8")
    target = SimpleNamespace(url="https://research.example")

    def request(messages):
        return SimpleNamespace(kind="llm", run_id=run_id, operation_id="synthetic", payload={
            "model": "fixture", "tools": _tools(), "messages": messages,
        })

    stage, instruction, finish = fixture._w2_stage(request([]), target)
    assert stage == "instruction" and finish == "tool_calls"
    instruction_call = instruction["tool_calls"][0]
    assert instruction_call["function"] == {
        "name": "instruction_view",
        "arguments": '{"capability_id":"exploratory-data-analysis"}',
    }
    instruction_result = {
        "role": "tool", "tool_call_id": instruction_call["id"],
        "content": "EDA instruction " * 24,
    }
    short_history = [
        {"role": "assistant", "tool_calls": [instruction_call]},
        {"role": "tool", "tool_call_id": instruction_call["id"], "content": "too short"},
    ]
    try:
        fixture._w2_stage(request(short_history), target)
    except RuntimeError as exc:
        assert "selected EDA instruction" in str(exc)
    else:
        raise AssertionError("W2 fixture advanced without consuming the selected instruction text")
    prior = [{"role": "assistant", "tool_calls": [instruction_call]}, instruction_result]

    stage, search, finish = fixture._w2_stage(request(prior), target)
    assert stage == "search" and finish == "tool_calls"
    search_call = search["tool_calls"][0]
    assert search_call["function"]["arguments"] == '{"request_id":"crossref"}'
    prior.extend([{"role": "assistant", "tool_calls": [search_call]}, {
        "role": "tool", "tool_call_id": search_call["id"], "content": "metadata"}])

    stage, compute, finish = fixture._w2_stage(request(prior), target)
    assert stage == "compute" and finish == "tool_calls"
    compute_call = compute["tool_calls"][0]
    assert compute_call["function"]["arguments"] == '{"grant_id":"csv_describe"}'
    prior.extend([{"role": "assistant", "tool_calls": [compute_call]}, {
        "role": "tool", "tool_call_id": compute_call["id"], "content": "summary"}])

    stage, final, finish = fixture._w2_stage(request(prior), target)
    assert stage == "final" and finish == "stop"
    assert "Crossref" in final["content"]


def test_w2_resolver_only_synthesizes_provider_and_uses_real_crossref_dns(monkeypatch):
    fixture = _load_fixture()
    resolved = []

    def real_getaddrinfo(host, port, *, type):
        resolved.append((host, port, type))
        return [(2, 1, 6, "", ("192.0.2.10", port))]

    monkeypatch.setattr(fixture.socket, "getaddrinfo", real_getaddrinfo)
    assert fixture._synthetic_resolver("research.example", 443) == ["93.184.216.34"]
    assert fixture._synthetic_resolver("api.crossref.org", 443) == ["192.0.2.10"]
    assert resolved == [("api.crossref.org", 443, fixture.socket.SOCK_STREAM)]
    try:
        fixture._synthetic_resolver("unapproved.example", 443)
    except OSError:
        pass
    else:
        raise AssertionError("fixture resolver allowed a non-Crossref network destination")



@pytest.mark.parametrize("status", [422, 503])
def test_observed_boundary_reraises_the_same_domain_error(status):
    fixture_path = Path(__file__).parent / "fixtures" / "scientific" / "sitecustomize.py"
    tree = ast.parse(fixture_path.read_text(encoding="utf-8"))
    observed = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "observed_boundary"
    )
    error = DomainError("invalid_scientific_result", status)
    state = {"calls": 0, "observed": 0}

    def original(*_args, **_kwargs):
        raise error

    env = {
        "DomainError": DomainError,
        "original": original,
        "boundary_state": state,
        "Path": Path,
        "sys": sys,
        "controller": SimpleNamespace(boundary=original),
    }
    module = ast.Module(body=[observed], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(fixture_path), "exec"), env)
    try:
        env["observed_boundary"]()
    except DomainError as caught:
        assert caught is error
    else:
        raise AssertionError("boundary DomainError was swallowed")
    assert state == {"calls": 1, "observed": int(status == 503)}

def test_targeted_stop_stall_records_operation_then_times_out(monkeypatch):
    fixture = _load_fixture()
    events = []
    table_present = {"value": False}

    class Result:
        def __init__(self, value=None, rowcount=0):
            self.value = value
            self.rowcount = rowcount

        def scalar_one(self):
            return self.value

        def scalar_one_or_none(self):
            return self.value

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, parameters=None):
            query = str(statement)
            events.append(("execute", query, parameters))
            if "to_regclass" in query:
                return Result("w2_fixture_stall_targets" if table_present["value"] else None)
            if query.startswith("SELECT 1 FROM w2_fixture_stall_targets"):
                return Result(1)
            if query.startswith("INSERT INTO w2_fixture_stalls"):
                return Result(rowcount=1)
            return Result()

        def commit(self):
            events.append(("commit",))

    fake_db = SimpleNamespace(session=lambda: Session())
    monkeypatch.setitem(sys.modules, "scientist", SimpleNamespace(db=fake_db))

    def fake_sleep(seconds):
        events.append(("sleep", seconds))

    monkeypatch.setattr(fixture.time, "sleep", fake_sleep)
    request = SimpleNamespace(run_id=UUID("c5f6b6a7-7f53-4a40-9c38-1151bc6eb7d8"), operation_id="op-stop-probe")
    assert fixture._stall_targeted_operation(request) is None
    assert len(events) == 1 and "to_regclass" in events[0][1]
    events.clear()
    table_present["value"] = True
    try:
        fixture._stall_targeted_operation(request)
    except TimeoutError as exc:
        assert "stopped before the provider response" in str(exc)
    else:
        raise AssertionError("targeted W2 stop probe returned a provider response")

    assert events[-2:] == [("commit",), ("sleep", 120)]
    marker = next(item for item in events if item[0] == "execute" and item[1].startswith("INSERT INTO w2_fixture_stalls"))
    assert marker[2] == {"run": request.run_id, "operation": request.operation_id}


def test_fixture_transport_preserves_bound_dispatch_and_delegates_crossref_to_native_get(monkeypatch):
    fixture = _load_fixture()
    from scientist import broker
    from scientist.contracts import CrossrefQueryV1, OperationRequest
    from scientist.scholarly_retrieval import build_crossref_url

    query = CrossrefQueryV1(
        source_id="crossref", version=1, access_mode="public_read",
        query="fixture query", doi=None, limit=5,
    )
    request = OperationRequest(
        run_id=UUID("c5f6b6a7-7f53-4a40-9c38-1151bc6eb7d8"),
        generation=1,
        operation_id="synthetic-crossref-get",
        kind="search",
        payload={"request_id": "crossref", "query": query.model_dump(mode="json")},
        reserve_tokens=0,
    )
    target = broker.DispatchTarget(
        kind="search",
        url=build_crossref_url(query),
        approved_recipients=("https://api.crossref.org",),
    )
    events = []

    class Response:
        status = 200

        def getheader(self, _name):
            return None

        def read(self, _limit):
            return b'{"status":"ok","message-type":"work-list","message":{"items":[]}}'

    class Connection:
        def __init__(self, host, ip, port, _timeout):
            events.append(("connect", host, ip, port))

        def request(self, method, path, *, body, headers):
            events.append(("request", method, path, body, headers))

        def getresponse(self):
            return Response()

        def close(self):
            events.append(("close",))

    resolved = []
    monkeypatch.setattr(broker, "_resolver", lambda host, port: resolved.append((host, port)) or ["8.8.8.8"])
    monkeypatch.setattr(broker, "_PinnedHTTPSConnection", Connection)
    synthetic_result = (b'{"synthetic":true}', 2)
    monkeypatch.setattr(fixture, "_synthetic_model", lambda _request, _target: synthetic_result)
    from scientist.dispatch_authority import BoundDispatchTransport

    identity = SimpleNamespace(executor_id=UUID("3dc75415-5c6b-4ad9-a80f-b9b63c0a1f11"), process_incarnation=UUID("7fffa5f1-9197-4e7c-b204-10a4710e168e"))
    bound = BoundDispatchTransport(identity.executor_id, identity.process_incarnation, broker.http_transport)
    monkeypatch.setattr(broker, "_transport", bound)
    installed, restore = fixture._install_fixture_transport(broker, identity)
    try:
        assert installed is bound and broker._transport is bound
        assert bound.executor_id == identity.executor_id
        assert bound.incarnation == identity.process_incarnation
        assert bound.transport(SimpleNamespace(kind="llm"), target) == synthetic_result
        assert events == [] and resolved == []
        result, usage = bound.transport(request, target)
        assert usage == 0
        assert b'"records":[]' in result
        assert resolved == [("api.crossref.org", 443)]
        assert [event for event in events if event[0] == "request"] == [
            ("request", "GET", "/works?query=fixture+query&rows=5", None, {"Host": "api.crossref.org"}),
        ]
        assert sum(event[0] == "connect" for event in events) == 1
    finally:
        restore()
    assert broker._transport is bound and bound.transport is broker.http_transport
