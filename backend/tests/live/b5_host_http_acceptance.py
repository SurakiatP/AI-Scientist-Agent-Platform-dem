#!/usr/bin/env python3
"""LV1: live proof of the trusted supervisor host over real loopback HTTP (no harness supervisor calls).

  uv run python backend/tests/live/b5_host_http_acceptance.py [host-happy|host-stop|host-extend|host-restart-retry]

Each case starts `scientist.host` as a SUBPROCESS (env without secrets), bootstraps the owner session over
http://127.0.0.1:<port>, and drives everything (project, session, run, plan, approve, stop, decisions, SSE)
through the REST API. The harness only READS the database/Docker for evidence, creates one synthetic credential
row (the only direct write, plus binding it to the REST-created project), and releases the stall fixture.
Test-only launcher (`--launch`): wraps host.compose to set broker._resolver -> 8.8.8.8 because
https://research.example is NXDOMAIN; the counter fixture (dispatch image) supplies the synthetic provider.
Exit 0 PASS, 1 FAIL, 77 NOT RUN (missing config, raised by b5_live_config before anything else runs).
"""
from __future__ import annotations

import sys

if __name__ == "__main__" and sys.argv[1:2] == ["--launch"]:  # host launcher: no harness imports, no secrets
    from scientist import broker, host

    _compose = host.compose

    def _compose_with_resolver(cfg):
        engine = _compose(cfg)
        broker._resolver = lambda name, port: ["8.8.8.8"]  # test-only: compose resets it to real DNS
        return engine

    host.compose = _compose_with_resolver
    raise SystemExit(host.main(["--config", sys.argv[2]]))

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import httpx

import b5_matrix_common as c  # noqa: E402  (exits 77 NOT RUN when config is missing)
from sqlalchemy.engine import make_url  # noqa: E402
from scientist.contracts import Principal  # noqa: E402
from scientist.host import HostConfig  # noqa: E402

SECRET_FILES = ("database_url", "broker_capability_key", "master_key", "s3_access_key", "s3_secret_key")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
TOKENISH_RE = re.compile(r"[A-Za-z0-9_+=-]{32,}")
STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
CASES = ("host-happy", "host-stop", "host-extend", "host-restart-retry")


