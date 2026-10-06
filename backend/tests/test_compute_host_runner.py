"""Host-owned compute durability tests. All engine and object-store seams are fakes."""

from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import io
import json
from contextlib import contextmanager
from threading import Event
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scientist import broker, compute_runtime, host, objects, supervisor
from scientist.auth import DomainError
from scientist.contracts import ObjectRef, OperationRequest
from scientist.runtime_contracts import canonical_bytes


_RUN_ID = uuid4()
_PROJECT_ID = uuid4()
_OPERATION_ID = "compute-test"
_REQUEST = OperationRequest(
    run_id=_RUN_ID,
    generation=1,
    operation_id=_OPERATION_ID,
    kind="compute",
    payload={"grant_id": "csv-grant", "grant": {"approved": True}},
    reserve_tokens=0,
)
_STATE: dict = {}


class _Engine:
    context = "colima-scientist-platform-test"

    def engine_id(self):
        return "owned-engine-test"


class _Rows:
    def __init__(self, row=None, rows=None, rowcount=1):
        self.row = row
        self.rows = rows or []
        self.rowcount = rowcount

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return None if self.row is None else self.row.get("container_id")


class _DB:
    def execute(self, statement, params=None):
        sql = str(statement)
        if "SELECT o.run_id, o.operation_id" in sql:
            return _Rows(rows=[_STATE["schedule"]])
        if "FROM runtime_executors" in sql and "SELECT" in sql:
            return _Rows(dict(_STATE["executor"]))
        if "UPDATE runtime_executors SET state='starting'" in sql:
            executor = _STATE["executor"]
            matches = (
                executor["state"] == "unknown"
                and executor["container_id"] in (None, params["container"])
                and executor["engine_id"] in (None, params["engine"])
            )
            if matches:
                executor["state"] = "starting"
                _STATE.setdefault("events", []).append("executor-requalified")
            return _Rows(rowcount=int(matches))
        if "FROM operations" in sql and "SELECT" in sql:
            return _Rows({"id": _STATE["journal_id"], "result": _STATE["result"]})
        if "UPDATE operations SET result" in sql:
            _STATE["result"] = json.loads(params["result"])
            _STATE.setdefault("events", []).append("journal-update")
        return _Rows()

    def commit(self):
        if _STATE.get("events") is not None:
            _STATE["events"].append("journal-commit")

    def rollback(self):
        pass


class _Binding:
    def model_dump(self, mode="json"):
        return {"binding_version": 2, "frozen": True}


def _object_ref(project_id, data):
    digest = hashlib.sha256(data).hexdigest()
    return ObjectRef(
        project_id=project_id,
        key=f"{project_id}/{digest}",
        sha256=digest,
        size=len(data),
        content_type="application/octet-stream",
    )


def _grant():
    source = b"value\n1\n"
    digest = hashlib.sha256(source).hexdigest()
    return SimpleNamespace(
        profile_id="prof.csv-stdlib@py3.14.7",
        profile_version="1",
        image_digest="sha256:" + "d" * 64,
        recipe_manifest_sha256="a" * 64,
        recipe_id="csv.describe.v1",
        recipe_version="1",
        input_ref=_object_ref(_PROJECT_ID, source),
        input_sha256=digest,
        numeric_columns=["value"],
        max_input_bytes=1_048_576,
    )


