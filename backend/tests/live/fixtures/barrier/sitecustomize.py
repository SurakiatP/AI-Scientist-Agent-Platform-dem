"""Synthetic transport injection in a separate test image, never production."""
import json
from pathlib import Path
import uvicorn

_real_run = uvicorn.run

def _run_with_synthetic_transport(app, *args, **kwargs):
    from scientist import broker
    from scientist.dispatch_authority import BoundDispatchTransport
    from scientist.dispatch_runtime import parse_dispatch_config
    identity = parse_dispatch_config(Path('/run/scientist/dispatch/config.json').read_bytes())

    def synthetic_model(request, target):
        if request.kind != 'llm' or target.url != 'https://research.example':
            raise RuntimeError('unsupported synthetic fixture operation')
        from sqlalchemy import text
        from scientist import db
        with db.session() as counter_session:
            counter_session.execute(text("CREATE TABLE IF NOT EXISTS b5_fixture_provider_attempts (attempt_id uuid PRIMARY KEY, run_id uuid NOT NULL, operation_id text NOT NULL, attempted_at timestamptz NOT NULL DEFAULT now())"))
            counter_session.execute(text("INSERT INTO b5_fixture_provider_attempts (attempt_id, run_id, operation_id) VALUES (gen_random_uuid(), :run, :operation)"), {"run": request.run_id, "operation": request.operation_id})
            counter_session.commit()
        if request.payload.get('model') == 'fixture-unknown':
            raise TimeoutError('Synthetic provider lost its remote response')
        if request.payload.get('model') == 'fixture-stall':
            import time
            time.sleep(20)
        response = {'id':'fixture-completion','object':'chat.completion','created':1,'model':'fixture',
            'choices':[{'index':0,'message':{'role':'assistant','content':'Synthetic research synthesis with no paid call.'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}
        return json.dumps(response,separators=(',',':')).encode(), 2

    # Fixture-only replacement: preserve auth, persistence and destination authority.
    broker._transport = BoundDispatchTransport(identity.executor_id, identity.process_incarnation, synthetic_model)
    broker._resolver = lambda host, port: ["8.8.8.8"]
    if os.environ.get("SCIENTIST_B5_FAULT") == "after_result_commit_before_delivery":
        app = DeliveryBarrier(app, identity)
    return _real_run(app,*args,**kwargs)

uvicorn.run = _run_with_synthetic_transport
import asyncio
import os


class DeliveryBarrier:
    """Test-only barrier after the effects handler returns, before HTTP delivery."""

    def __init__(self, app, identity):
        self.app = app
        self.identity = identity

    async def __call__(self, scope, receive, send):
        async def bounded_send(message):
            if (scope.get("type") == "http" and scope.get("path") == "/effects"
                    and message.get("type") == "http.response.start"
                    and message.get("status") == 200):
                from sqlalchemy import text
                from scientist import db
                key = {"run": self.identity.run_id, "generation": self.identity.generation}
                with db.session() as barrier_db:
                    barrier_db.execute(text("CREATE TABLE IF NOT EXISTS b5_fixture_delivery_barriers (run_id uuid NOT NULL, generation bigint NOT NULL, reached_at timestamptz NOT NULL DEFAULT now(), released boolean NOT NULL DEFAULT false, PRIMARY KEY (run_id, generation))"))
                    barrier_db.execute(text("INSERT INTO b5_fixture_delivery_barriers (run_id, generation) VALUES (:run, :generation) ON CONFLICT (run_id, generation) DO NOTHING"), key)
                    barrier_db.commit()
                deadline = asyncio.get_running_loop().time() + 30
                while asyncio.get_running_loop().time() < deadline:
                    with db.session() as barrier_db:
                        released = barrier_db.execute(text("SELECT released FROM b5_fixture_delivery_barriers WHERE run_id=:run AND generation=:generation"), key).scalar_one()
                    if released:
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise TimeoutError("Synthetic delivery barrier was not released")
            await send(message)

        await self.app(scope, receive, bounded_send)
