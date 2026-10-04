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
        model = request.payload.get('model')
        if model == 'fixture-unknown':
            raise TimeoutError('Synthetic provider lost its remote response')
        if model == 'fixture-unknown-once':
            # Lose only the run's first remote response; an owner retry then succeeds.
            with db.session() as once_session:
                first = once_session.execute(text("SELECT count(*) FROM b5_fixture_provider_attempts WHERE run_id=:run"), {"run": request.run_id}).scalar_one() == 1
            if first:
                raise TimeoutError('Synthetic provider lost its first remote response')
        if model == 'fixture-stall-db':
            # Hold until the harness inserts a release row (or 20 s), so tests need no wall-clock guess.
            import time
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                with db.session() as stall_session:
                    stall_session.execute(text("CREATE TABLE IF NOT EXISTS b5_fixture_stall_release (run_id uuid PRIMARY KEY, released_at timestamptz NOT NULL DEFAULT now())"))
                    stall_session.commit()
                    if stall_session.execute(text("SELECT 1 FROM b5_fixture_stall_release WHERE run_id=:run"), {"run": request.run_id}).scalar_one_or_none():
                        break
                time.sleep(0.05)
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
    return _real_run(app,*args,**kwargs)

uvicorn.run = _run_with_synthetic_transport