def _configure(monkeypatch, tmp_path, *, is_new=True):
    grant = _grant()
    request = _REQUEST
    executor_id, incarnation = uuid4(), uuid4()
    unbound = supervisor.ExecutorRef(
        executor_id, _RUN_ID, 1, "compute", _OPERATION_ID, incarnation,
        "owned-engine-test", None,
    )
    bound = supervisor.ExecutorRef(
        executor_id, _RUN_ID, 1, "compute", _OPERATION_ID, incarnation,
        "owned-engine-test", "b" * 64,
    )
    initial_result = {"request": request.model_dump(mode="json")}
    _STATE.clear()
    _STATE.update({
        "journal_id": uuid4(),
        "result": initial_result,
        "schedule": {
            "run_id": _RUN_ID, "operation_id": _OPERATION_ID,
            "generation": 1, "result": initial_result,
        },
        "executor": {
            "id": executor_id, "generation": 1, "process_incarnation": incarnation,
            "engine_id": "owned-engine-test", "container_id": None,
            "state": "starting", "proof": {},
        },
        "store": {},
        "events": [],
        "ref": bound,
    })
    row = {"project_id": _PROJECT_ID, "revision": 4}

    def authority(_db, req):
        row["operation_result"] = _STATE["result"]
        return row, _Binding(), "csv-grant", grant

    monkeypatch.setattr(host, "_compute_authority", authority)
    monkeypatch.setattr(host, "_compute_engine", _Engine())
    monkeypatch.setattr(host, "_compute_image_digest", grant.image_digest)
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    monkeypatch.setattr(host, "_compute_recipe_directory", recipe)
    monkeypatch.setattr(host, "_compute_recipe_manifest", grant.recipe_manifest_sha256)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(host, "_compute_state_dir", state_dir)

    @contextmanager
    def session():
        yield _DB()

    monkeypatch.setattr(host.database, "session", session)

    @contextmanager
    def open_verified(ref):
        if ref == grant.input_ref:
            yield io.BytesIO(b"value\n1\n")
        else:
            yield io.BytesIO(_STATE["store"][ref.key])

    monkeypatch.setattr(objects, "open_verified", open_verified)

    def reserve(*_args, **_kwargs):
        return unbound, is_new

    monkeypatch.setattr(supervisor, "reserve_compute_executor", reserve)

    def bind(_db, ref):
        _STATE["executor"]["container_id"] = ref.container_id
        return ref

    monkeypatch.setattr(supervisor, "bind_compute_executor", bind)
    monkeypatch.setattr(supervisor, "start_compute_executor", lambda *_args, **_kwargs: None)

    def inactive(_db, ref):
        _STATE["executor"]["state"] = "inactive"
        _STATE["executor"]["proof"] = {
            "source": "owned-engine-exact-container",
            "engine_id": ref.engine_id,
            "container_id": ref.container_id,
        }

    monkeypatch.setattr(supervisor, "mark_compute_executor_inactive", inactive)
    monkeypatch.setattr(
        compute_runtime, "_verify_container",
        lambda *_args, **_kwargs: ({}, {"Status": "created", "Running": False}),
    )
    monkeypatch.setattr(compute_runtime, "start_compute", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(compute_runtime, "poll_compute", lambda *_args, **_kwargs: 0)
    output = {
        "summary.json": b"{}",
        "summary.csv": b"name,value\nvalue,1\n",
        "chart.svg": b"<svg/>",
        "report.md": b"# Report\n",
    }
    monkeypatch.setattr(compute_runtime, "read_compute_outputs", lambda *_args, **_kwargs: output)

    def put(_db, project_id, stream, _content_type):
        data = stream.read()
        _STATE["events"].append("put-attempt")
        ref = _object_ref(project_id, data)
        _STATE["store"][ref.key] = data
        return ref

    monkeypatch.setattr(objects, "put", put)
    return request, grant, unbound, bound, output


def _stage(monkeypatch, tmp_path):
    request, grant, _unbound, bound, output = _configure(monkeypatch, tmp_path)
    result = host._stage_compute_outputs(request, _PROJECT_ID, _Binding(), "csv-grant", grant, output)
    _STATE["executor"].update(container_id=bound.container_id, state="active")
    return request, grant, bound, result


def test_partial_object_put_replays_staged_bytes_without_compute_or_start(monkeypatch, tmp_path):
    request, grant, bound, _staged = _stage(monkeypatch, tmp_path)
    calls, events = 0, []
    staged_before = _STATE["result"]["compute_stage"]
    raw_envelope = base64.b64decode(staged_before["envelope_base64"], validate=True)
    parsed_envelope = json.loads(raw_envelope)
    assert raw_envelope == canonical_bytes(parsed_envelope)
    assert hashlib.sha256(raw_envelope).hexdigest() == staged_before["envelope_sha256"]
    for entry in parsed_envelope["outputs"]:
        payload = base64.b64decode(entry["data_base64"], validate=True)
        assert len(payload) == entry["object_ref"]["size"]
        assert hashlib.sha256(payload).hexdigest() == entry["object_ref"]["sha256"]
    assert _STATE["events"].index("journal-commit") > _STATE["events"].index("journal-update")
    original_put = objects.put
    creates, starts = [], []
    monkeypatch.setattr(compute_runtime, "create_compute", lambda *_args, **_kwargs: creates.append(True))
    monkeypatch.setattr(supervisor, "start_compute_executor", lambda *_args, **_kwargs: starts.append(True))

    def fail_once(db, project_id, stream, content_type):
        nonlocal calls
        calls += 1
        data = stream.read()
        _STATE["events"].append("put-attempt")
        if calls == 3:
            raise DomainError("storage_unavailable", 503)
        events.append("put")
        return original_put(db, project_id, io.BytesIO(data), content_type)

    monkeypatch.setattr(objects, "put", fail_once)
    monkeypatch.setattr(compute_runtime, "find_compute", lambda *_args, **_kwargs: bound)
    monkeypatch.setattr(compute_runtime, "stop_compute", lambda *_args, **_kwargs: events.append("stop"))
    monkeypatch.setattr(broker, "record_verified_completion", lambda *_args, **_kwargs: events.append("complete"))

    with pytest.raises(DomainError, match="storage_unavailable"):
        host._run_compute_operation(request)
    assert "compute_stage" in _STATE["result"]
    assert _STATE["result"]["compute_stage"] == staged_before

    host._run_compute_operation(request)
    assert calls == 8  # two durable puts, one failed put, then idempotent five-object replay
    assert creates == starts == []
    assert events.count("stop") == events.count("complete") == 1
    assert _STATE["result"]["compute_stage"] == staged_before
    assert _STATE["events"].index("journal-commit") < _STATE["events"].index("put-attempt")


def test_staged_output_readback_mismatch_blocks_completion(monkeypatch, tmp_path):
    request, grant, _bound, _staged = _stage(monkeypatch, tmp_path)
    original_open = objects.open_verified

    @contextmanager
    def corrupt_readback(ref):
        if ref == grant.input_ref:
            with original_open(ref) as stream:
                yield stream
        else:
            yield io.BytesIO(b"corrupt readback")

    monkeypatch.setattr(objects, "open_verified", corrupt_readback)
    with pytest.raises(RuntimeError, match="object readback differs"):
        host._resume_compute_stage(
            request, _PROJECT_ID, _Binding(), "csv-grant", grant, _STATE["result"]
        )


def test_create_ack_loss_does_not_create_again(monkeypatch, tmp_path):
    request, _grant_obj, unbound, _bound, _output = _configure(monkeypatch, tmp_path)
    reserve_results = iter((True, False))
    monkeypatch.setattr(
        supervisor, "reserve_compute_executor",
        lambda *_args, **_kwargs: (unbound, next(reserve_results)),
    )
    creates = []

    def create(*_args, **_kwargs):
        creates.append(True)
        raise TimeoutError("create acknowledgement lost")

    monkeypatch.setattr(compute_runtime, "create_compute", create)
    monkeypatch.setattr(compute_runtime, "find_compute", lambda *_args, **_kwargs: None)
    host._run_compute_operation(request)
    host._run_compute_operation(request)
    assert len(creates) == 1


@pytest.mark.parametrize(
    ("initial_state", "already_bound"),
    [("starting", False), ("unknown", False), ("unknown", True)],
)
def test_exactly_rediscovered_executor_requalifies_and_starts_without_create(
    monkeypatch, tmp_path, initial_state, already_bound
):
    request, _grant_obj, _unbound, bound, _output = _configure(monkeypatch, tmp_path, is_new=False)
    _STATE["executor"]["state"] = initial_state
    if already_bound:
        _STATE["executor"]["container_id"] = bound.container_id
    events = []
    monkeypatch.setattr(
        compute_runtime, "find_compute",
        lambda *_args, **_kwargs: (events.append("rediscover") or bound),
    )
    monkeypatch.setattr(
        compute_runtime, "create_compute",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not recreate")),
    )
    monkeypatch.setattr(
        supervisor, "start_compute_executor",
        lambda _db, ref, _request, start: (events.append("start"), start()),
    )
    monkeypatch.setattr(compute_runtime, "stop_compute", lambda *_args, **_kwargs: events.append("stop"))
    monkeypatch.setattr(broker, "record_verified_completion", lambda *_args, **_kwargs: events.append("complete"))

    host._run_compute_operation(request)

    assert events == ["rediscover", "start", "rediscover", "stop", "complete"]
    assert _STATE["executor"]["container_id"] == bound.container_id
    assert _STATE["executor"]["state"] == "inactive"
    if initial_state == "unknown":
        assert _STATE["events"].index("executor-requalified") < _STATE["events"].index("journal-commit")


def test_bound_unknown_executor_rejects_different_rediscovered_cid_without_requalifying(
    monkeypatch, tmp_path
):
    request, _grant_obj, _unbound, bound, _output = _configure(monkeypatch, tmp_path, is_new=False)
    _STATE["executor"].update(container_id=bound.container_id, state="unknown")
    wrong = replace(bound, container_id="c" * 64)
    monkeypatch.setattr(compute_runtime, "find_compute", lambda *_args, **_kwargs: wrong)
    monkeypatch.setattr(
        supervisor, "start_compute_executor",
        lambda *_args, **_kwargs: pytest.fail("mismatched CID must not start"),
    )
    monkeypatch.setattr(
        compute_runtime, "create_compute",
        lambda *_args, **_kwargs: pytest.fail("mismatched CID must not recreate"),
    )

    with pytest.raises(RuntimeError, match="could not be requalified"):
        host._run_compute_operation(request)

    assert _STATE["executor"]["state"] == "unknown"
    assert _STATE["executor"]["container_id"] == bound.container_id
    assert "executor-requalified" not in _STATE["events"]


def test_stale_generation_or_approval_prevents_launch_intent(monkeypatch, tmp_path):
    request, *_ = _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        host, "_compute_authority",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("stale approval or generation")),
    )
    reserved = []
    monkeypatch.setattr(supervisor, "reserve_compute_executor", lambda *_args, **_kwargs: reserved.append(True))
    with pytest.raises(RuntimeError, match="stale"):
        host._run_compute_operation(request)
    assert reserved == []


