#!/usr/bin/env python3
"""Shared helpers for the ignored B5 restart/race matrix actors. Synthetic providers only; no paid calls.

Nothing here prints or stores credentials, capabilities, object keys, payloads or raw logs. Actors write
sanitized proof JSON under the run evidence dir (archiving any previous file first). Owned engine only
(colima-scientist-platform-test); cleanup touches only the run's own exact-labelled executors and networks.
`python b5_matrix_common.py fresh ...` is the fresh-supervisor-process entry used by the restart actors.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import signal
import threading
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from b5_live_config import CFG  # noqa: E402

ROOT = CFG.root
PRIVATE = CFG.private_dir
SECURITY = CFG.evidence
ARCHIVE = SECURITY / "b5-matrix-proof-archive-20261004"
CTX = CFG.docker_context
DB_URL = CFG.database_url
BUCKET = "scientist-b5"
RUNTIME_COMMIT = CFG.runtime_commit
QUESTION = "Synthesize the approved synthetic fixture evidence."
SYNTHESIS = "Synthetic research synthesis with no paid call."  # counter/barrier fixture completion text
SYSTEM_PROMPT = "Return one concise evidence synthesis based only on the approved synthetic fixture."
EXPECTED_ENGINE_ID = CFG.expected_engine_id
CLEAN_PATHS = ("backend", "runtime")
EXACT_PROOF_SOURCES = {"owned-engine-exact-container", "owned-engine-generation-fence", "worker-launch-not-attempted"}  # last one: exact-shape branch only
TERMINAL = {"completed", "failed", "canceled", "rejected"}
PG_CONTAINER, MINIO_CONTAINER = "scientist-b5-postgres", "scientist-b5-minio"

os.environ["SCIENTIST_DATABASE_URL"] = DB_URL
os.environ["SCIENTIST_MASTER_KEY_FILE"] = str(PRIVATE / "master_key")  # path only; never read here
sys.path.insert(0, str(Path(__file__).resolve().parent))

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from scientist import broker, checkpoints, objects, secrets, supervisor  # noqa: E402
from scientist import db as scientist_db  # noqa: E402
from scientist.contracts import CheckpointManifest, ObjectRef, PlanSpec, Principal  # noqa: E402
from scientist.db import create_project, create_session, migrate, session  # noqa: E402
from scientist.dispatch_runtime import DispatchServiceConfig, DockerDispatchRuntime  # noqa: E402
from scientist.domain import approve_run, revise_plan, submit_run  # noqa: E402
from scientist.private_worker_api import RuntimePins, WorkerController  # noqa: E402
from scientist.runtime_contracts import (  # noqa: E402
    BootstrapMetadata, RuntimeContextV1, WorkspaceFile,
)
from scientist.supervisor import DockerWorkerEngine, WorkerBootstrap  # noqa: E402

WORKER_IMAGE = CFG.worker_image_id
SERVER_DIGEST = CFG.server_image_id


REPO: dict = {}


def require_clean_repo() -> dict:
    """Refuse to run actors against a dirty backend/ or runtime/; record the exact HEAD in every proof."""
    guard_storage_headroom()
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout.strip()
    dirty = git("status", "--porcelain", "--", *CLEAN_PATHS)
    if dirty:
        raise RuntimeError(f"backend/runtime is dirty ({len(dirty.splitlines())} paths); commit or stash before running actors")
    head = git("rev-parse", "HEAD")
    # The pinned -08 image was built from these exact sources; the host must match them.
    pinned = json.loads(CFG.server_source_hashes.read_text())
    pinned = pinned.get("files", pinned)
    for path, digest in pinned.items():
        if path.startswith("backend/src/") and hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"host source {path} differs from the pinned server image source")
    REPO.update(head=head, backend_runtime_clean=True, host_matches_pinned_image_source=True)
    return REPO


def fixture_ref(name: str, override: str | None = None) -> str:
    """Pinned `repo@sha256:...` for happy|counter|barrier (and checkpoint_fault once the parent pins it)."""
    if override:
        return override
    ref = CFG.fixture_images.get(name)
    if not ref:
        raise SystemExit(f"fixture '{name}' is not pinned; pass its image reference explicitly")
    return ref


def digest_of(ref: str) -> str:
    return ref.split("@", 1)[1]


def s3_client(fast: bool = False):
    cfg = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}) if fast else Config(
        connect_timeout=5, read_timeout=30, retries={"max_attempts": 2})
    return boto3.client(
        "s3", endpoint_url=CFG.s3_endpoint, region_name="us-east-1", config=cfg,
        aws_access_key_id=(PRIVATE / "s3_access_key").read_text().strip(),
        aws_secret_access_key=(PRIVATE / "s3_secret_key").read_text().strip())


def runtime_pins() -> dict[str, str]:
    manifest = json.loads((ROOT / "runtime/skills-manifest.json").read_text())
    return {"image_digest": WORKER_IMAGE, "skills_digest": manifest["manifest_sha256"],
            "environment_digest": hashlib.sha256((ROOT / "runtime/requirements.lock").read_bytes()).hexdigest()}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------- seeded bootstrap contexts
def _ws_file(path: str, data: bytes) -> dict:
    return {"path": path, "sha256": sha(data), "size": len(data), "data_base64": base64.b64encode(data).decode()}


_TODOS = {"todos": [{"id": "t1", "content": "Review synthetic evidence", "status": "in_progress"},
                    {"id": "t2", "content": "Draft synthesis", "status": "pending", "parent": "t1"}], "revision": 3}


def seed_compressed(data: dict) -> list[dict]:
    data["compacted_context"] = {"compression_count": 1, "previous_summary": "Synthetic earlier-evidence summary.",
                                 "summary_has_user_turn": False, "ineffective_compression_count": 0, "micro": {}}
    return []


def seed_todo(data: dict) -> list[dict]:
    data["todo"] = dict(_TODOS)  # Todo only: pending_assistant stays None
    return []


def seed_workspace(data: dict) -> list[dict]:
    file = _ws_file("notes/seed.txt", b"synthetic workspace note\n")
    data["workspace_manifest"] = [{k: file[k] for k in ("path", "sha256", "size")}]
    return [file]


def seed_todo_messages(data: dict) -> list[dict]:
    data["todo"] = dict(_TODOS)
    data["messages"] = [*data["messages"], {"role": "assistant", "content": "Working through the request.",
        "tool_calls": [{"id": "raw_call_1", "type": "function", "function": {"name": "todo_list", "arguments": "{}"}}]}]
    data["boundary"] = "model_committed"
    data["pending_assistant"] = {"turn_id": data["turn_id"], "message_index": 1, "next_tool_index": 0, "applied_tool_ids": []}
    return []


# todo_messages_carry_through: honest label; a seeded bootstrap cannot reach a before_tool checkpoint with a pending
# assistant (the worker completes the pending read-only todo_list call before its first checkpoint), so the proof is
# todo + assistant tool-call message carry-through, not pending_assistant restore.
SEEDS = {"compressed": seed_compressed, "todo_messages_carry_through": seed_todo_messages,
         "workspace": seed_workspace, "todo": seed_todo}


def class_fingerprint(context: bytes | dict, seeded_messages: int) -> str:
    """Hash of the fields a context class must preserve across restore (not generation/mappings/revision)."""
    ctx = RuntimeContextV1.model_validate_json(context) if isinstance(context, bytes) else RuntimeContextV1.model_validate(context)
    dump = ctx.model_dump(mode="json")
    return sha(canon({"compacted": dump["compacted_context"], "todos": dump["todo"]["todos"],
                      "messages": dump["messages"][:seeded_messages], "workspace": dump["workspace_manifest"]}).encode())


# ---------------------------------------------------------------- harness
class H:
    """One actor's view of one synthetic run on the owned engine."""

    def __init__(self, name: str, *, fast_s3: bool = False):
        self.name, self.run_id, self.seed, self.stage = name, None, None, "init"
        self.boot: dict[int, bytes] = {}
        self.s3 = s3_client(fast_s3)
        self.pins = runtime_pins()
        self.eng = None
        self.model = "fixture"
        self.seeded_messages = 1

    # -- setup
    def setup(self, *fixture_refs: str) -> "H":
        self.eng = DockerWorkerEngine()  # fixed owned context/profile
        self.engine_id = self.eng.engine_id()  # first Docker contact: must be the pinned owned engine
        if self.engine_id != EXPECTED_ENGINE_ID:
            raise RuntimeError("Docker engine identity differs from the pinned owned engine")
        try:
            self.s3.head_bucket(Bucket=BUCKET)
        except Exception as exc:  # create only on a genuine 404
            if getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise RuntimeError("synthetic MinIO bucket readiness failed") from None
            self.s3.create_bucket(Bucket=BUCKET)
        self.configure_storage()
        migrate()
        for ref in (WORKER_IMAGE, *fixture_refs):
            want = ref if ref.startswith("sha256:") else digest_of(ref)
            if self.docker("image", "inspect", ref, "--format", "{{.Id}}") != want:
                raise RuntimeError("image identity differs from its immutable pin")
        broker.configure(capability_key=(PRIVATE / "broker_capability_key").read_bytes().strip(),
                         provider_destinations={}, resolver=lambda host, port: ["8.8.8.8"])
        return self

    def configure_storage(self, **override: str) -> None:
        objects.configure(self.s3, bucket=BUCKET)
        pins = {**self.pins, **override}
        checkpoints.configure_trusted_pins(image_digest=pins["image_digest"], skills_digest=pins["skills_digest"],
                                           environment_digest=pins["environment_digest"], runtime_commit=RUNTIME_COMMIT)

    def docker(self, *args: str) -> str:
        return self.eng._docker(*args)

    # -- run creation / attach
    def _paths(self) -> None:
        self.config_path = PRIVATE / f"native-matrix-template-{self.run_id.hex}.json"
        self.launch_root = PRIVATE / f"native-matrix-launches-{self.run_id.hex}"

    def new_run(self, model: str = "fixture", token_limit: int = 20_000, elapsed_limit_ms: int = 600_000) -> UUID:
        self.model = model
        with session() as db:
            owner = Principal(identity=uuid4(), kind="owner")
            self.owner = owner
            project_id = create_project(db, f"B5 matrix {self.name}")
            chat = create_session(db, project_id, self.name)
            credential = secrets.save_secret(db, owner, "synthetic fixture credential", "synthetic-only-no-provider-access")
            db.execute(text("UPDATE credentials SET project_id=:p WHERE id=:c"), {"p": project_id, "c": credential})
            run = submit_run(db, owner, project_id, chat, uuid4().hex, QUESTION, [], credential, "fixture")
            snapshot = db.execute(text("SELECT digest FROM input_snapshots WHERE run_id=:r"), {"r": run.run_id}).scalar_one().strip()
            plan = PlanSpec(input_snapshot_digest=snapshot, provider_id=credential, model=model, stages=["synthesis"],
                            allowed_ops=["llm"], data_recipients=["https://research.example"], packages=[],
                            token_limit=token_limit, elapsed_limit_ms=elapsed_limit_ms)
            os.environ["SCIENTIST_PROVIDER_DESTINATIONS"] = '{"%s": "https://research.example"}' % credential  # D4: bind the plan recipient to this provider
            run = revise_plan(db, owner, run.run_id, run.revision, plan)
            approve_run(db, owner, run.run_id, run.revision, run.plan_digest)
            db.commit()
            self.run_id, self.project_id, self.provider_id = run.run_id, project_id, credential
        self._paths()
        template = {"schema_version": 1, "runtime_commit": RUNTIME_COMMIT, **self.pins,
                    "provider_destinations": {str(self.provider_id): "https://research.example"},
                    "secret_files": {n: n for n in ("database_url", "broker_capability_key", "master_key",
                                                    "s3_access_key", "s3_secret_key")}}
        self.config_path.write_text(json.dumps(template, sort_keys=True), encoding="utf-8")
        self.config_path.chmod(0o444)
        self.launch_root.mkdir(mode=0o700)
        return self.run_id

    def attach(self, run_id: UUID) -> "H":
        self.run_id = run_id
        self._paths()
        with session() as db:
            row = db.execute(text("SELECT project_id, revision FROM runs WHERE id=:r"), {"r": run_id}).one()
            self.project_id = row.project_id
            self.provider_id = broker._load_plan(db, run_id, row.revision).provider_id
        self.launch_root.mkdir(mode=0o700, exist_ok=True)
        return self

    # -- supervisor wiring
    def configure(self, image: str, label: str) -> None:
        service = DispatchServiceConfig(
            image=image, image_digest=digest_of(image), service_network="scientist-b5-services-test",
            config_path="/run/scientist/dispatch/config.json", secrets_dir="/run/scientist/secrets",
            host_config_file=str(self.config_path), host_secrets_dir=str(PRIVATE),
            launcher_dir=str(self.launch_root / label))
        dispatch = DockerDispatchRuntime(service, engine=self.eng)

        broker.configure(capability_key=(PRIVATE / "broker_capability_key").read_bytes().strip(),
                         provider_destinations={str(self.provider_id): "https://research.example"},
                         resolver=lambda host, port: ["8.8.8.8"])
        supervisor.configure(
            image=WORKER_IMAGE, image_digest=WORKER_IMAGE, broker_url="http://127.0.0.1:8123",
            broker_ip="172.29.32.2", broker_port=8123, runtime_commit=RUNTIME_COMMIT,
            skills_digest=self.pins["skills_digest"], environment_digest=self.pins["environment_digest"],
            bootstrap_factory=self._bootstrap,
            capability_factory=lambda db, run, gen: broker.issue_capability(db, run, gen, 300),
            dispatch=dispatch, engine=self.eng)
        # supervisor.configure registers the exact dispatch-inactivity proof; owner retry needs no direct broker wiring.
        if broker._dispatch_inactivity_proof is None:
            raise RuntimeError("supervisor.configure did not register the dispatch inactivity proof")

    def rest_decide(self, choice: str, key: str) -> dict:
        """Submit an owner decision through the real REST host (bootstrap, cookie, CSRF), as the browser does."""
        from secrets import token_urlsafe
        from fastapi.testclient import TestClient
        from scientist.app import create_app
        token = token_urlsafe(32)
        app = create_app(bootstrap_token=token)
        client = TestClient(app, client=("127.0.0.1", 12345))
        headers = {"host": "localhost", "origin": "http://localhost"}
        boot = client.post("/api/v1/bootstrap", headers=headers, json={"token": token})
        client.headers.update({**headers, "x-csrf-token": boot.json()["csrf_token"]})
        with session() as db:
            row = db.execute(text("""SELECT d.decision_id, r.revision FROM owner_decisions d JOIN runs r ON r.id = d.run_id
                                     WHERE d.run_id = :run AND d.state = 'pending'"""), {"run": self.run_id}).one()
        resp = client.post(f"/api/v1/runs/{self.run_id}/decisions", json={
            "decision_id": str(row.decision_id), "expected_revision": row.revision, "idempotency_key": key, "choice": choice})
        if resp.status_code != 200:
            raise RuntimeError(f"decision rejected: {resp.status_code} {resp.text}")
        return resp.json()

    def _bootstrap(self, db, run_id, generation) -> WorkerBootstrap:
        # Contract (supervisor.continuation_bootstrap): fresh context is lawful only with no checkpoint AND no operation rows.
        prior = db.execute(text("SELECT (SELECT count(*) FROM checkpoints WHERE run_id=:r) + "
                                "(SELECT count(*) FROM operations WHERE run_id=:r)"), {"r": run_id}).scalar_one()
        if generation == 1 or prior == 0:
            return self._fresh_context(db, run_id, generation)
        row = db.execute(text("SELECT revision FROM runs WHERE id=:r"), {"r": run_id}).one()
        plan = broker._load_plan(db, run_id, row.revision)
        controller = WorkerController(
            pins=RuntimePins(runtime_commit=RUNTIME_COMMIT, **self.pins),
            provider_destinations={plan.provider_id: "https://research.example"})
        boot = supervisor.continuation_bootstrap(db, run_id, generation, controller)  # fail-closed, never fresh
        self.boot[generation] = bytes(boot.context)
        return boot

    def _fresh_context(self, db, run_id, generation) -> WorkerBootstrap:
        row = db.execute(text("""SELECT r.project_id, r.revision, r.plan_digest, s.digest AS snapshot, p.plan
            FROM runs r JOIN input_snapshots s ON s.run_id=r.id AND s.project_id=r.project_id
            JOIN plan_revisions p ON p.run_id=r.id AND p.revision=r.revision
            WHERE r.id=:run AND r.generation=:generation"""), {"run": run_id, "generation": generation}).mappings().one()
        plan = PlanSpec.model_validate(row["plan"])
        stamp = time.time()
        data = {"schema_version": 1, "run_id": str(run_id), "project_id": str(row["project_id"]), "generation": generation,
                "revision": row["revision"], "input_snapshot_digest": row["snapshot"].strip(),
                "plan_digest": row["plan_digest"].strip(), "runtime_commit": RUNTIME_COMMIT, **self.pins,
                "provider_id": str(plan.provider_id), "provider_endpoint": "https://research.example", "model": plan.model,
                "plan": plan.model_dump(mode="json"), "turn_id": str(uuid4()),
                "system_prompt": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": QUESTION}],
                "native_message_metadata": [{"message_index": 0, "timestamp": stamp}],
                "current_turn_user_index": 0, "native_turn_timestamp": stamp,
                "todo": {"todos": [], "revision": 0}, "compacted_context": None, "boundary": "before_model",
                "pending_assistant": None, "operation_mappings": [], "operation_sequence": 0, "workspace_manifest": []}
        workspace = self.seed(data) if self.seed else []
        context = RuntimeContextV1.model_validate(data)
        self.seed_context = context.model_dump_json().encode()
        self.seeded_messages = len(context.messages)
        return WorkerBootstrap(context=self.seed_context, workspace=[WorkspaceFile.model_validate(f) for f in workspace],
                               metadata=BootstrapMetadata(schema_version=1, checkpoint_revision=0))

    # -- lifecycle helpers
    def guard_no_other_claimable(self) -> None:
        """supervisor.claim takes any approved queued run; refuse if one other than ours exists."""
        others = self.q("""SELECT r.id FROM runs r WHERE r.id IS DISTINCT FROM CAST(:run AS uuid) AND r.state='queued' AND r.cancel_requested=false
            AND r.plan_digest IS NOT NULL AND EXISTS (SELECT 1 FROM approvals a WHERE a.run_id=r.id AND a.revision=r.revision
            AND a.plan_digest=r.plan_digest)""")
        if others:
            raise RuntimeError(f"{len(others)} other approved queued run(s) exist; cancel/finalize them before this actor")

    def claim_one(self):
        self.guard_no_other_claimable()
        with session() as db:
            return supervisor.claim(db, max_active=3)

    def claim_start(self) -> tuple[int, str]:
        self.guard_no_other_claimable()
        with session() as db:
            claim = supervisor.claim(db, max_active=3)
            if claim is None or claim[0] != self.run_id:
                raise RuntimeError("run was not claimed")
            return claim[1], supervisor.start(db, self.run_id, claim[1])

    def ident(self, container: str) -> dict:
        raw = self.docker("inspect", "--format",
                          '{{.Id}}|{{.Image}}|{{.State.Status}}|{{.State.ExitCode}}|{{index .Config.Labels "scientist.platform/run"}}|'
                          '{{index .Config.Labels "scientist.platform/generation"}}|{{index .Config.Labels "scientist.platform/kind"}}', container)
        keys = ("container_id", "image_id", "status", "exit_code", "run_id", "generation", "kind")
        values = raw.split("|")
        if len(values) != len(keys):
            raise RuntimeError("container inspection shape differs")
        return dict(zip(keys, values))

    def kill(self, container: str) -> None:
        ident = self.ident(container)
        if ident["run_id"] != str(self.run_id) or ident["kind"] != "worker":
            raise RuntimeError("refusing to kill a container that is not this run's worker")
        self.docker("kill", container)
        if int(self.docker("wait", container)) != 137:
            raise RuntimeError("exact worker kill did not prove exit 137")

    def wait(self, container: str) -> int:
        return int(self.docker("wait", container))

    def recover(self):
        with session() as db:
            return supervisor.recover(db, self.run_id)

    # -- reads (read-only SQL)
    def q(self, sql: str, **params):
        with session() as db:
            return db.execute(text(sql), {"run": self.run_id, **params}).mappings().all()

    def run_row(self) -> dict:
        return dict(self.q("""SELECT state, waiting_reason, generation, revision, usage_tokens, reserved_tokens, token_limit,
            elapsed_limit_ms, elapsed_used_ms, elapsed_active_since, budget_decision_id, cancel_requested
            FROM runs WHERE id=:run""")[0])

    def ops(self) -> list[dict]:
        return [dict(r) for r in self.q("""SELECT operation_id, generation, kind, state, reserve_tokens, usage_tokens, result
            FROM operations WHERE run_id=:run ORDER BY created_at, operation_id""")]

    def executors(self) -> list[dict]:
        return [dict(r) for r in self.q("""SELECT generation, kind, state, container_id, engine_id, proof
            FROM runtime_executors WHERE run_id=:run ORDER BY generation, kind""")]

    def attempts(self) -> int:
        """Fixture-side durable provider attempts, independent of the application ledger."""
        if self.q("SELECT to_regclass('b5_fixture_provider_attempts') AS t")[0]["t"] is None:
            return 0
        return self.q("SELECT count(*) AS n FROM b5_fixture_provider_attempts WHERE run_id=:run")[0]["n"]

    def latest_checkpoint(self) -> dict | None:
        rows = self.q("SELECT id, revision, manifest FROM checkpoints WHERE run_id=:run ORDER BY revision DESC LIMIT 1")
        return dict(rows[0]) if rows else None

    def checkpoint_by_revision(self, revision: int) -> dict:
        return dict(self.q("SELECT id, revision, manifest FROM checkpoints WHERE run_id=:run AND revision=:rev", rev=revision)[0])

    def event_count(self, kind: str, state: str | None = None) -> int:
        if state is None:
            return self.q("SELECT count(*) AS n FROM events WHERE run_id=:run AND kind=:k", k=kind)[0]["n"]
        return self.q("SELECT count(*) AS n FROM events WHERE run_id=:run AND kind=:k AND payload->>'state'=:s", k=kind, s=state)[0]["n"]

    def db_clock(self) -> datetime:
        return self.q("SELECT clock_timestamp() AS t")[0]["t"]

    def verify_checkpoint(self, manifest: dict) -> str:
        """Read the manifest's objects back from MinIO: verified | missing | mismatch | unavailable."""
        mf = CheckpointManifest.model_validate(manifest)
        for ref in (mf.context, *mf.workspace):
            try:
                body = self.s3.get_object(Bucket=BUCKET, Key=ref.key)["Body"].read(ref.size + 1)
            except Exception as exc:
                code = str(((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code", ""))
                return "missing" if code in {"404", "NoSuchKey", "NotFound"} else "unavailable"
            if len(body) != ref.size or sha(body) != ref.sha256:
                return "mismatch"
        return "verified"

    def project_object_keys(self) -> set[str]:
        keys: set[str] = set()
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=f"{self.project_id}/"):
            keys.update(item["Key"] for item in page.get("Contents", []))
        return keys

    def orphan_count(self) -> int:
        """Objects under this project's prefix with no stored_objects registry row (counts only; keys never kept)."""
        registered = {r["key"] for r in self.q("SELECT key FROM stored_objects WHERE project_id=:p", p=self.project_id)}
        return len(self.project_object_keys() - registered)

    # -- barrier flow
    def wait_barrier(self, timeout: float = 90) -> dict:
        """Wait until generation 1's POST response is held after the result committed (single attempt)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.q("SELECT to_regclass('b5_fixture_delivery_barriers') AS t")[0]["t"] is not None:
                barrier = self.q("SELECT reached_at, released FROM b5_fixture_delivery_barriers WHERE run_id=:run AND generation=1")
                ops, attempts = self.ops(), self.attempts()
                if barrier and barrier[0]["reached_at"] is not None and not barrier[0]["released"] and len(ops) == 1:
                    op = ops[0]
                    ref = (op["result"] or {}).get("ref")
                    if op["state"] != "committed" or not ref or attempts != 1 or op["usage_tokens"] != 2:
                        raise RuntimeError("barrier reached without one committed result and one attempt")
                    ckpt = self.latest_checkpoint()
                    if ckpt is None:
                        raise RuntimeError("barrier reached without a durable checkpoint")
                    return {"operation_id": op["operation_id"], "ref": ref, "attempts": attempts,
                            "usage_tokens": op["usage_tokens"], "reserve_tokens": op["reserve_tokens"],
                            "checkpoint_id": str(ckpt["id"]), "checkpoint_revision": ckpt["revision"]}
            time.sleep(0.1)
        raise TimeoutError("did not observe the held post-commit barrier")

    def release_barrier(self) -> None:
        with session() as db:
            if db.execute(text("UPDATE b5_fixture_delivery_barriers SET released=true WHERE run_id=:r AND generation=1"),
                          {"r": self.run_id}).rowcount != 1:
                raise RuntimeError("exact barrier release row was not updated")
            db.commit()

    def barrier_kill_fence(self, barrier_image: str) -> dict:
        """Gen-1 worker on the barrier fixture; kill it after commit/before delivery; fence with the same image."""
        self.stage = "barrier_start"
        self.configure(barrier_image, "barrier")
        gen, worker = self.claim_start()
        if gen != 1:
            raise RuntimeError("expected generation one")
        self.stage = "barrier_wait"
        baseline = self.wait_barrier()
        self.stage = "barrier_kill"
        self.kill(worker)
        return baseline

    def resume_to_completion(self, counter_image: str, label: str = "resume") -> tuple[int, int, str]:
        """After barrier_kill_fence: fence gen 1 with the barrier config, release it, continue on the counter fixture."""
        self.stage = "fence_generation_one"
        if self.recover().state != "queued":  # barrier-image config still active: exact dispatch match
            raise RuntimeError("old generation was not fenced to queued")
        self.release_barrier()
        self.stage = "continue_on_counter"
        self.configure(counter_image, label)
        if self.recover().state != "queued":
            raise RuntimeError("recovery did not queue the run")
        gen, worker = self.claim_start()
        code = self.wait(worker)
        return gen, code, self.recover().state

    # -- cleanup
    def exact_cleanup(self, remove_files: bool = True) -> dict:
        rows = self.executors()
        if not rows or any(r["state"] != "inactive" or not r["engine_id"]
                          or not (r["container_id"] or (r["proof"] or {}).get("source") == "worker-launch-not-attempted") for r in rows):
            raise RuntimeError("executors are not all durably inactive")
        for r in rows:
            proof = r["proof"] or {}
            if proof.get("source") == "worker-launch-not-attempted":
                # product writes exactly this proof (no engine/container binding) for a run whose worker never launched
                if r["container_id"] is not None or r["engine_id"] != EXPECTED_ENGINE_ID or proof != {"source": "worker-launch-not-attempted"}:
                    raise RuntimeError("worker-launch-not-attempted proof is not the exact product shape on the pinned engine")
                continue
            if (proof.get("source") not in EXACT_PROOF_SOURCES or proof.get("engine_id") != r["engine_id"]
                    or r["engine_id"] != EXPECTED_ENGINE_ID or proof.get("container_id") != r["container_id"]):
                raise RuntimeError("executor cleanup proof does not bind the exact physical identity and pinned engine")
        for r in rows:
            if r["container_id"] and self.docker("ps", "-aq", "--no-trunc", "--filter", f"id={r['container_id']}").strip():
                raise RuntimeError("an inactive executor container still exists")
        removed = 0
        for net in [n for n in self.docker("network", "ls", "-q", "--filter", f"label=scientist.platform/run={self.run_id}").splitlines() if n]:
            info = json.loads(self.docker("network", "inspect", "--format", "{{json .}}", net))
            labels = info.get("Labels") or {}
            gen = labels.get("scientist.platform/generation", "")
            if (not gen.isdigit() or info.get("Name") != f"scientist-run-{self.run_id.hex[:12]}-g{gen}"
                    or labels.get("scientist.platform/run") != str(self.run_id) or info.get("Containers")):
                raise RuntimeError("network identity mismatch or still attached")
            self.docker("network", "rm", net)
            removed += 1
        if self.docker("network", "ls", "-q", "--filter", f"label=scientist.platform/run={self.run_id}").strip():
            raise RuntimeError("run-labelled networks remain")
        if remove_files:
            self.remove_private_files()
        return {"status": "PASS", "executors_inactive": True, "networks_removed": removed, "engine_id": self.engine_id}

    def remove_private_files(self) -> None:
        self.config_path.unlink(missing_ok=True)
        shutil.rmtree(self.launch_root, ignore_errors=True)

    def fail_cleanup(self) -> dict:
        """Best-effort: fence/stop only this run, then try exact cleanup. Never raises."""
        if self.run_id is None or self.eng is None:
            return {"status": "NOT_STARTED"}
        try:
            with session() as db:
                state = db.execute(text("SELECT state FROM runs WHERE id=:r"), {"r": self.run_id}).scalar_one()
                if getattr(self, "storage_breach", False):
                    # Storage stop: fence only (reserved -> unknown, reservations kept); no recovery or relaunch.
                    if state not in TERMINAL:
                        supervisor.stop(db, self.run_id, 5)
                elif state not in TERMINAL:
                    view = supervisor.stop(db, self.run_id, 5)
                    if (view.state, getattr(view, "waiting_reason", None)) == ("waiting_input", "unknown_outcome"):
                        supervisor.recover(db, self.run_id)
                else:
                    supervisor.recover(db, self.run_id)
            return self.exact_cleanup()
        except Exception as exc:
            return {"status": "UNPROVEN; operator review required", "error_type": type(exc).__name__}


# ---------------------------------------------------------------- deployment restart
def wait_ready(timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            engine = create_engine(DB_URL, connect_args={"connect_timeout": 3})
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
            s3_client(fast=True).head_bucket(Bucket=BUCKET)
            return
        except Exception:
            if time.monotonic() >= deadline:
                raise TimeoutError("owned PostgreSQL/MinIO did not become ready") from None
            time.sleep(1)


def guard_storage_headroom() -> None:
    """Stop before the persistent b5 volumes or the VM disk fill (owner-approved monitoring)."""
    import b5_persistent_services as ps
    free, sizes = ps.vm_free_kib(), ps.volume_kib()
    if free < ps.MIN_FREE_KIB or sum(sizes.values()) * 1024 > ps.VOLUME_BUDGET_BYTES:
        raise RuntimeError(f"b5 storage headroom exhausted (free {free} KiB, volumes {sizes}); refusing to continue")


def guard_no_foreign_running(own_run: UUID) -> None:
    """Refuse a PostgreSQL/MinIO restart or stop while any other run's labelled container is running."""
    out = subprocess.run(["docker", "--context", CTX, "ps", "--no-trunc", "--filter", "label=scientist.platform/run",
                          "--format", '{{.ID}} {{.Label "scientist.platform/run"}}'],
                         capture_output=True, text=True, timeout=45, check=False)
    if out.returncode:
        raise RuntimeError("could not list labelled run containers; refusing to touch shared services")
    foreign = [line for line in out.stdout.splitlines() if line.strip() and line.split()[-1] != str(own_run)]
    if foreign:
        raise RuntimeError(f"{len(foreign)} running container(s) belong to another run; refusing service restart/stop")


def restart_services(own_run: UUID) -> None:
    """Restart only the owned isolated test PostgreSQL and MinIO containers, then wait for readiness."""
    guard_no_foreign_running(own_run)
    scientist_db.engine().dispose()
    result = subprocess.run(["docker", "--context", CTX, "restart", "--time", "20", PG_CONTAINER, MINIO_CONTAINER],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=240, check=False)
    if result.returncode:
        raise RuntimeError("owned service restart failed")
    wait_ready()


def container_state(name: str, action: str, own_run: UUID) -> None:
    """`docker stop|start` of one owned test service (storage-fault actor)."""
    if name not in {PG_CONTAINER, MINIO_CONTAINER} or action not in {"stop", "start"}:
        raise ValueError("only owned test services may be stopped or started")
    if action == "stop":
        guard_no_foreign_running(own_run)
    result = subprocess.run(["docker", "--context", CTX, action, name], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=120, check=False)
    if result.returncode:
        raise RuntimeError(f"owned service {action} failed")


# ---------------------------------------------------------------- proofs
def write_json(path: Path, value: dict) -> None:
    """Archive any existing file (sha256 in its name) before overwriting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        ARCHIVE.mkdir(parents=True, exist_ok=True, mode=0o700)
        old = path.read_bytes()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (ARCHIVE / f"{path.stem}-{sha(old)[:12]}-{stamp}{path.suffix}").write_bytes(old)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8")


def proof_doc(case: str, fixture_ref_: str, proof: dict, evidence: dict) -> dict:
    return {"schema_version": 1, "case": case, "status": "PASS", "worker_image_digest": WORKER_IMAGE,
            "server_image_digest": SERVER_DIGEST, "fixture_image_digest": digest_of(fixture_ref_),
            "proof": proof, "evidence": {**evidence, "repo": dict(REPO)}}


def emit(name: str, doc: dict) -> Path:
    path = SECURITY / f"b5-matrix-{name}.json"
    write_json(path, doc)
    ev = doc.get("evidence") or {}
    readback = None
    if doc.get("proof") is not None and ev.get("run_id") and ev.get("baseline"):
        readback = (f"{sys.executable} {Path(__file__).with_name('b5_supervisor_matrix.py')} {doc['case']} --run-id {ev['run_id']} "
                    f"--baseline {ev['baseline']} --fixture-proof {path} --server-image-digest {doc['server_image_digest']} "
                    f"--fixture-image-digest {doc['fixture_image_digest']} --output {SECURITY / ('b5-matrix-' + name + '-result.json')}")
    print(json.dumps({"status": doc.get("status"), "case": doc.get("case"), "proof": str(path), "readback": readback}, sort_keys=True))
    return path


def write_baseline(h: H, name: str) -> Path:
    """Matrix-format baseline (`{"run_id","snapshot"}`) built with the matrix's own snapshot reader."""
    import b5_supervisor_matrix as matrix  # same directory; module has no import-time side effects
    engine = create_engine(DB_URL, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            snap = matrix._snapshot(conn, h.run_id, h.s3, docker_context=CTX, tolerate_storage_fault=True)
    finally:
        engine.dispose()
    path = SECURITY / f"b5-matrix-{name}-baseline.json"
    write_json(path, {"schema_version": 1, "run_id": str(h.run_id), "snapshot": snap})
    return path


def write_min_baseline(h: H, name: str) -> Path:
    """Baseline for matrix cases that read only `run_id` + the starting run row (race/hung/recovery/fault)."""
    path = SECURITY / f"b5-matrix-{name}-baseline.json"
    write_json(path, {"schema_version": 1, "run_id": str(h.run_id), "snapshot": {"run": h.run_row()}})
    return path


@contextmanager
def actor(name: str, *fixture_refs: str, fast_s3: bool = False):
    """Set up the harness; on any failure write a sanitized FAILED record and run exact best-effort cleanup."""
    h = H(name, fast_s3=fast_s3)
    halt = threading.Event()
    gate = threading.Lock()  # makes monitor's check-then-kill atomic against halt.set()

    def monitor() -> None:
        # Continuous headroom check while the actor runs; a breach interrupts the main thread so the
        # actor exits through fail_cleanup (fence only, never resend, never delete data).
        while not halt.wait(30):
            try:
                guard_storage_headroom()
            except Exception as exc:
                with gate:
                    if halt.is_set():
                        return
                    h.storage_breach = True
                    h.storage_breach_cause = type(exc).__name__
                    os.kill(os.getpid(), signal.SIGINT)
                return

    prev_sigint = signal.getsignal(signal.SIGINT)

    def _sigint(signum, frame):
        if halt.is_set():
            return  # cleanup is running: a late interrupt must not land inside it
        (prev_sigint if callable(prev_sigint) else signal.default_int_handler)(signum, frame)
    signal.signal(signal.SIGINT, _sigint)
    watcher = threading.Thread(target=monitor, name="b5-storage-headroom", daemon=True)
    try:
        require_clean_repo()
        watcher.start()
        h.setup(*fixture_refs)
        yield h
    except BaseException as exc:
        with gate:
            halt.set()  # no further interrupt may land inside cleanup
        record = {"schema_version": 1, "case": name, "status": "FAILED", "failed_at": h.stage,
                  "failure_type": type(exc).__name__, "run_id": str(h.run_id) if h.run_id else None,
                  "storage_breach": getattr(h, "storage_breach", False),
                  "storage_breach_cause": getattr(h, "storage_breach_cause", None),
                  "cleanup": h.fail_cleanup(), "worker_image_digest": WORKER_IMAGE}
        write_json(SECURITY / f"b5-matrix-{name}-failure.json", record)
        print(json.dumps({"status": "FAILED", "case": name, "failed_at": h.stage, "failure_type": type(exc).__name__}, sort_keys=True))
        raise
    finally:
        with gate:
            halt.set()
        signal.signal(signal.SIGINT, prev_sigint)
        if watcher.is_alive():
            watcher.join()


# ---------------------------------------------------------------- fresh supervisor process
def fresh_process(h: H, recover_image: str, resume_image: str | None) -> dict:
    """Run recover (old dispatch image) [+ recover/claim/start/wait/recover on resume_image] in a NEW process."""
    cmd = [sys.executable, str(Path(__file__).resolve()), "fresh", "--run-id", str(h.run_id), "--recover-image", recover_image]
    if resume_image:
        cmd += ["--resume-image", resume_image]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=420, check=False)
    lines = [line for line in out.stdout.splitlines() if line.startswith("{")]
    if out.returncode != 0 or not lines:
        raise RuntimeError("fresh supervisor process failed")
    return json.loads(lines[-1])


def _redact(text_: str) -> str:
    return re.sub(r"[A-Za-z0-9_\-+/=]{32,}", "<redacted>", text_)


def _fresh_main(args: argparse.Namespace) -> int:
    h = H("fresh").setup(args.recover_image, *([args.resume_image] if args.resume_image else []))
    h.attach(UUID(args.run_id))
    result: dict[str, Any] = {}
    h.configure(args.recover_image, "fresh-recover")
    view = h.recover()
    result.update(state=view.state, waiting_reason=view.waiting_reason)
    if args.resume_image:
        h.configure(args.resume_image, "fresh-resume")
        if h.recover().state != "queued":
            raise RuntimeError("fresh recovery did not queue the run")
        gen, worker = h.claim_start()
        result.update(generation=gen, exit_code=h.wait(worker))
        if result["exit_code"] != 0:  # diagnostics only: last worker log lines; long token-like strings are redacted before they are stored
            result["worker_log_tail"] = _redact(h.docker("logs", "--tail", "15", worker)[-1500:])
            for cid in h.docker("ps", "-aq", "--filter", f"label=scientist.platform/run={h.run_id}",
                                "--filter", "label=scientist.platform/kind=dispatch",
                                "--filter", f"label=scientist.platform/generation={gen}").split():
                state = h.docker("inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", cid)
                result.setdefault("dispatch", []).append({"state": state, "log_tail": _redact(h.docker("logs", "--tail", "25", cid)[-2500:])})
        final = h.recover()
        result.update(state=final.state, waiting_reason=final.waiting_reason)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    fresh = sub.add_parser("fresh")
    fresh.add_argument("--run-id", required=True)
    fresh.add_argument("--recover-image", required=True)
    fresh.add_argument("--resume-image")
    namespace = parser.parse_args()
    raise SystemExit(_fresh_main(namespace))
