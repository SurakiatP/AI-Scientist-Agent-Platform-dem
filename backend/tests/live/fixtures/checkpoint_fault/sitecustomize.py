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


def _install_checkpoint_faults():
    """Test-only checkpoint fault injection driven by b5_fixture_checkpoint_faults.

    A row (run_id, generation, kind) is consumed by the first capture of that generation that happens after
    at least one durable checkpoint exists, so the previous checkpoint is always a real committed one.
    kind=db_commit: run the real capture (objects uploaded, rows inserted in the caller transaction), then raise
    before the endpoint commits. kind=upload: objects.put raises on its first call during capture.
    """
    from sqlalchemy import text
    from scientist import checkpoints, db, objects  # `objects` is the module checkpoints.capture calls put() on
    real_capture = checkpoints.capture

    def faulting_capture(session, run_id, generation, context, workspace_dir):
        kind = None
        with db.session() as fault_session:
            fault_session.execute(text("CREATE TABLE IF NOT EXISTS b5_fixture_checkpoint_faults (run_id uuid NOT NULL, generation int NOT NULL, kind text NOT NULL, consumed boolean NOT NULL DEFAULT false, PRIMARY KEY (run_id, generation))"))
            fault_session.commit()
            has_checkpoint = fault_session.execute(text("SELECT 1 FROM checkpoints WHERE run_id=:run LIMIT 1"), {"run": run_id}).scalar_one_or_none()
            if has_checkpoint:
                kind = fault_session.execute(text("UPDATE b5_fixture_checkpoint_faults SET consumed=true WHERE run_id=:run AND generation=:generation AND NOT consumed RETURNING kind"), {"run": run_id, "generation": generation}).scalar_one_or_none()
                fault_session.commit()
        if kind == 'db_commit':
            real_capture(session, run_id, generation, context, workspace_dir)
            raise RuntimeError('Synthetic checkpoint database commit fault')
        if kind == 'upload':
            real_put = objects.put
            def failing_put(*args, **kwargs):
                raise RuntimeError('Synthetic checkpoint object upload fault')
            objects.put = failing_put
            try:
                return real_capture(session, run_id, generation, context, workspace_dir)
            finally:
                objects.put = real_put
        return real_capture(session, run_id, generation, context, workspace_dir)

    # private_dispatch_entrypoint.create_dispatch_app reads `checkpoints.capture` (WorkerController(capture=...)) before
    # uvicorn.run, so this module attribute must be replaced at interpreter start, before the entrypoint imports run.
    checkpoints.capture = faulting_capture


uvicorn.run = _run_with_synthetic_transport
_install_checkpoint_faults()  # import time, before private_dispatch_entrypoint binds checkpoints.capture