def test_start_stop_race_stops_bound_guest_and_records_unknown(monkeypatch, tmp_path):
    request, _grant_obj, _unbound, bound, _output = _configure(monkeypatch, tmp_path)
    events = []
    monkeypatch.setattr(compute_runtime, "create_compute", lambda *_args, **_kwargs: bound)
    monkeypatch.setattr(
        supervisor, "start_compute_executor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stop won start race")),
    )
    monkeypatch.setattr(compute_runtime, "stop_compute", lambda *_args, **_kwargs: events.append("stop-exact"))
    monkeypatch.setattr(broker, "_record_unknown", lambda *_args, **_kwargs: events.append("unknown"))
    host._run_compute_operation(request)
    assert events == ["stop-exact", "unknown"]


def test_compute_scheduler_tick_stays_fast_while_storage_put_is_stalled(monkeypatch, tmp_path):
    request, _grant_obj, _unbound, bound, _output = _configure(monkeypatch, tmp_path)
    entered, release = Event(), Event()
    original_put = objects.put

    def stalled_put(*args, **kwargs):
        entered.set()
        release.wait(3)
        return original_put(*args, **kwargs)

    monkeypatch.setattr(objects, "put", stalled_put)
    monkeypatch.setattr(compute_runtime, "create_compute", lambda *_args, **_kwargs: bound)
    monkeypatch.setattr(compute_runtime, "stop_compute", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(broker, "record_verified_completion", lambda *_args, **_kwargs: None)
    runner = host.Host(_Engine())
    try:
        started = monotonic()
        runner._compute_tick()
        assert monotonic() - started < 0.5
        assert entered.wait(1)
        started = monotonic()
        runner._compute_tick()
        assert monotonic() - started < 0.5
        assert list(runner._compute_futures) == [(request.run_id, request.operation_id)]
    finally:
        release.set()
        runner.close()
