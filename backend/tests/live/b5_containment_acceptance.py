#!/usr/bin/env python3
"""Replayable three-run, real-container containment acceptance probe.

This exercises the pinned final worker candidate and namespace firewall policy
on the project-owned Colima profile. The entrypoint is overridden for the
deterministic probes, so this is not application-startup acceptance.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import uuid
import hashlib
from pathlib import Path

from b5_live_config import CFG


CONTEXT = CFG.docker_context
PROFILE = CFG.colima_profile
IMAGE = CFG.worker_image
EXPECTED_IMAGE_ID = CFG.worker_image_id
HISTORICAL_BOOTSTRAP_IMAGE = "sha256:8cbde14ca35ea7cf9977c77260d6fe8d95dfc275e2155a37151f88042c75a04d"
ENGINE_VERSION = CFG.engine_version
WORKER_PROGRAM = "import time; time.sleep(86400)"
SERVER_PROGRAM = r'''
import json, socket, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

state = Path('/tmp/accepted.json')
state.write_text(json.dumps({'marker': 'private-sentinel', 'writes': []}))
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = json.loads(state.read_text())
        if self.path == '/reset':
            data['writes'] = []
            state.write_text(json.dumps(data))
            body = b'ok'
        else:
            body = json.dumps(data).encode()
        self.server.requests += 1
        self.send_response(200); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        data = json.loads(state.read_text())
        data['writes'].append(self.rfile.read(int(self.headers.get('Content-Length', '0'))).decode())
        state.write_text(json.dumps(data))
        self.server.requests += 1
        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
    def log_message(self, *_): pass

servers = []
for family, addr in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')):
    try:
        server = ThreadingHTTPServer((addr, 8124), Handler, bind_and_activate=False)
        server.address_family = family
        server.socket = socket.socket(family, socket.SOCK_STREAM)
        if family == socket.AF_INET6: server.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        server.server_bind(); server.server_activate(); server.requests = 0
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    except OSError:
        pass
if not servers: raise RuntimeError('no sentinel listener could bind')
threading.Event().wait()
'''

PROBE_PROGRAM = r'''
import json, socket, sys, struct
targets = json.loads(sys.argv[1])
full = len(sys.argv) > 2 and sys.argv[2] == 'full'
def request(host, port, path, method='GET', body=None):
    try:
        family = socket.AF_INET6 if ':' in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM); sock.settimeout(0.7)
        sock.connect((host, int(port))); payload = (body or '').encode()
        req = (f'{method} {path} HTTP/1.0\r\nHost: probe\r\nContent-Length: {len(payload)}\r\n\r\n').encode()+payload
        sock.sendall(req); data = sock.recv(4096); sock.close()
        return {'connected': data.startswith(b'HTTP/1.0 200')}
    except Exception as exc: return {'connected': False, 'error': type(exc).__name__}
out = {'targets': {name: {method.lower(): request(addr, port, '/secret' if method == 'GET' else '/write', method, 'probe-write')
                          for method in ('GET','POST')} for name, (addr, port) in targets.items()}}
try:
    addresses = sorted({item[4][0] for item in socket.getaddrinfo('sentinel', 8124, type=socket.SOCK_STREAM)})
    out['dns_control'] = {'resolved': True, 'addresses': addresses}
except Exception as exc: out['dns_control'] = {'resolved': False, 'error': type(exc).__name__}
if not full:
    print(json.dumps(out, sort_keys=True)); raise SystemExit(0)
try:
    socket.getaddrinfo('sentinel', 8124, type=socket.SOCK_STREAM)
    out['dns'] = {'resolved': True}
except Exception as exc: out['dns'] = {'resolved': False, 'error': type(exc).__name__}
qid = 0xB512
question = b'\x07example\x03com\x00' + struct.pack('!HH', 28, 1)
query = struct.pack('!HHHHHH', qid, 0x0100, 1, 0, 0, 0) + question
try:
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM); sock.settimeout(0.7)
    out['dns_v6'] = {'attempted': True}
    sock.connect(('2606:4700:4700::1111', 53))
    sock.sendall(struct.pack('!H', len(query)) + query)
    try: sock.recv(512); out['dns_v6']['response_received'] = True
    except socket.timeout: out['dns_v6']['response_received'] = False
    sock.close()
except Exception as exc: out['dns_v6'] = {**out.get('dns_v6', {}), 'attempted': True,
                                           'response_received': False, 'error': type(exc).__name__}
unix_paths = ['/var/run/docker.sock', '/run/docker.sock', '/run/containerd/containerd.sock',
              '/var/run/containerd/containerd.sock', '/run/podman/podman.sock']
out['unix_sockets'] = {}
for path in unix_paths:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); sock.settimeout(0.2)
    try: sock.connect(path); out['unix_sockets'][path] = True
    except Exception: out['unix_sockets'][path] = False
    finally: sock.close()
def listeners(path):
    try: return [{'table':path, 'local':line.split()[1]} for line in open(path).read().splitlines()[1:] if line.split()[3] == '0A']
    except OSError: return []
out['listening_sockets'] = listeners('/proc/net/tcp') + listeners('/proc/net/tcp6')
out['native_listeners'] = [item for item in out['listening_sockets']
                           if not (item['table'] == '/proc/net/tcp' and item['local'].startswith('0B00007F:'))]
print(json.dumps(out, sort_keys=True))
'''


class HarnessError(RuntimeError):
    pass


def run(argv: list[str], *, timeout: int = 45, check: bool = True) -> str:
    cp = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, timeout=timeout, check=False)
    if check and cp.returncode:
        raise HarnessError(f"command failed ({cp.returncode}): {argv!r}: {cp.stderr[-1200:]}")
    return cp.stdout.strip()


def docker(*args: str, timeout: int = 45, check: bool = True) -> str:
    return run(["docker", "--context", CONTEXT, *args], timeout=timeout, check=check)


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def vm(container_id: str, pid: int, *args: str, timeout: int = 45) -> str:
    if not re.fullmatch(r"[a-f0-9]{64}", container_id) or pid <= 0:
        raise HarnessError("invalid owned container identity")
    if not args or args[0] not in {"iptables", "ip6tables", "iptables-save", "ip6tables-save", "ip"}:
        raise HarnessError("namespace command is outside the firewall allowlist")
    return run(["colima", "ssh", "--profile", PROFILE, "--", "sudo", "-n",
                "nsenter", "-t", str(pid), "-n", *args], timeout=timeout)


def inspect(cid: str) -> dict:
    return json.loads(docker("inspect", cid))[0]


def resource_is_absent(resource_type: str, name: str) -> bool:
    cp = subprocess.run(["docker", "--context", CONTEXT, resource_type, "inspect", name],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                        timeout=20, check=False)
    if cp.returncode == 0:
        return False
    diagnostic = cp.stderr.lower()
    if (resource_type == "container" and "no such container" in diagnostic) or (
            resource_type == "network" and (
                "no such network" in diagnostic or f"network {name.lower()} not found" in diagnostic
            )):
        return True
    raise HarnessError(f"could not verify {resource_type} cleanup: {cp.stderr[-500:]}")


def assert_project_binding(cid: str, run_id: uuid.UUID, project_id: uuid.UUID) -> None:
    labels = (inspect(cid)["Config"].get("Labels") or {})
    if labels.get("b5.acceptance") != str(run_id) or labels.get("b5.project") != str(project_id):
        raise HarnessError("test resource labels do not match its declared run/project identity")


def exec_python(cid: str, code: str, *args: str, timeout: int = 20) -> str:
    return docker("exec", cid, "/opt/python/bin/python3.14", "-c", code, *args, timeout=timeout)


def pick_free_third_octet(start: int, used: set[int]) -> int:
    """First unused 172.29.<third>.0/29 octet in 32..221, scanning from `start`."""
    for offset in range(190):
        third = 32 + (start + offset) % 190
        if third not in used:
            return third
    raise HarnessError("no free 172.29.x subnet for the acceptance network")


def used_third_octets() -> set[int]:
    """172.29.x subnets already held by any Docker network (same scan as supervisor.create_run_network)."""
    used: set[int] = set()
    for network_id in docker("network", "ls", "-q").splitlines():
        if not network_id:
            continue
        try:
            configs = json.loads(docker("network", "inspect", "--format", "{{json .IPAM.Config}}", network_id))
        except Exception:
            continue
        for config in configs or []:
            subnet = config.get("Subnet") or ""
            if subnet.startswith("172.29."):
                try:
                    used.add(int(subnet.split(".")[2]))
                except (IndexError, ValueError):
                    continue
    return used


def create_network(run_id: uuid.UUID, project_id: uuid.UUID, suffix: str,
                   owned_networks: list[str]) -> tuple[str, ipaddress.IPv4Network, ipaddress.IPv6Network]:
    # UUID-derived start, advanced past subnets other networks already use. Never deletes networks.
    digest = hashlib.sha256(run_id.bytes + suffix.encode()).digest()
    v4 = ipaddress.ip_network(f"172.29.{pick_free_third_octet(digest[0] % 190, used_third_octets())}.0/29")
    v6 = ipaddress.ip_network(f"fd42:{digest[1]:x}{digest[2]:x}:{digest[3]:x}::/64")
    name = f"b5-accept-{run_id.hex[:12]}-{suffix}"
    docker("network", "create", "--driver", "bridge", "--internal", "--ipv6",
           "--subnet", str(v4), "--subnet", str(v6),
           "--label", f"b5.acceptance={run_id}", "--label", f"b5.project={project_id}", name)
    owned_networks.append(name)
    return name, v4, v6


def create_sentinel(run_id: uuid.UUID, project_id: uuid.UUID, index: int, network: str,
                    v4: ipaddress.IPv4Network, v6: ipaddress.IPv6Network,
                    owned_containers: list[str]) -> tuple[str, str, str]:
    name = f"b5-accept-{run_id.hex[:12]}-sentinel-{index}"
    cid = docker("create", "--name", name, "--label", f"b5.acceptance={run_id}",
                 "--label", f"b5.project={project_id}",
                 "--network", network, "--network-alias", "sentinel",
                 "--ip", str(v4.network_address + 4),
                 "--ip6", str(v6.network_address + 4), "--read-only", "--user", "65532:65532",
                 "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                 "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=1048576,mode=1777",
                 "--entrypoint", "/opt/python/bin/python3.14", IMAGE, "-c", SERVER_PROGRAM)
    owned_containers.append(cid)
    docker("start", cid)
    return cid, str(v4.network_address + 4), str(v6.network_address + 4)


def create_broker(run_id: uuid.UUID, project_id: uuid.UUID, index: int, network: str,
                  v4: ipaddress.IPv4Network, owned_containers: list[str]) -> tuple[str, str]:
    name = f"b5-accept-{run_id.hex[:12]}-broker-{index}"
    cid = docker("create", "--name", name, "--label", f"b5.acceptance={run_id}",
                 "--label", f"b5.project={project_id}",
                 "--network", network, "--ip", str(v4.network_address + 2),
                 "--read-only", "--user", "65532:65532", "--cap-drop", "ALL",
                 "--security-opt", "no-new-privileges:true",
                 "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=1048576,mode=1777",
                 "--entrypoint", "/opt/python/bin/python3.14", IMAGE, "-c",
                 SERVER_PROGRAM.replace("8124", "8123"))
    owned_containers.append(cid)
    docker("start", cid)
    return cid, str(v4.network_address + 2)


def create_worker(run_id: uuid.UUID, project_id: uuid.UUID, index: int, network: str,
                  v4: ipaddress.IPv4Network, v6: ipaddress.IPv6Network,
                  owned_containers: list[str]) -> str:
    name = f"b5-accept-{run_id.hex[:12]}-worker-{index}"
    cid = docker("create", "--name", name, "--label", f"b5.acceptance={run_id}",
                 "--label", f"b5.project={project_id}",
                 "--label", "scientist.platform/kind=worker", "--network", network,
                 "--ip", str(v4.network_address + 3), "--ip6", str(v6.network_address + 3),
                 "--read-only", "--user", "65532:65532", "--cap-drop", "ALL",
                 "--security-opt", "no-new-privileges:true", "--cpus", "1",
                 "--memory", "1073741824", "--memory-swap", "1073741824",
                 "--pids-limit", "128", "--shm-size", "16777216",
                 "--ulimit", "nofile=1024:1024",
                 "--tmpfs", "/workspace:rw,noexec,nosuid,nodev,size=67108864,mode=1777",
                 "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16777216,mode=1777",
                 "--entrypoint", "/opt/python/bin/python3.14", IMAGE, "-c", WORKER_PROGRAM)
    owned_containers.append(cid)
    docker("start", cid)
    return cid


def probe(cid: str, targets: dict[str, tuple[str, int]], *, full: bool = False) -> dict:
    args = (json.dumps(targets), "full") if full else (json.dumps(targets),)
    return json.loads(exec_python(cid, PROBE_PROGRAM, *args))


def install_policy(cid: str, broker_ip: str, broker_port: int = 8123) -> None:
    info = inspect(cid)
    labels = info["Config"].get("Labels") or {}
    if labels.get("b5.acceptance") is None or labels.get("scientist.platform/kind") != "worker":
        raise HarnessError("refusing policy operation on a non-test worker")
    pid = info["State"].get("Pid", 0)
    if not info["State"].get("Running") or pid <= 0:
        raise HarnessError("test worker is not running")
    full_id = info["Id"]
    for binary, family in (("iptables", 4), ("ip6tables", 6)):
        commands = [("-w", "-F", "OUTPUT"), ("-w", "-P", "OUTPUT", "DROP"),
                    ("-w", "-F", "INPUT"), ("-w", "-P", "INPUT", "DROP")]
        if family == 4:
            # Match the supervisor's required rule order: DNS before loopback.
            commands.append(("-w", "-A", "OUTPUT", "-d", "127.0.0.11", "-j", "DROP"))
        commands.extend([("-w", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"),
                         ("-w", "-A", "INPUT", "-i", "lo", "-j", "ACCEPT"),
                         ("-w", "-A", "INPUT", "-m", "conntrack", "--ctstate",
                          "ESTABLISHED,RELATED", "-j", "ACCEPT")])
        for args in commands:
            vm(full_id, pid, binary, *args)
        for args in (("-w", "-A", "OUTPUT", "-m", "conntrack", "--ctstate",
                      "ESTABLISHED,RELATED", "-j", "ACCEPT"),):
            vm(full_id, pid, binary, *args)
        if family == 4:
            vm(full_id, pid, binary, "-w", "-A", "OUTPUT", "-d", broker_ip,
               "-p", "tcp", "--dport", str(broker_port), "-m", "conntrack",
               "--ctstate", "NEW", "-j", "ACCEPT")


def output_counters(cid: str, binary: str) -> str:
    info = inspect(cid)
    return vm(info["Id"], info["State"]["Pid"], binary, "-w", "-v", "-x", "-n", "-L", "OUTPUT")


def dns_rule_order(cid: str) -> dict:
    info = inspect(cid)
    saved = vm(info["Id"], info["State"]["Pid"], "iptables-save", "-t", "filter")
    output_rules = [line for line in saved.splitlines() if line.startswith("-A OUTPUT ")]
    dns_index = next((i for i, line in enumerate(output_rules)
                      if "127.0.0.11" in line and "-j DROP" in line), None)
    established_index = next((i for i, line in enumerate(output_rules)
                              if "--ctstate" in line
                              and any(state in line for state in ("ESTABLISHED,RELATED", "RELATED,ESTABLISHED"))
                              and "-j ACCEPT" in line), None)
    if dns_index is None or established_index is None or dns_index >= established_index:
        raise HarnessError(f"DNS DROP must precede established OUTPUT accept: {output_rules!r}")
    return {"dns_drop_rule_index": dns_index, "established_accept_rule_index": established_index,
            "dns_drop_precedes_established_accept": True,
            "dns_drop_rule": output_rules[dns_index],
            "established_accept_rule": output_rules[established_index]}


def route_probe_target(cid: str, family: int, target: str, gateway: str) -> str:
    info = inspect(cid)
    labels = info["Config"].get("Labels") or {}
    if labels.get("b5.acceptance") is None or labels.get("scientist.platform/kind") != "worker":
        raise HarnessError("refusing route operation on a non-test worker")
    address = ipaddress.ip_address(target)
    via = ipaddress.ip_address(gateway)
    if address.version != family or via.version != family:
        raise HarnessError("route address family mismatch")
    pid = info["State"].get("Pid", 0)
    if not info["State"].get("Running") or pid <= 0:
        raise HarnessError("test worker is not running")
    route = f"{address}/32" if family == 4 else f"{address}/128"
    args = ("ip", "-4", "route", "replace", route, "via", str(via)) if family == 4 else (
        "ip", "-6", "route", "replace", route, "via", str(via), "dev", "eth0")
    vm(info["Id"], pid, *args)
    check = ("ip", "-4", "route", "get", str(address)) if family == 4 else (
        "ip", "-6", "route", "get", str(address))
    return vm(info["Id"], pid, *check)


def policy_packets(counters: str) -> int:
    match = re.search(r"policy DROP (\d+) packets", counters)
    if not match:
        raise HarnessError(f"could not read DROP-policy counters: {counters.splitlines()[:2]!r}")
    return int(match.group(1))


def dns_drop_packets(counters: str) -> int:
    for line in counters.splitlines():
        if "127.0.0.11" in line and "DROP" in line:
            fields = line.split()
            if len(fields) >= 2 and fields[0].isdigit():
                return int(fields[0])
    return 0


def fs_exhaust(cid: str) -> dict:
    code = r'''
import errno, json, os
path='/workspace/.b5-exhaust'; fd=os.open(path, os.O_CREAT|os.O_WRONLY|os.O_TRUNC, 0o600)
block=b'x'*(1024*1024); total=0
try:
    while True:
        n=os.write(fd, block); total+=n
except OSError as exc:
    if exc.errno != errno.ENOSPC: raise
    print(json.dumps({'bytes_before_enospc': total, 'errno': exc.errno}))
finally:
    os.close(fd)
'''
    return json.loads(exec_python(cid, code, timeout=60))



def observe_cpu_throttling(cid: str) -> dict:
    code = r"""
