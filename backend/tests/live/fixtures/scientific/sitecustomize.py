"""Synthetic provider and one-shot lost-boundary-ACK barrier for W1 live acceptance."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import uvicorn

_run = uvicorn.run


def _messages(request):
    payload = request.payload
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        raise RuntimeError("scientific fixture expected a model transcript")
    tool_messages = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
    ids = [m.get("tool_call_id") for m in tool_messages]
    if any(not isinstance(value, str) for value in ids) or len(ids) != len(set(ids)):
        raise RuntimeError("scientific fixture received an invalid tool transcript")
    return set(ids)


def _stage(request, target):
    if (request.kind != "llm" or target.url != "https://research.example" or
            request.payload.get("model") != "fixture"):
        raise RuntimeError("scientific fixture rejected an unexpected provider operation")
    ids = _messages(request)
    calls = [call for message in request.payload["messages"]
             if isinstance(message, dict) and message.get("role") == "assistant"
             for call in message.get("tool_calls", [])]
    if not ids and not calls:
        return "batch"
    expected = [("w1-instruction", "instruction_view", {"capability_id": "get-available-resources"}),
                ("w1-resources", "scientific_resources", {})]
    actual = []
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            raise RuntimeError("scientific fixture received an invalid assistant tool batch")
        try:
            arguments = function.get("arguments")
            arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        except (TypeError, json.JSONDecodeError):
            raise RuntimeError("scientific fixture received invalid tool arguments") from None
        actual.append((call.get("id"), function.get("name"), arguments))
    if ids == {"w1-instruction", "w1-resources"} and actual == expected:
        return "final"
    raise RuntimeError("scientific fixture rejected an unexpected provider transcript")


def _response(stage):
    if stage == "batch":
        message = {
            "role": "assistant", "content": None,
            "tool_calls": [
                {"id": "w1-instruction", "type": "function", "function": {
                    "name": "instruction_view", "arguments": '{"capability_id":"get-available-resources"}'}},
                {"id": "w1-resources", "type": "function", "function": {
                    "name": "scientific_resources", "arguments": "{}"}},
            ],
        }
        finish = "tool_calls"
    elif stage == "final":
        message = {"role": "assistant", "content": "Synthetic resource measurements recorded."}
        finish = "stop"
    else:
        raise RuntimeError("scientific fixture rejected an unexpected stage")
    return json.dumps({"id": "w1-completion", "object": "chat.completion", "created": 1,
                       "model": "fixture", "choices": [{"index": 0, "message": message,
                       "finish_reason": finish}],
                       "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
                      separators=(",", ":")).encode(), 2


def _hold_boundary(status, receipt_committed, first_for_run, targeted):
    return status == 200 and receipt_committed and first_for_run and targeted


def _synthetic_model(request, target):
    stage = _stage(request, target)
    from sqlalchemy import text
    from scientist import db

    with db.session() as session:
        session.execute(text("""CREATE TABLE IF NOT EXISTS w1_fixture_attempts (
            run_id uuid NOT NULL, stage text NOT NULL, operation_id text NOT NULL,
            attempted_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (run_id, stage))"""))
        inserted = session.execute(text("""INSERT INTO w1_fixture_attempts(run_id,stage,operation_id)
            VALUES (:run,:stage,:operation) ON CONFLICT DO NOTHING"""),
            {"run": request.run_id, "stage": stage, "operation": request.operation_id}).rowcount
        session.commit()
    if inserted != 1:
        raise RuntimeError("scientific fixture rejected a repeated stage identity")
    return _response(stage)


class _BoundaryAckBarrier:
    """Pause once after a committed scientific receipt and before its HTTP ACK is sent."""

    def __init__(self, app, executor):
        self.app, self.executor = app, executor

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") != "/control/boundary":
            await self.app(scope, receive, send)
            return
        response = []
        async def capture(message):
            response.append(message)
        await self.app(scope, receive, capture)
        start = next((m for m in response if m.get("type") == "http.response.start"), None)
        if start is None or start.get("status") != 200:
            for message in response:
                await send(message)
            return

        from sqlalchemy import text
        from scientist import db, objects
        from scientist.contracts import ObjectRef
        run_id, generation = self.executor.run_id, self.executor.generation
        with db.session() as session:
            contexts = session.execute(text("""SELECT c.manifest->'context' AS ref
                FROM scientific_artifact_receipts r JOIN checkpoints c ON c.id=r.checkpoint_id
                WHERE r.run_id=:run"""), {"run": run_id}).mappings().all()
        committed = False
        for row in contexts:
            ref = ObjectRef.model_validate(row["ref"])
            with objects.open_verified(ref) as source:
                context = json.loads(source.read(1_048_577))
            committed = committed or context.get("boundary") == "tool_committed"
        if not committed:
            for message in response:
                await send(message)
            return
        # The owner acceptance harness arms one recovery run before approval.
        # Ordinary resource runs, including the browser run, pass through untouched.
        with db.session() as session:
            targeted = session.execute(text(
                "SELECT 1 FROM w1_boundary_fault_targets WHERE run_id=:run"
            ), {"run": run_id}).scalar_one_or_none()
        if targeted is None:
            for message in response:
                await send(message)
            return
        with db.session() as session:
            session.execute(text("""CREATE TABLE IF NOT EXISTS w1_boundary_barriers (
                run_id uuid NOT NULL, generation bigint NOT NULL, released boolean NOT NULL DEFAULT false,
                PRIMARY KEY (run_id))"""))
            claimed = session.execute(text("""INSERT INTO w1_boundary_barriers(run_id,generation)
                VALUES (:run,:generation) ON CONFLICT (run_id) DO NOTHING"""),
                {"run": run_id, "generation": generation}).rowcount
            session.commit()
        if _hold_boundary(start["status"], committed, claimed == 1, targeted is not None):
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                with db.session() as session:
                    released = session.execute(text("""SELECT released FROM w1_boundary_barriers
                        WHERE run_id=:run AND generation=:generation"""),
                        {"run": run_id, "generation": generation}).scalar_one()
                if released:
                    break
                await asyncio.sleep(0.05)
            else:
                raise TimeoutError("scientific boundary-ACK barrier expired")
        for message in response:
            await send(message)


def _with_fixture(app, *args, **kwargs):
    from scientist import broker
    from scientist.dispatch_authority import BoundDispatchTransport
    from scientist.dispatch_runtime import parse_dispatch_config

    identity = parse_dispatch_config(Path("/run/scientist/dispatch/config.json").read_bytes())
    broker._transport = BoundDispatchTransport(identity.executor_id, identity.process_incarnation, _synthetic_model)
    return _run(_BoundaryAckBarrier(app, identity), *args, **kwargs)


uvicorn.run = _with_fixture
