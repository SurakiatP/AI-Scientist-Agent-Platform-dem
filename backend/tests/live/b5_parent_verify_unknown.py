import hashlib
import json
import re
import subprocess
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from scientist.db import session

from b5_live_config import CFG

report_path = CFG.evidence / 'b5-native-unknown-acceptance.json'
report = json.loads(report_path.read_text())
assert report['status'] == 'PASS' and report['paid_calls'] == 0
assert report['service_image_digest'] == CFG.digest(CFG.fixture_images['counter'])
assert report['worker_image_digest'] == CFG.worker_image_id
run_id = UUID(report['run_id'])
operation_id = report['unknown_snapshot']['operation_id']
with session() as db:
    db.execute(text('SET TRANSACTION READ ONLY'))
    run = db.execute(text('SELECT state,waiting_reason,usage_tokens,reserved_tokens,generation FROM runs WHERE id=:r'), {'r': run_id}).mappings().one()
    operation = db.execute(text('SELECT operation_id,state,reserve_tokens,usage_tokens,result FROM operations WHERE run_id=:r'), {'r': run_id}).mappings().one()
    assert (run['state'], run['waiting_reason']) == ('waiting_input', 'unknown_outcome')
    assert run['usage_tokens'] == operation['usage_tokens'] == 0
    assert run['reserved_tokens'] == operation['reserve_tokens'] == 3623
    assert run['generation'] == 1
    assert operation['state'] == 'unknown' and operation['operation_id'] == operation_id
    assert operation['result']['usage_known'] is False
    assert not operation['result'].get('ref')
    attempts = db.execute(text('SELECT count(*) FROM b5_fixture_provider_attempts WHERE run_id=:r'), {'r': run_id}).scalar_one()
    per_operation_attempts = db.execute(text('SELECT count(*) FROM b5_fixture_provider_attempts WHERE run_id=:r AND operation_id=:o'), {'r': run_id, 'o': operation_id}).scalar_one()
    assert attempts == per_operation_attempts == 1

assert report['fresh_supervisor_process_recovery']['fresh_process']
assert report['fresh_supervisor_process_recovery']['result'] == 'PASS'
cleanup = report['cleanup']
assert cleanup['status'] == 'PASS' and cleanup['owner_wait_preserved']
assert cleanup['executors_inactive'] and cleanup['network_removed']
docker = ['docker', '--context', CFG.docker_context]
engine_id = subprocess.check_output(docker + ['info', '--format', '{{.ID}}'], text=True).strip()
assert engine_id == CFG.expected_engine_id
container_ids = [proof['container_id'] for proof in cleanup['executor_proofs']]
assert len(container_ids) == 2 and all(re.fullmatch('[0-9a-f]{64}', value) for value in container_ids)
assert all(proof['engine_id'] == engine_id for proof in cleanup['executor_proofs'])
absent = [subprocess.run(docker + ['container', 'inspect', value], capture_output=True).returncode != 0 for value in container_ids]
absent.append(subprocess.run(docker + ['network', 'inspect', cleanup['network_id']], capture_output=True).returncode != 0)
assert all(absent)
proof = {
    'status': 'PASS', 'scope': 'native unknown outcome and supervisor process reload only',
    'run_id': str(run_id), 'operation_id': operation_id,
    'usage': 0, 'held_reservation': 3623,
    'actual_DB_provider_attempts': attempts, 'actual_DB_perop_attempts': per_operation_attempts,
    'exact_resource_absence': absent,
    'report_sha256': hashlib.sha256(report_path.read_bytes()).hexdigest(),
    'paid_calls': 0, 'full_deployment_restart': 'NOT_TESTED', 'whole_B5': 'PENDING',
}
(CFG.evidence / 'b5-final-canonical-unknown-parent-verification.json').write_text(json.dumps(proof, indent=2) + '\n')
print(json.dumps({'parent_actual_PG_checks': 'PASS', 'held_reservation': 3623, 'attempts': attempts, 'physical_cleanup': all(absent)}))