import json, subprocess, sys, time
def stat(): return {k:int(v) for k,v in (line.split() for line in open('/sys/fs/cgroup/cpu.stat'))}
limit=open('/sys/fs/cgroup/cpu.max').read().strip(); before=stat()
children=[subprocess.Popen([sys.executable, '-c', 'while True: pass']) for _ in range(2)]
time.sleep(1.5)
for child in children: child.terminate()
for child in children: child.wait(timeout=5)
after=stat()
print(json.dumps({'cpu_max':limit, 'nr_throttled_delta':after.get('nr_throttled',0)-before.get('nr_throttled',0),
                  'throttled_usec_delta':after.get('throttled_usec',0)-before.get('throttled_usec',0)}))
"""
    return json.loads(exec_python(cid, code, timeout=30))


def exhaust_pids(cid: str) -> dict:
    # One worker only; children sleep without allocating or doing CPU work.
    code = r"""
import errno, json, os, signal, time
maximum=open('/sys/fs/cgroup/pids.max').read().strip(); children=[]; eagain=False
try:
    for _ in range(140):
        try:
            pid=os.fork()
            if pid == 0:
                time.sleep(60); os._exit(0)
            children.append(pid)
        except OSError as exc:
            if exc.errno != errno.EAGAIN: raise
            eagain=True; break