def need(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError(message)


def wait_for(fn, what: str, timeout: float, step: float = 0.5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(step)
    raise TimeoutError(what)


def call(api: httpx.Client, method: str, path: str, ok=(200, 201), **kw) -> dict:
    resp = api.request(method, path, **kw)
    if resp.status_code not in ok:
        code = resp.json().get("code") if resp.headers.get("content-type", "").startswith("application/json") else "?"
        raise RuntimeError(f"{method} {path.split('?')[0]} -> {resp.status_code} {code}")
    return resp.json() if resp.content else {}


HALT = threading.Event()
GATE = threading.Lock()  # makes the monitor's check-then-interrupt atomic against HALT.set()


def headroom_monitor() -> None:
    """Same rule as c.actor: a storage-headroom breach interrupts the main thread; cleanup then runs uninterrupted."""
    while not HALT.wait(30):
        try:
            c.guard_storage_headroom()
        except Exception:
            with GATE:
                if not HALT.is_set():
                    os.kill(os.getpid(), signal.SIGINT)
            return


def forbid_supervisor() -> None:
    """The harness must never claim/start/recover/stop: any such call in THIS process raises."""
    def forbidden(*_a, **_k):
        raise RuntimeError("harness supervisor call forbidden: only the host subprocess supervises")
    for name in ("claim", "start", "recover", "stop"):
        setattr(c.supervisor, name, forbidden)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------- host subprocess
class HostProc:
    def __init__(self, case: str, root: Path, provider_id: UUID, dispatch_image: str, poll: float):
        self.case, self.n, self.proc, self.provider_id = case, 0, None, provider_id
        self.dir, self.logs, self.tokens, self.secrets_seen = root / case, [], [], []
        self.dir.mkdir(mode=0o700)
        self.state = self.dir / "state"
        self.state.mkdir(mode=0o700)
        os.chmod(self.dir, 0o700), os.chmod(self.state, 0o700)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.destinations = {str(provider_id): "https://research.example"}
        pins = c.runtime_pins()
        self.cfg_path = self.dir / "host.json"
        url = make_url(c.DB_URL)  # product rule: host.json URL carries no username; libpq reads PGUSER
        host_url = url._replace(username=None).render_as_string(hide_password=False)  # URL.set() ignores None
        HostConfig._database(host_url)  # offline self-check: the host would refuse anything else (rc=2)
        self.cfg = {
            "schema_version": 1, "database_url": host_url, "listen_port": self.port,
            "expected_engine_id": c.EXPECTED_ENGINE_ID, "worker_image": c.CFG.worker_image, "dispatch_image": dispatch_image,
            "service_network": "scientist-b5-services-test", "skills_digest": pins["skills_digest"],
            "environment_digest": pins["environment_digest"], "s3_endpoint": c.CFG.s3_endpoint, "bucket": c.BUCKET,
            "secrets_dir": str(c.PRIVATE), "state_dir": str(self.state), "max_active": 3, "poll_seconds": poll,
            "provider_destinations": self.destinations}
        # allowlist; the owned Docker context is hardcoded in scientist.supervisor, so no DOCKER_CONTEXT/DOCKER_HOST
        keep = re.compile(r"^(PATH|HOME|LANG|LC_.*|TMPDIR|VIRTUAL_ENV|USER|DOCKER_CONFIG|COLIMA_HOME|LIMA_HOME|XDG_CONFIG_HOME)$")
        self.env = {k: v for k, v in os.environ.items() if keep.match(k)}
        if url.username:
            self.env["PGUSER"] = url.username
        self.env["SCIENTIST_PROVIDER_DESTINATIONS"] = json.dumps(self.destinations)  # identical to host.json

    def start(self, poll: float | None = None) -> httpx.Client:
        self.n += 1
        if poll is not None:
            self.cfg["poll_seconds"] = poll
        self.cfg_path.unlink(missing_ok=True)
        self.cfg_path.write_text(json.dumps(self.cfg, sort_keys=True))
        self.cfg_path.chmod(0o600)
        (self.state / "owner-bootstrap.url").unlink(missing_ok=True)
        out, err = (c.SECURITY / f"host-{self.case}-{self.n}-{name}.log" for name in ("stdout", "stderr"))
        self.logs += [out, err]
        with open(out, "wb") as o, open(err, "wb") as e:
            self.proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--launch", str(self.cfg_path)],
                                         env=self.env, stdout=o, stderr=e, cwd=c.ROOT)

        def ready():
            need(self.proc.poll() is None, f"host exited early rc={self.proc.returncode}")
            if not (self.state / "owner-bootstrap.url").exists():
                return None
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
            except OSError:
                return None
            return True
        wait_for(ready, "host did not publish bootstrap URL and listen", 180, 0.5)
        token = (self.state / "owner-bootstrap.url").read_text().strip().split("#bootstrap=", 1)[1]
        self.tokens.append(token)
        api = httpx.Client(base_url=self.base, headers={"origin": self.base}, timeout=30)
        boot = api.post("/api/v1/bootstrap", json={"token": token})
        need(boot.status_code == 200, f"bootstrap -> {boot.status_code}")
        csrf = boot.json()["csrf_token"]
        api.headers["x-csrf-token"] = csrf
        self.secrets_seen += [csrf, api.cookies.get("owner_session") or ""]
        return api

    def stop(self) -> int | None:
        if self.proc is None or self.proc.poll() is not None:
            return None if self.proc is None else self.proc.returncode
        self.proc.send_signal(signal.SIGTERM)
        try:
            return self.proc.wait(150)
        except subprocess.TimeoutExpired:
            self.proc.kill()  # fail closed: caller sees rc != 0
            return self.proc.wait(10)

    def scan_logs(self) -> dict:
        """Compare in memory only; never print or store matched values. Booleans/counts only."""
        text = "".join(p.read_text(errors="replace") for p in self.logs)
        secrets = [(c.PRIVATE / n).read_bytes().strip().decode(errors="replace") for n in SECRET_FILES]
        needles = [v for v in (*self.tokens, *self.secrets_seen, *secrets) if len(v) >= 8]
        words = [w for w in UUID_RE.sub("", text).split() if "/" not in w]  # file paths are not secrets
        tokenish = [m for w in words for m in TOKENISH_RE.findall(w) if not re.fullmatch(r"[0-9a-f]+", m)]
        return {"log_files": len(self.logs), "known_secret_in_logs": any(v in text for v in needles),
                "tokenish_strings": len(tokenish)}


def sse_reader(base: str, cookies, run_id: UUID, out: dict) -> None:
    try:
        with httpx.Client(base_url=base, cookies=cookies, timeout=httpx.Timeout(10, read=40)) as cl:
            with cl.stream("GET", f"/api/v1/runs/{run_id}/events", headers={"accept": "text/event-stream"}) as resp:
                out["status"], kind = resp.status_code, None
                for line in resp.iter_lines():
                    if line.startswith("event: "):
                        kind = line[7:]
                    elif line.startswith("data: "):
                        out["events"].append((kind, (json.loads(line[6:]).get("payload") or {}).get("state")))
    except Exception as exc:  # recorded; the case asserts on what was received
        out["error"] = type(exc).__name__


# ---------------------------------------------------------------- case scaffold
def labelled(h) -> set[str]:
    return set(h.docker("ps", "-aq", "--no-trunc", "--filter", "label=scientist.platform/run").split()) | \
        set(h.docker("network", "ls", "-q", "--filter", "label=scientist.platform/run").split())


def attached(h0, run_id: UUID, name: str):
    h = c.H(name)
    h.eng, h.engine_id = h0.eng, h0.engine_id
    return h.attach(run_id)


class Case:
    def __init__(self, name: str, h0, root: Path):
        self.name, self.h0, self.root, self.stage = name, h0, root, "init"
        self.host, self.api, self.runs, self.before = None, None, [], labelled(h0)

    def new_host(self, dispatch_image: str, poll: float) -> None:
        self.stage = "start_host"
        self.guard(None)  # before creating anything
        cred = c.secrets.save_secret  # one synthetic credential row per case (provider id for the host map)
        with c.session() as db:
            self.cred = cred(db, Principal(identity=uuid4(), kind="owner"), "synthetic fixture credential",
                             "synthetic-only-no-provider-access")
            db.commit()
        self.host = HostProc(self.name, self.root, self.cred, dispatch_image, poll)
        self.api = self.host.start()

    def guard(self, own: UUID | None) -> None:
        """The host claims ANY approved queued run and recovers ANY running one: refuse unless nothing else exists."""
        c.guard_no_foreign_running(own or uuid4())
        h = self.runs[0] if own and self.runs else self.h0
        h.guard_no_other_claimable()
        n = h.q("SELECT count(*) AS n FROM runs WHERE state IN ('running','recovering','stopping') "
                "AND id IS DISTINCT FROM CAST(:run AS uuid)")[0]["n"]
        need(n == 0, f"{n} other active run(s) exist; the host would recover them")

    def project(self) -> tuple[str, str]:
        proj = call(self.api, "POST", "/api/v1/projects", json={"name": f"B5 host {self.name}", "instructions": ""})["id"]
        sess = call(self.api, "POST", f"/api/v1/projects/{proj}/sessions", json={"title": self.name})["id"]
        with c.session() as db:
            db.execute(c.text("UPDATE credentials SET project_id=:p WHERE id=:c"), {"p": proj, "c": self.cred})
            db.commit()
        return proj, sess

    def run(self, sess: str, model: str, token_limit: int = 20_000, elapsed_ms: int = 600_000) -> tuple[UUID, int, str]:
        run = call(self.api, "POST", f"/api/v1/sessions/{sess}/runs", json={
            "submission_key": uuid4().hex, "question": c.QUESTION, "input_ids": [], "provider_id": str(self.cred),
            "model": "fixture"})
        rid = run["run_id"]
        snap = call(self.api, "GET", f"/api/v1/runs/{rid}/plan")["plan"]["input_snapshot_digest"]
        plan = {"input_snapshot_digest": snap, "provider_id": str(self.cred), "model": model, "stages": ["synthesis"],
                "allowed_ops": ["llm"], "data_recipients": ["https://research.example"], "packages": [],
                "token_limit": token_limit, "elapsed_limit_ms": elapsed_ms}
        run = call(self.api, "PATCH", f"/api/v1/runs/{rid}/plan", json={"expected_revision": run["revision"], "plan": plan})
        self.runs.append(attached(self.h0, UUID(rid), f"{self.name}-{len(self.runs)}"))
        return UUID(rid), run["revision"], run["plan_digest"]

    def approve(self, rid: UUID, revision: int, digest: str) -> None:
        call(self.api, "POST", f"/api/v1/runs/{rid}/approve", json={"expected_revision": revision, "plan_digest": digest})

    def get(self, rid: UUID) -> dict:
        return call(self.api, "GET", f"/api/v1/runs/{rid}")

    def until(self, rid: UUID, pred, what: str, timeout: float = 240):
        return wait_for(lambda: (lambda v: v if pred(v) else None)(self.get(rid)), what, timeout)

    def finish(self, expect_rc: int = 0) -> dict:
        """Stop the host, scan its logs, exact-clean this case's runs, prove no new labelled leftovers."""
        self.stage = "finish"
        rc = self.host.stop()
        need(rc == expect_rc, f"host exit code {rc} != {expect_rc}")
        logs = self.host.scan_logs()
        need(not logs["known_secret_in_logs"] and not logs["tokenish_strings"], "host logs contain secret-like material")
        cleanup = {str(h.run_id): h.exact_cleanup() for h in self.runs}
        leftovers = labelled(self.h0) - self.before
        need(not leftovers, f"{len(leftovers)} new labelled container/network resource(s) remain")
        return {"host_exit_code": rc, "host_logs": logs, "cleanup": cleanup, "new_leftovers": 0}

    def fail_cleanup(self) -> dict:
        """Best effort, exact run ids only: REST stop, stop host, exact cleanup. Never raises; leaves evidence."""
        out = {}
        try:
            if self.host and self.host.proc and self.host.proc.poll() is None and self.api:
                for h in self.runs:
                    try:
                        if self.get(h.run_id)["state"] not in c.TERMINAL:
                            self.api.post(f"/api/v1/runs/{h.run_id}/stop")
                    except Exception:
                        pass
            out["host_exit_code"] = self.host.stop() if self.host else None
        except Exception as exc:
            out["host_error"] = type(exc).__name__
        for h in self.runs:
            try:
                out[str(h.run_id)] = h.exact_cleanup()
            except Exception as exc:
                out[str(h.run_id)] = {"status": "UNPROVEN; operator review required", "error_type": type(exc).__name__}
        return out


def _safe_scan(proc) -> dict | None:
    """Failure records must survive a log-scan error; record only its type."""
    try:
        return proc.scan_logs() if proc else None
    except Exception as exc:  # noqa: BLE001
        return {"scan_error": type(exc).__name__}


@contextmanager
def case(name: str, h0, root: Path):
    x = Case(name, h0, root)
    try:
        yield x
    except BaseException as exc:
        with GATE:
            HALT.set()  # no further headroom interrupt may land inside cleanup
        record = {"schema_version": 1, "case": name, "status": "FAILED", "failed_at": x.stage,
                  "failure_type": type(exc).__name__, "run_ids": [str(h.run_id) for h in x.runs],
                  "cleanup": x.fail_cleanup(), "worker_image_digest": c.WORKER_IMAGE,
                  "host_logs": _safe_scan(x.host)}
        c.write_json(c.SECURITY / f"b5-matrix-{name}-failure.json", record)
        print(json.dumps({"status": "FAILED", "case": name, "failed_at": x.stage, "failure_type": type(exc).__name__}, sort_keys=True))
        raise
    else:
        shutil.rmtree(x.host.dir, ignore_errors=True)  # config/state only; evidence logs live in the evidence dir


def emit(name: str, counter: str, x: Case, proof: dict, extra: dict) -> None:
    c.emit(name, c.proof_doc(name, counter, proof, {"run_ids": [str(h.run_id) for h in x.runs], "host_port": x.host.port,
                                                    "host_logs": [p.name for p in x.host.logs], **extra}))


def decision_from_events(x: "Case", rid: UUID, reason: str) -> str:
    """decision_id of the latest decision.required event of this reason, read over HTTP (event-page)."""
    page = call(x.api, "GET", f"/api/v1/runs/{rid}/event-page?after=0&limit=200")["events"]
    ids = [e["payload"]["decision_id"] for e in page if e["kind"] == "decision.required" and e["payload"].get("reason") == reason]
    need(ids, f"no {reason} decision.required event over HTTP")
    return ids[-1]


def ops_key(h) -> list[tuple]:
    return [(o["operation_id"], o["state"], o["reserve_tokens"], o["usage_tokens"]) for o in h.ops()]


# ---------------------------------------------------------------- cases
def host_happy(h0, counter: str, root: Path) -> None:
    with case("host-happy", h0, root) as x:
        x.new_host(counter, 1.0)
        _, sess = x.project()
        rid, rev, digest = x.run(sess, "fixture")
        h = x.runs[0]
        sse = {"events": []}
        reader = threading.Thread(target=sse_reader, args=(x.host.base, x.api.cookies, rid, sse), daemon=True)
        reader.start()
        x.stage = "approve_and_wait"
        x.approve(rid, rev, digest)
        final = x.until(rid, lambda v: v["state"] in c.TERMINAL, "run did not reach a terminal state")
        need(final["state"] == "completed", f"run ended {final['state']}")
        reader.join(60)
        need(not reader.is_alive() and sse.get("status") == 200 and "error" not in sse, f"SSE stream did not end cleanly ({sse.get('error')})")
        need(("run.state", "completed") in sse["events"], "SSE did not deliver run.state completed")
        need(h.attempts() == 1 and len(h.ops()) == 1 and h.ops()[0]["state"] == "committed", "expected one committed attempt")
        run = h.run_row()
        need((run["usage_tokens"], run["reserved_tokens"]) == (2, 0), "usage/reservation wrong after completion")
        done = x.finish()
        emit("host-happy", counter, x, {
            "host_claimed_started_reaped_recovered": True, "harness_supervisor_calls": 0, "rest_state_completed": True,
            "sse_run_state_completed": True, "provider_attempts": 1, "executors_inactive_exact": True,
            "host_logs_clean": True}, {"sse_event_count": len(sse["events"]), **done})


def host_stop(h0, counter: str, root: Path) -> None:
    with case("host-stop", h0, root) as x:
        x.new_host(counter, 0.5)
        _, sess = x.project()
        rid, rev, digest = x.run(sess, "fixture-stall-db")
        h = x.runs[0]
        x.approve(rid, rev, digest)
        x.stage = "wait_for_stalled_attempt"
        wait_for(lambda: h.attempts() == 1 and any(o["state"] == "reserved" for o in h.ops()), "stall not observed", 180, 0.2)
        before, run_before = ops_key(h), h.run_row()
        x.stage = "stop"
        first = call(x.api, "POST", f"/api/v1/runs/{rid}/stop")
        stop_at = h.db_clock()
        need(first["state"] == "canceled", f"stop -> {first['state']}")
        ops1 = ops_key(h)
        need(len(ops1) == 1 and ops1[0][1] == "unknown" and ops1[0][0::2] == (before[0][0], before[0][2])
             and ops1[0][3] == before[0][3], "operation is not the unknown, same-reservation fenced operation")
        need(h.run_row()["reserved_tokens"] == run_before["reserved_tokens"] > 0, "reservation changed by stop")
        second = call(x.api, "POST", f"/api/v1/runs/{rid}/stop")
        need(second["state"] == "canceled" and ops_key(h) == ops1, "second stop was not idempotent")
        x.stage = "release_stall"
        with c.session() as db:  # a surviving executor would commit an effect now
            db.execute(c.text("CREATE TABLE IF NOT EXISTS b5_fixture_stall_release (run_id uuid PRIMARY KEY, released_at timestamptz NOT NULL DEFAULT now())"))
            db.execute(c.text("INSERT INTO b5_fixture_stall_release (run_id) VALUES (:r) ON CONFLICT DO NOTHING"), {"r": rid})
            db.commit()
        time.sleep(4)
        late = h.q("SELECT count(*) AS n FROM b5_fixture_provider_attempts WHERE run_id=:run AND attempted_at > :t", t=stop_at)[0]["n"]
        need(late == 0 and h.attempts() == 1 and ops_key(h) == ops1 and x.get(rid)["state"] == "canceled", "late effect after stop")
        done = x.finish()
        emit("host-stop", counter, x, {
            "stop_http_200_canceled": True, "second_stop_idempotent": True, "operation_state": "unknown",
            "reservation_unchanged": True, "late_effect_count": late, "provider_attempts": 1}, done)


def host_extend(h0, counter: str, root: Path) -> None:
    with case("host-extend", h0, root) as x:
        # poll_seconds=30: the loop waits one full period before its FIRST tick, so A and B (approved within
        # seconds of the start) must both be handled by that single tick; a second tick is >= 60 s away.
        x.new_host(counter, 30.0)
        _, sess = x.project()
        a = x.run(sess, "fixture", token_limit=0)
        b = x.run(sess, "fixture")
        ha, hb = x.runs
        x.stage = "approve_a_and_b"
        x.approve(*a)
        x.approve(*b)
        x.stage = "same_tick"
        ev = "SELECT occurred_at AS t FROM events WHERE run_id=:run AND kind='run.state' AND payload->>'state'=:s ORDER BY sequence LIMIT 1"
        wait_for(lambda: ha.q(ev, s="waiting_input") and hb.q(ev, s="running"), "A park / B running events", 90, 0.5)
        t_park, t_b = ha.q(ev, s="waiting_input")[0]["t"], hb.q(ev, s="running")[0]["t"]
        gap = abs((t_b - t_park).total_seconds())
        need(gap <= 5, f"B running and A park events {gap:.1f}s apart (not the same tick)")
        need(t_park < t_b, "B started before A parked: the re-poll branch was not exercised")
        run_a = ha.run_row()
        need((run_a["state"], run_a["waiting_reason"]) == ("waiting_input", "budget_exhausted") and ha.attempts() == 0
             and not ha.ops(), "A did not park at the budget ceiling without work")
        need(x.until(b[0], lambda v: v["state"] == "completed", "B did not complete")["state"] == "completed", "B failed")
        x.stage = "extend_a"
        a_view = x.get(a[0])
        event_decision = decision_from_events(x, a[0], "budget_exhausted")
        db_decision = str(run_a["budget_decision_id"])
        need(event_decision == db_decision, "budget decision id over HTTP differs from the DB")
        ext_body = {"decision_id": event_decision, "expected_revision": a_view["revision"],
                    "idempotency_key": "host-extend-1", "choice": "extend", "add_tokens": 20_000, "add_elapsed_ms": 600_000}
        lost = False
        try:  # lost response: the client gives up reading before the server answers
            with httpx.Client(base_url=x.host.base, headers=dict(x.api.headers), cookies=x.api.cookies,
                              timeout=httpx.Timeout(10, read=0.001)) as cl:
                cl.post(f"/api/v1/runs/{a[0]}/decisions", json=ext_body)
        except httpx.HTTPError:
            lost = True
        need(lost, "the lost-response send did not time out client-side")
        wait_for(lambda: ha.q("SELECT 1 FROM owner_decisions WHERE run_id=:run AND idempotency_key='host-extend-1' AND state='resolved'"),
                 "server never committed the lost-response extension", 30, 0.2)
        ext = call(x.api, "POST", f"/api/v1/runs/{a[0]}/decisions", json=ext_body)  # SAME key: replay, no second grant
        applied = ha.run_row()
        need(applied["token_limit"] == run_a["token_limit"] + 20_000 and applied["elapsed_limit_ms"] == run_a["elapsed_limit_ms"] + 600_000,
             "extension was not applied exactly once")
        need(ha.q("SELECT count(*) AS n FROM run_budget_extensions WHERE run_id=:run")[0]["n"] == 1, "more than one extension row")
        final = x.until(a[0], lambda v: v["state"] in c.TERMINAL, "A did not finish after extension", 300)
        need(final["state"] == "completed", f"A ended {final['state']}")
        need(ha.attempts() == 1 and hb.attempts() == 1, "provider attempts are not exactly one per run")
        need(ha.run_row()["usage_tokens"] == 2, "A usage wrong")
        done = x.finish()
        emit("host-extend", counter, x, {
            "budget_decision_pending": True, "same_tick_gap_s": round(gap, 2), "a_parked_before_b_started": True,
            "budget_decision_id_read_over_http": True, "extension_http_200": True, "lost_response_resend_applied_once": True,
            "host_requeued_and_completed_a": True, "provider_attempts_a": 1, "provider_attempts_b": 1},
            {"extend_response_state": ext["state"], "poll_seconds": 30, **done})


def host_restart_retry(h0, counter: str, root: Path) -> None:
    with case("host-restart-retry", h0, root) as x:
        x.new_host(counter, 1.0)
        _, sess = x.project()
        rid, rev, digest = x.run(sess, "fixture-stall-db")
        h = x.runs[0]
        x.approve(rid, rev, digest)
        x.stage = "wait_for_stalled_attempt"
        wait_for(lambda: h.attempts() == 1 and any(o["state"] == "reserved" for o in h.ops()), "stall not observed", 180, 0.2)
        before, run_before = ops_key(h), h.run_row()
        need(len(before) == 1 and run_before["reserved_tokens"] > 0 and run_before["generation"] == 1, "unexpected in-flight state")
        x.stage = "sigterm_host_1"
        rc1 = x.host.stop()  # the worker and its dispatch keep running; only the supervisor goes away
        need(rc1 == 0, f"host #1 exit {rc1}")
        need(h.run_row()["state"] == "running", "run left running state while the host was down")
        x.stage = "start_host_2"
        x.guard(rid)
        t2 = h.db_clock()  # DB clock just before host #2: a decision issued by its startup recovery is later
        x.api = x.host.start(poll=30.0)  # long poll: the retry bodies below cannot race a claim tick
        t_ready = h.db_clock()  # startup recovery ran before the URL was published; the first tick is 30 s later
        window = h.q("SELECT EXTRACT(EPOCH FROM (CAST(:t AS timestamptz) - min(attempted_at))) AS s FROM b5_fixture_provider_attempts "
                     "WHERE run_id=:run", t=t_ready)[0]["s"]
        need(window is not None and window < 19, f"stall window exceeded: {window}s from attempt to host #2 ready "
             "(fixture-stall-db self-releases after 20 s); not a product failure")
        x.stage = "recovery_issued_decision"
        view = x.get(rid)
        need((view["state"], view["waiting_reason"]) == ("waiting_input", "unknown_outcome"), "startup recovery did not park unknown_outcome")
        ops1 = ops_key(h)
        need(len(ops1) == 1 and ops1[0][1] == "unknown" and (ops1[0][0], ops1[0][2], ops1[0][3]) == (before[0][0], before[0][2], before[0][3]),
             "operation is not the unknown, same-reservation fenced operation")
        need(h.run_row()["reserved_tokens"] == run_before["reserved_tokens"], "reservation changed by recovery")
        rows = h.q("SELECT decision_id, operation_id, reason, issued_at > CAST(:t2 AS timestamptz) AS after_t2, "
                   "issued_at <= CAST(:t3 AS timestamptz) AS before_ready "
                   "FROM owner_decisions WHERE run_id=:run AND state='pending'", t2=t2, t3=t_ready)
        need(len(rows) == 1 and rows[0]["reason"] == "unknown_outcome" and rows[0]["operation_id"] == before[0][0]
             and rows[0]["after_t2"] and rows[0]["before_ready"], "pending decision was not issued by host #2's recovery")
        event_decision = decision_from_events(x, rid, "unknown_outcome")
        need(event_decision == str(rows[0]["decision_id"]), "decision id over HTTP differs from the DB row")
        x.stage = "release_stall"
        stop_at = h.db_clock()
        with c.session() as db:  # a surviving executor would commit an effect now
            db.execute(c.text("CREATE TABLE IF NOT EXISTS b5_fixture_stall_release (run_id uuid PRIMARY KEY, released_at timestamptz NOT NULL DEFAULT now())"))
            db.execute(c.text("INSERT INTO b5_fixture_stall_release (run_id) VALUES (:r) ON CONFLICT DO NOTHING"), {"r": rid})
            db.commit()
        time.sleep(4)
        late = h.q("SELECT count(*) AS n FROM b5_fixture_provider_attempts WHERE run_id=:run AND attempted_at > :t", t=stop_at)[0]["n"]
        need(late == 0 and h.attempts() == 1 and ops_key(h) == ops1 and x.get(rid)["state"] == "waiting_input", "late effect before any decision")
        x.stage = "owner_retry_http"
        body = {"decision_id": event_decision, "expected_revision": view["revision"], "idempotency_key": "host-retry-1", "choice": "retry"}
        headers, cookies = dict(x.api.headers), x.api.cookies
        barrier, results = threading.Barrier(4), [None] * 4

        def racer(i: int) -> None:
            with httpx.Client(base_url=x.host.base, headers=headers, cookies=cookies, timeout=30) as cl:
                barrier.wait(30)
                r = cl.post(f"/api/v1/runs/{rid}/decisions", json=body)
                results[i] = (r.status_code, r.json())
        threads = [threading.Thread(target=racer, args=(i,)) for i in range(4)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        need(all(r is not None and r[0] == 200 for r in results), f"concurrent D3 POST codes {[r and r[0] for r in results]}")
        need(all(r[1] == results[0][1] for r in results), "concurrent D3 POST bodies differ")
        conflict = x.api.post(f"/api/v1/runs/{rid}/decisions", json={**body, "choice": "stop"})
        need(conflict.status_code == 409 and conflict.json().get("code") == "idempotency_conflict", f"conflict -> {conflict.status_code}")
        x.stage = "retry_completes"
        final = x.until(rid, lambda v: v["state"] in c.TERMINAL, "retry did not finish", 300)
        need(final["state"] == "completed", f"run ended {final['state']}")
        ops = h.ops()
        by_id = {o["operation_id"]: o for o in ops}
        retry = [o for o in ops if o["operation_id"].startswith("retry-")]
        need(len(ops) == 2 and len(retry) == 1 and by_id[before[0][0]]["state"] == "unknown"
             and by_id[before[0][0]]["result"].get("retry_identity") == retry[0]["operation_id"]
             and retry[0]["state"] == "committed" and h.attempts() == 2, "owner retry did not produce exactly one bound extra attempt")
        need((by_id[before[0][0]]["reserve_tokens"], by_id[before[0][0]]["usage_tokens"]) == (before[0][2], before[0][3]),
             "the original operation's reservation or usage was not retained")
        run = h.run_row()
        need(run["usage_tokens"] == 2 and run["generation"] == 2, "usage/generation wrong after retry")
        need(not h.q("SELECT 1 FROM owner_decisions WHERE run_id=:run AND state='pending'"), "a decision is still pending")
        done = x.finish()
        emit("host-restart-retry", counter, x, {
            "decision_issued_by_host_2_recovery": True, "op_unknown_reservation_unchanged": True, "late_effect_count": late,
            "decision_id_read_over_http": True, "concurrent_post_codes": [r[0] for r in results], "concurrent_bodies_identical": True,
            "payload_conflict_code": 409, "retry_operations": 1,
            "provider_attempts": 2, "one_attempt_per_operation": True, "host_1_exit_code": rc1},
            {"reserved_tokens_after": run["reserved_tokens"], "generation": run["generation"], **done})


def main() -> int:
    names = sys.argv[1:] or list(CASES)
    need(all(n in CASES for n in names), f"unknown case; choose from {CASES}")
    counter = c.fixture_ref("counter")
    c.require_clean_repo()
    root = c.PRIVATE / f"host-{STAMP}"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    prev_sigint = signal.getsignal(signal.SIGINT)

    def _sigint(signum, frame):
        if HALT.is_set():
            return  # cleanup is running: a late interrupt must not land inside it
        (prev_sigint if callable(prev_sigint) else signal.default_int_handler)(signum, frame)
    signal.signal(signal.SIGINT, _sigint)
    try:
        h0 = c.H("host-http").setup(counter)
        threading.Thread(target=headroom_monitor, name="b5-storage-headroom", daemon=True).start()
        forbid_supervisor()
        for name in names:
            {"host-happy": host_happy, "host-stop": host_stop, "host-extend": host_extend,
             "host-restart-retry": host_restart_retry}[name](h0, counter, root)
        print(json.dumps({"status": "PASS", "cases": names}))
        return 0
    except BaseException as exc:
        print(json.dumps({"status": "FAIL", "failure_type": type(exc).__name__}))
        return 1
    finally:
        with GATE:
            HALT.set()
        signal.signal(signal.SIGINT, prev_sigint)
        if root.is_relative_to(c.PRIVATE) and not any(root.iterdir()):
            root.rmdir()  # a failed case leaves its directory for operator review


if __name__ == "__main__":
    sys.exit(main())
