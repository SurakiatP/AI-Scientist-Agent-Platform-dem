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
    return _real_run(app,*args,**kwargs)

uvicorn.run = _run_with_synthetic_transport
