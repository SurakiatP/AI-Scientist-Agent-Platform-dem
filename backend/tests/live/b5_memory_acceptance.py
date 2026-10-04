import json,subprocess,time,uuid
from pathlib import Path
from b5_live_config import CFG
context=CFG.docker_context
image=CFG.worker_image
label="scientist.platform/acceptance=memory-"+uuid.uuid4().hex
def docker(*args,timeout=40):
 return subprocess.run(["docker","--context",context,*args],capture_output=True,text=True,timeout=timeout)
engine=docker("info","--format","{{.ID}}").stdout.strip()
assert engine
program="import time,pathlib; print('memory_max='+pathlib.Path('/sys/fs/cgroup/memory.max').read_text().strip(),flush=True); print('swap_max='+pathlib.Path('/sys/fs/cgroup/memory.swap.max').read_text().strip(),flush=True); blocks=[]\nfor i in range(96):\n blocks.append(bytearray(16*1024*1024)); time.sleep(.02)\nprint('LIMIT_FAILED',flush=True)"
container=None
try:
 r=docker("run","--detach","--pull=never","--label",label,"--network","none","--read-only","--user","65532:65532","--cap-drop","ALL","--security-opt","no-new-privileges","--memory","1g","--memory-swap","1g","--cpus","1","--pids-limit","128",image,"-c",program)
 assert r.returncode==0,r.stderr
 container=r.stdout.strip();assert len(container)==64
 waited=docker("wait",container);assert waited.returncode==0
 state=json.loads(docker("inspect","--format","{{json .State}}",container).stdout)
 limits=json.loads(docker("inspect","--format","{{json .HostConfig}}",container).stdout)
 logs=docker("logs",container).stdout
 assert state["OOMKilled"] is True and state["ExitCode"]==137,state
 assert limits["Memory"]==1073741824 and limits["MemorySwap"]==1073741824
 assert "memory_max=1073741824" in logs and "swap_max=0" in logs and "LIMIT_FAILED" not in logs,logs
 assert docker("info","--format","{{.ID}}").stdout.strip()==engine
 assert json.loads(docker("inspect","--format","{{json .Config.Labels}}",container).stdout).get("scientist.platform/acceptance")==label.split("=",1)[1]
 report={"scope":"dedicated exact-image hard memory proof only","image":image.split("@",1)[1],"engine_id":engine,"context":context,"container_id":container,"memory_max":1073741824,"swap_max":0,"oom_killed":True,"exit_code":137,"whole_b5_pass":False,"paid_calls":0}
finally:
 if container is not None:
  assert docker("info","--format","{{.ID}}").stdout.strip()==engine
  removed=docker("rm",container);assert removed.returncode==0,removed.stderr
  assert not docker("ps","-aq","--no-trunc","--filter","id="+container).stdout.strip()
report["exact_cleanup"]=True
(CFG.evidence/'b5-memory-acceptance.json').write_text(json.dumps(report,indent=2)+"\n")
print(json.dumps(report))