finally:
    for pid in children:
        try: os.kill(pid, signal.SIGKILL)
        except ProcessLookupError: pass
    for pid in children:
        try: os.waitpid(pid, 0)
        except ChildProcessError: pass
print(json.dumps({'pids_max':maximum, 'children_before_eagain':len(children), 'eagain':eagain}))
"""
    return json.loads(exec_python(cid, code, timeout=45))


def main() -> int:
    if os.environ.get("DOCKER_CONTEXT") not in (None, "", CONTEXT):
        raise HarnessError(f"DOCKER_CONTEXT must not redirect the harness: {os.environ['DOCKER_CONTEXT']}")
    context = docker("context", "show")
    if context != CONTEXT:
        raise HarnessError(f"unexpected Docker context: {context}")
    version = docker("version", "--format", "{{.Server.Version}}")
    engine_id = docker("info", "--format", "{{.ID}}")
    if version != ENGINE_VERSION:
        raise HarnessError(f"expected owned test-engine {ENGINE_VERSION}, got {version}")
    image_id = docker("image", "inspect", IMAGE, "--format", "{{.Id}}")
    if image_id != EXPECTED_IMAGE_ID:
        raise HarnessError(f"expected final worker candidate image {EXPECTED_IMAGE_ID}, got {image_id}")

    same_project_id = uuid.uuid4()
    project_ids = [same_project_id, same_project_id, uuid.uuid4()]
    run_ids = [uuid.uuid4() for _ in range(3)]
    if project_ids[0] != project_ids[1] or project_ids[2] in project_ids[:2]:
        raise HarnessError("the replay must contain two runs in one project and one run in another")
    memory_evidence_path = CFG.evidence / "b5-memory-acceptance.json"
    memory_evidence = json.loads(memory_evidence_path.read_text(encoding="utf-8"))
    if (memory_evidence.get("image") != EXPECTED_IMAGE_ID
            or memory_evidence.get("context") != context
            or memory_evidence.get("engine_id") != engine_id
            or memory_evidence.get("memory_max") != 1_073_741_824
            or memory_evidence.get("swap_max") != 0
            or memory_evidence.get("oom_killed") is not True
            or memory_evidence.get("exit_code") != 137
        or memory_evidence.get("exact_cleanup") is not True
        or memory_evidence.get("whole_b5_pass") is not False
        or memory_evidence.get("paid_calls") != 0):
        raise HarnessError("separate hard-memory evidence does not match the final image/context")
    owned_containers: list[str] = []
    owned_networks: list[str] = []
    report: dict = {
        "status": "running",
        "scope": "pinned final worker candidate containment exercise with test entrypoint override; not application-startup acceptance",
        "docker_context": context, "colima_profile": PROFILE, "engine_id": engine_id,
        "engine_version": version,
        "worker_image": IMAGE, "worker_image_id": image_id,
        "project_ids": [str(project_id) for project_id in project_ids],
        "run_ids": [str(run_id) for run_id in run_ids], "runs": [],
        "separate_hard_memory_evidence": {"file": str(memory_evidence_path),
                                           "scope": memory_evidence.get("scope"),
                                           "image": memory_evidence["image"],
            "historical_only": False,
                                           "matches_final_worker_image": memory_evidence["image"] == image_id,
                                           "interpretation": "separate scoped OOM proof applies to final image; it is not whole-B5 acceptance",
                                           "memory_max": memory_evidence["memory_max"],
                                           "swap_max": memory_evidence["swap_max"],
                                           "oom_killed": memory_evidence["oom_killed"],
                                           "exit_code": memory_evidence["exit_code"],
            "exact_cleanup": memory_evidence["exact_cleanup"],
            "whole_b5_pass": memory_evidence["whole_b5_pass"],
            "paid_calls": memory_evidence["paid_calls"]},
        "scenario": {"runs_0_and_1_share_project": project_ids[0] == project_ids[1],
                     "run_2_uses_another_project": project_ids[2] not in project_ids[:2]},
        "proved": ["same-network IPv4 sentinel and broker controls before firewall",
                   "same-network IPv6 sentinel control and broker GET/POST controls",
                   "IPv4/IPv6/DNS firewall drop counters after enforcement",
                   "cross-run GET and POST denial at attempted target workers",
                   "gateway, metadata, broker wrong-port, external IPv4/IPv6 and IPv6 DNS denied by worker OUTPUT filter",
            "1 CPU/1GiB memory/128 PID caps configured; 64MiB workspace ENOSPC",
            "separate 1GiB hard-memory OOM proof on this final image with zero swap",
                   "CPU throttling and PID exhaustion observed on one worker; no OOM kill",
                   "no provider/DB/storage environment, host mounts, Unix socket access or published listeners"],
        "historical_bootstrap_evidence": {"file": "b5-worker-bootstrap-acceptance.json",
            "status": "pass", "image_pin": HISTORICAL_BOOTSTRAP_IMAGE,
                                          "historical_only": True,
                                          "interpretation": "previously accepted bootstrap proof; not proof of this final image"},
        "not_proved": ["application entrypoint/bootstrap/readiness behavior for this final image",
                       "host services beyond gateway address",
                       "successful external IPv6 reachability; probes are stopped at OUTPUT filter"],
    }
    sentinel_cids: list[str] = []
    broker_cids: list[str] = []
    try:
        for i, run_id in enumerate(run_ids):
            network, v4, v6 = create_network(run_id, project_ids[i], str(i), owned_networks)
            sentinel_cid, sentinel_v4, sentinel_v6 = create_sentinel(
                run_id, project_ids[i], i, network, v4, v6, owned_containers)
            assert_project_binding(sentinel_cid, run_id, project_ids[i])
            sentinel_cids.append(sentinel_cid)
            broker_cid, broker_ip = create_broker(
                run_id, project_ids[i], i, network, v4, owned_containers)
            assert_project_binding(broker_cid, run_id, project_ids[i])
            broker_cids.append(broker_cid)
            worker_cid = create_worker(run_id, project_ids[i], i, network, v4, v6,
                                       owned_containers)
            assert_project_binding(worker_cid, run_id, project_ids[i])
            # Readiness gate: reachable from the worker namespace before firewall installation.
            deadline = time.monotonic() + 12
            while True:
                try:
                    control = probe(worker_cid, {"sentinel_v4": (sentinel_v4, 8124),
                                                  "sentinel_v6": (sentinel_v6, 8124),
                                                  "broker": (broker_ip, 8123)})
                    if (not control["dns_control"]["resolved"]
                            or sentinel_v4 not in control["dns_control"].get("addresses", [])):
                        raise HarnessError(f"same-network Docker DNS control failed for run {i}: {control['dns_control']}")
                    control_targets = control["targets"]
                    positives = (control_targets["sentinel_v4"], control_targets["sentinel_v6"],
                                 control_targets["broker"])
                    if all(methods["get"]["connected"] and methods["post"]["connected"]
                           for methods in positives):
                        break
                except (subprocess.TimeoutExpired, HarnessError, json.JSONDecodeError):
                    pass
                if time.monotonic() >= deadline:
                    raise HarnessError(f"same-network sentinel control failed for run {i}: {control!r}")
                time.sleep(0.2)
            # Reset server state after control so any later accepted request is observable.
            docker("exec", sentinel_cid, "/opt/python/bin/python3.14", "-c",
                   "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8124/reset')")
            docker("exec", broker_cid, "/opt/python/bin/python3.14", "-c",
                   "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8123/reset')")
            # Capture counters at zero after policy install; tests then require observed drops.
            install_policy(worker_cid, str(v4.network_address + 2))
            pre_v4 = output_counters(worker_cid, "iptables")
            pre_v6 = output_counters(worker_cid, "ip6tables")
            rules = dns_rule_order(worker_cid)
            report["runs"].append({"index": i, "project_id": str(project_ids[i]),
                                   "run_id": str(run_id), "network": network, "subnet_v4": str(v4),
                                   "subnet_v6": str(v6), "sentinel": sentinel_v4,
                                   "sentinel_v6": sentinel_v6, "sentinel_container": sentinel_cid,
                                   "broker": broker_ip, "broker_container": broker_cid,
                                   "worker_id": worker_cid, "pre_policy_controls": control,
                                   "dns_rule_order": rules,
                                   "pre_probe_v4_drop_packets": policy_packets(pre_v4),
                                   "pre_probe_v6_drop_packets": policy_packets(pre_v6),
                                   "pre_probe_dns_drop_packets": dns_drop_packets(pre_v4)})

        # Exercise own-network and cross-run private GET/POST attempts from each worker.
        for i, item in enumerate(report["runs"]):
            other = report["runs"][(i + 1) % 3]
            # Explicit routes ensure foreign .4 requests reach the worker's
            # egress filter instead of failing at route lookup first.
            route_probe_target(item["worker_id"], 4, other["sentinel"],
                               str(ipaddress.ip_network(item["subnet_v4"]).network_address + 1))
            route_probe_target(item["worker_id"], 6, other["sentinel_v6"],
                               str(ipaddress.ip_network(item["subnet_v6"]).network_address + 1))
            for target in ("169.254.169.254", "1.1.1.1"):
                route_probe_target(item["worker_id"], 4, target,
                                   str(ipaddress.ip_network(item["subnet_v4"]).network_address + 1))
            route_probe_target(item["worker_id"], 6, "2606:4700:4700::1111",
                               str(ipaddress.ip_network(item["subnet_v6"]).network_address + 1))
            targets = {"own_v4": (item["sentinel"], 8124), "own_v6": (item["sentinel_v6"], 8124),
                       "crossrun_v4": (other["sentinel"], 8124), "crossrun_v6": (other["sentinel_v6"], 8124),
                       "host_gateway": (str(ipaddress.ip_network(item["subnet_v4"]).network_address + 1), 8124),
                       "metadata": ("169.254.169.254", 80), "broker_wrong_port": (item["broker"], 8124),
                       "external_v4": ("1.1.1.1", 443),
                       "external_v6": ("2606:4700:4700::1111", 443),
                       "broker_allowed": (item["broker"], 8123)}
            result = probe(item["worker_id"], targets, full=True)
            if result["dns"]["resolved"]:
                raise HarnessError(f"DNS escaped firewall for run {i}: {result['dns']}")
            if not result["dns_v6"]["attempted"] or result["native_listeners"]:
                raise HarnessError(f"IPv6 DNS query or listener probe invalid for run {i}: {result}")
            if any(result["unix_sockets"].values()):
                raise HarnessError(f"worker could access a host service socket in run {i}")
            for name, methods in result["targets"].items():
                if name == "broker_allowed":
                    continue
                if any(response["connected"] for response in methods.values()):
                    raise HarnessError(f"firewall allowed {name} traffic for run {i}: {methods}")
            v4_counts = output_counters(item["worker_id"], "iptables")
            v6_counts = output_counters(item["worker_id"], "ip6tables")
            delta_v4 = policy_packets(v4_counts) - item["pre_probe_v4_drop_packets"]
            delta_v6 = policy_packets(v6_counts) - item["pre_probe_v6_drop_packets"]
            dns_hits = dns_drop_packets(v4_counts) - item["pre_probe_dns_drop_packets"]
            if delta_v4 < 10 or delta_v6 < 6 or dns_hits < 1 or result["dns_v6"]["response_received"]:
                raise HarnessError(f"DROP counters did not prove all denied families for run {i}: "
                                   f"IPv4={delta_v4}, IPv6={delta_v6}, DNS={dns_hits}")
            broker_control = result["targets"]["broker_allowed"]
            if not broker_control["get"]["connected"] or not broker_control["post"]["connected"]:
                raise HarnessError(f"permitted broker IP:port GET/POST failed for run {i}")
            if result["targets"]["broker_wrong_port"]["get"]["connected"]:
                raise HarnessError(f"broker wrong-port egress was allowed for run {i}")
            broker_state = json.loads(exec_python(item["broker_container"],
                                                  "from pathlib import Path; print(Path('/tmp/accepted.json').read_text())"))
            if broker_state["writes"] != ["probe-write"]:
                raise HarnessError(f"allowed broker POST did not reach broker for run {i}: {broker_state}")
            cpu_observation = observe_cpu_throttling(item["worker_id"]) if i == 0 else None
            pid_observation = exhaust_pids(item["worker_id"]) if i == 0 else None
            if i == 0 and (cpu_observation["cpu_max"] != "100000 100000"
                           or cpu_observation["nr_throttled_delta"] < 1
                           or pid_observation["pids_max"] != "128"
                           or not pid_observation["eagain"]
                           or pid_observation["children_before_eagain"] < 100):
                raise HarnessError(f"CPU/PID enforcement observation failed: {cpu_observation}, {pid_observation}")
            exhaustion = fs_exhaust(item["worker_id"])
            if exhaustion["errno"] != 28 or exhaustion["bytes_before_enospc"] < 60 * 1024 * 1024:
                raise HarnessError(f"workspace did not exhaust at its tmpfs limit: {exhaustion}")
            info = inspect(item["worker_id"])
            host = info["HostConfig"]
            tmpfs = host.get("Tmpfs") or {}
            state = info["State"]
            env_names = [entry.partition("=")[0] for entry in info["Config"].get("Env", [])]
            sensitive_env = [name for name in env_names if re.search(
                r"SECRET|TOKEN|PASSWORD|CREDENTIAL|API_KEY|OPENAI|ANTHROPIC|AWS_|S3_|POSTGRES|DATABASE|DB_",
                name, re.IGNORECASE)]
            mounts = info.get("Mounts", [])
            non_tmpfs_mounts = [mount.get("Type") for mount in mounts if mount.get("Type") != "tmpfs"]
            port_bindings = host.get("PortBindings") or {}
            exposed_ports = info["Config"].get("ExposedPorts") or {}
            published_ports = info.get("NetworkSettings", {}).get("Ports") or {}
            if (host.get("NanoCpus") != 1_000_000_000 or host.get("Memory") != 1_073_741_824
                    or host.get("MemorySwap") != 1_073_741_824 or host.get("PidsLimit") != 128
                    or "/workspace" not in tmpfs or "size=67108864" not in tmpfs["/workspace"]
                    or not host.get("ReadonlyRootfs") or state.get("OOMKilled")
                    or host.get("Binds") or non_tmpfs_mounts or port_bindings or exposed_ports or published_ports
                    or sensitive_env):
                raise HarnessError(f"resource cap or OOM-free assertion failed for run {i}")
            sentinel = report["runs"][other["index"]]
            state_json = exec_python(other["sentinel_container"],
                                     "from pathlib import Path; print(Path('/tmp/accepted.json').read_text())")
            sentinel_state = json.loads(state_json)
            if sentinel_state["writes"]:
                raise HarnessError(f"cross-run write reached sentinel {other['index']}: {sentinel_state}")
            item.update({"probe_summary": {"dns_resolved": result["dns"]["resolved"],
                                             "dns_v6": result["dns_v6"],
                                             "listening_sockets": result["listening_sockets"],
                                             "native_listeners": result["native_listeners"],
                                             "unix_sockets_connected": [path for path, connected in
                                                                         result["unix_sockets"].items() if connected],
                                             "target_methods_connected": {
                                                 name: {method: response["connected"]
                                                       for method, response in methods.items()}
                                                 for name, methods in result["targets"].items()}},
                         "cpu_observation": cpu_observation,
                         "pid_observation": pid_observation,
                         "post_probe_ipv4_drop_packets": delta_v4,
                         "post_probe_ipv6_drop_packets": delta_v6,
                         "post_probe_dns_drop_packets": dns_hits,
                         "workspace_exhaustion": exhaustion,
                          "limits": {"nano_cpus": host["NanoCpus"], "memory_bytes": host["Memory"],
                                    "memory_swap_bytes": host["MemorySwap"], "pids_limit": host["PidsLimit"],
                                    "readonly_rootfs": host["ReadonlyRootfs"],
                                    "oom_killed": state["OOMKilled"], "host_mounts": non_tmpfs_mounts,
                                    "sensitive_env_names": sensitive_env, "exposed_ports": list(exposed_ports),
                                     "port_bindings": port_bindings},
                          "allowed_broker_writes": broker_state["writes"],
                          "crossrun_sentinel_writes": sentinel_state["writes"]})
        report["status"] = "pass"
        return 0
    except Exception as exc:
        report["status"] = "fail"
        report["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        # Remove only exact IDs/names created from this run ID; verify each absence.
        body_passed = report.get("status") == "pass"
        cleanup_errors = []
        removed_containers = []
        removed_networks = []
        for cid in reversed(owned_containers):
            try:
                docker("rm", "--force", cid, check=False)
                if not resource_is_absent("container", cid):
                    raise HarnessError("owned container remains after cleanup")
                removed_containers.append(cid)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"container {cid}: {type(cleanup_exc).__name__}: {cleanup_exc}")
        for network in reversed(owned_networks):
            try:
                docker("network", "rm", network, check=False)
                if not resource_is_absent("network", network):
                    raise HarnessError("owned network remains after cleanup")
                removed_networks.append(network)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"network {network}: {type(cleanup_exc).__name__}: {cleanup_exc}")
        report["cleanup"] = {
            "exact": not cleanup_errors and len(removed_containers) == len(owned_containers)
                     and len(removed_networks) == len(owned_networks),
            "containers_created": len(owned_containers),
            "containers_absent_verified": len(removed_containers),
            "networks_created": len(owned_networks),
            "networks_absent_verified": len(removed_networks),
            "errors": cleanup_errors,
        }
        if cleanup_errors:
            report["status"] = "fail"
            report.setdefault("failure", "exact cleanup verification failed")
        report_path = CFG.evidence / "b5-containment-acceptance.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"report_file": str(report_path), **report}, indent=2, sort_keys=True))
        if cleanup_errors and body_passed:
            raise HarnessError("exact cleanup verification failed; see acceptance report")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
