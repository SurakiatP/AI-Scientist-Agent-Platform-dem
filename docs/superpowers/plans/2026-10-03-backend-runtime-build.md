# Backend and Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the authoritative local research lifecycle and prove safe isolated continuation before exposing a working paper workflow.

**Architecture:** One Python package provides concrete PostgreSQL domain functions, REST handlers, a credential-holding broker and a trusted supervisor. Run workers have private runtime state, immutable approved inputs and broker-only egress. Durable operation results and versioned context/workspace manifests drive restart recovery.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy/psycopg, PostgreSQL, httpx, boto3, cryptography, pytest and Docker. PDF/XLSX preparation uses reviewed bounded parsers inside an isolated preparation worker.

**Spec:** [Runtime specification](../specs/2026-10-03-platform-runtime-design.md). Execution order, model allocation and project-wide constraints are in the [master plan](2026-10-03-platform-build.md).

## Global Constraints

All master-plan constraints apply verbatim. No test bypass flag may disable production authorization, ledger persistence or network containment. Fixtures use dependency injection and isolated resources, not a production insecure mode. Logs redact secrets/content; workers never receive provider, peer, S3 or database credentials.

## Review Focus

- External planning must not charge a model before owner authorization: B2/B3.
- Replayed requests or stale approvals must not duplicate/alter runs: B1/B2.
- A lost response must retain an unknown reservation and require an owner decision: B3/B5.
- Malicious PDF/archive/spreadsheet parsing must stay bounded and non-executing: B4/B5.
- A surviving stale worker must lose broker authority before recovery starts another: B5.

## Shared types and database contract

B1 defines these concrete Pydantic models in `backend/src/scientist/contracts.py`, with `extra='forbid'`, nonnegative limits, UUID parsing, request-size bounds and string-length validation. Field names remain stable across REST/generated TypeScript/protocol adapters:

```python
class Principal(BaseModel):
    identity: UUID
    kind: Literal['owner', 'external', 'worker']

class ObjectRef(BaseModel):
    project_id: UUID
    key: str
    sha256: str
    size: int
    content_type: str

class PackageSpec(BaseModel):
    name: str
    version: str
    source: str
    sha256: str

class PlanSpec(BaseModel):
    input_snapshot_digest: str
    provider_id: UUID
    model: str
    stages: list[str]
    allowed_ops: list[str]
    data_recipients: list[str]
    packages: list[PackageSpec]
    token_limit: int
    elapsed_limit_ms: int

class ArtifactView(BaseModel):
    artifact_id: UUID
    project_id: UUID
    run_id: UUID
    title: str
    kind: Literal['report', 'table', 'plot', 'file']
    sha256: str
    size: int
    content_type: str
    partial: bool

class PlanView(BaseModel):
    run_id: UUID
    revision: int
    plan_digest: str
    plan: PlanSpec

class RunView(BaseModel):
    run_id: UUID
    project_id: UUID
    session_id: UUID
    revision: int
    state: Literal['planning', 'awaiting_approval', 'queued', 'running',
                   'waiting_input', 'recovering', 'stopping', 'completed',
                   'failed', 'canceled', 'rejected']
    stage: str | None
    waiting_reason: str | None
    error_code: str | None
    plan_digest: str | None
    latest_cursor: int
    usage_tokens: int
    reserved_tokens: int
    planning_tokens: int
    token_limit: int
    artifacts: list[ArtifactView]

class OperationRequest(BaseModel):
    run_id: UUID
    generation: int
    operation_id: str
    kind: Literal['llm', 'search', 'package', 'peer']
    payload: dict
    reserve_tokens: int

class OperationResult(BaseModel):
    operation_id: str
    state: Literal['committed', 'unknown', 'denied']
    result: ObjectRef | None
    usage_tokens: int | None

class CheckpointManifest(BaseModel):
    schema_version: int
    run_id: UUID
    revision: int
    plan_digest: str
    runtime_commit: str
    image_digest: str
    skills_digest: str
    context: ObjectRef
    workspace: list[ObjectRef]
    environment_digest: str
    operation_ids: list[str]
```

Event contract: `{schema_version: 1, run_id: UUID, sequence: int, revision: int, occurred_at: UTC timestamp, kind: str, payload: dict}`. Public event kinds are `plan.ready`, `run.state`, `stage.started`, `stage.completed`, `artifact.ready`, `decision.required`, and `usage.updated`; payloads use explicit allowlisted schemas. Cursor is the event sequence, not a transient socket offset. API errors are `{code: str, message: str, request_id: str}` with no upstream raw response or traceback.

Also export `ProjectView(id, name, revision, instructions)`, `SessionView(id, project_id, title)`, `FileView(id, project_id, filename, size, content_type, state, error_code)`, `FindingView(id, project_id, session_id, artifact_id, text, citation_ids)`, `CitationView(id, title, authors, year, identifier, original_url, access, verification)`, `ConnectionView(id, label, provider, model, state, has_secret)`, and `RunEvent` using these same field names in generated TypeScript. IDs are UUIDs; nullable source/error fields are explicitly optional, collection fields are arrays, file states are uploading/preparing/ready/failed and connection states are unconfigured/checking/ready/invalid_credentials/unavailable_provider/unavailable_model. API-facing artifact metadata contains no object key or storage credential; trusted ObjectRef remains an internal storage/checkpoint type.

Database rows cover projects, sessions, messages, findings, sources/citations, file_versions, artifacts, publication_requests, runs, input_snapshots, plan_revisions, approvals, operations, checkpoints, events, credentials, owner_sessions, access_tokens/grants and delegations. Foreign keys preserve project ownership; composite references prevent a run from attaching a session/input belonging to another project. Immutable snapshot/plan/checkpoint rows are append-only. `runs` holds current revision, state, consumed/reserved tokens, planning usage, lease generation/expiry and cancel request; all mutation functions lock the affected run row and append an event in the same transaction.

### B1 — Durable records and typed contracts

**Owns:** root `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore` changes; `backend/src/scientist/{__init__,settings,contracts,db}.py`; `backend/migrations/001_initial.sql`; `backend/tests/{conftest,test_records}.py`; `contracts/{run-event.schema.json,api-types.ts}`. Parent owns later generated changes to `api-types.ts`.

**Dependencies:** Approved plan and isolated checkout. No Docker image is pulled before host/security prerequisites are resolved.

**Interfaces:** `db.session() -> context manager[sqlalchemy.orm.Session]`, `db.migrate() -> None`, `db.create_project(db, name: str) -> UUID`, `db.create_session(db, project_id: UUID, title: str) -> UUID`; typed models above. Test fixture `db` supplies a transaction against a task-specific PostgreSQL database, with no production connections. Test fixture `project_session` returns `(project_id, session_id)` created by these functions.

Creation functions flush SQL before returning so foreign-key violations are observed by the caller's transaction. Database fixtures never swallow an integrity failure or auto-commit partially constructed test records.

- [ ] **1. Write the durable ownership/idempotency checks.** Include migrations-once/hash mismatch, restart persistence and same-project foreign-key tests. Use this core assertion against the real test database:

```python
def test_session_cannot_reference_missing_project(db):
    with pytest.raises(IntegrityError):
        create_session(db, uuid4(), 'invalid')

def test_contract_rejects_negative_reservation():
    with pytest.raises(ValidationError):
        OperationRequest(run_id=uuid4(), generation=1, operation_id='op1',
                         kind='llm', payload={}, reserve_tokens=-1)
```

- [ ] **2. Resolve the isolated test environment and observe RED.** Recreate the workspace venv with `python -m venv .venv`, as originally requested, using the selected patched 3.13 interpreter after confirming it is a disposable environment. Discover that interpreter through UV without falling back to Python 3.9, then run the exact module command using its returned path:

```python
interpreter = subprocess.check_output(
    ['rtk', 'proxy', 'uv', 'python', 'find', '3.13'], text=True).strip()
subprocess.run(['rtk', 'proxy', interpreter, '-m', 'venv', '.venv'], check=True)
```

Reconcile the selected patched 3.13 interpreter with `.python-version`; do not force an old patch solely because it was previously written there. Minimal packaging configuration:

```toml
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "ai-scientist-agent-platform"
version = "0.1.0"
requires-python = ">=3.13,<3.14"
dependencies = []

[tool.setuptools.packages.find]
where = ["backend/src"]

[tool.pytest.ini_options]
testpaths = ["backend/tests"]
```

Install only the backend/test dependencies above through `rtk proxy uv add`, then `rtk proxy uv add --dev pytest`. Resolve current patched compatible versions and reject a resolver result with an applicable critical/high advisory. Run `rtk proxy uv run pytest backend/tests/test_records.py -q`; the initial failure is missing new models/functions, not connection to an existing database. Test DB connection is operator-configured outside the agent context. If PostgreSQL is unavailable, record the prerequisite and do not claim the SQL checks passed.

- [ ] **3. Implement schema, migration runner and validation.** Apply the migration under a PostgreSQL advisory lock; store its checksum in `schema_migrations` and reject editing an applied migration. Use JSONB for immutable manifests, and typed relational ownership/counters for enforceable constraints. Required SQL patterns:

```sql
UNIQUE (caller_identity, submission_key)
UNIQUE (run_id, sequence)
UNIQUE (run_id, operation_id)
CHECK (usage_tokens >= 0 AND reserved_tokens >= 0)
CHECK (token_limit >= 0)
UNIQUE (id, project_id)
```

The last composite key supports same-project foreign keys. Admission reserves under a locked row/conditional update only when consumed plus reserved plus requested allowance fits the approved limit; test the concurrent database transaction, not just an in-memory comparison. Settlement must still record truthful provider usage if its report unexpectedly exceeds a reservation, prevent further dispatch and surface budget exhaustion; do not use a ceiling CHECK that would reject recording an already incurred charge. Store payload hashes beside submission/operation identities; constraint conflict does not silently accept a changed payload. Generate shared TS types/event JSON schema from the validated backend models with a deterministic checked-in generator in `backend/src/scientist/contracts.py` (`python -m scientist.contracts`), rather than maintaining two independent definitions.

- [ ] **4. Verify and parent-commit.** Run `rtk proxy uv run pytest backend/tests/test_records.py -q`, `rtk proxy uv run python -m scientist.contracts`, and `rtk git diff --check`. Parent checks fresh-database migration and second application, commits `feat: add durable research records and contracts`, and publishes exact types to dependent workers. Lockfiles stay parent-controlled thereafter.

### B2 — Owner boundary, grants, secrets and approved lifecycle

**Owns:** `backend/src/scientist/{app,auth,domain,secrets}.py`; `backend/tests/{test_auth,test_lifecycle}.py`. Uses B1 schema; schema corrections are routed to parent, never concurrent migration edits.

**Dependencies:** B1.

**Interfaces:**

```python
authorize(db, principal: Principal, action: str, project_id: UUID) -> None
create_token(db, owner: Principal, grants: dict[UUID, list[str]]) -> str
revoke_token(db, owner: Principal, token_id: UUID) -> None
authenticate_bearer(db, token: str) -> Principal
save_secret(db, owner: Principal, label: str, value: str) -> UUID
read_secret(db, credential_id: UUID) -> str  # trusted broker only
submit_run(db, principal: Principal, project_id: UUID, session_id: UUID,
           submission_key: str, question: str, input_ids: list[UUID],
           provider_id: UUID, model: str) -> RunView
revise_plan(db, owner: Principal, run_id: UUID, expected_revision: int,
            plan: PlanSpec) -> RunView
approve_run(db, owner: Principal, run_id: UUID, expected_revision: int,
            plan_digest: str) -> RunView
request_stop(db, principal: Principal, run_id: UUID) -> RunView
get_run(db, principal: Principal, run_id: UUID) -> RunView
get_plan(db, owner: Principal, run_id: UUID) -> PlanView
get_events(db, principal: Principal, run_id: UUID, after: int, limit: int = 100) -> list[dict]
```

Domain failures use `DomainError(code: str, status: int)`. Codes include `forbidden`, `not_found`, `revision_conflict`, `idempotency_conflict`, `approval_required`, `cursor_expired`, `budget_exhausted` and `storage_unavailable`. No caller learns whether a forbidden cross-project object exists.

Validate event pagination `after >= 0` and `1 <= limit <= 200`; no transport may request an unbounded history. Grant checks apply to each cursor page and current stream authorization.

- [ ] **1. Write RED policy/lifecycle checks.** Test wrong Host/Origin, missing CSRF, expired bootstrap/session, bearer impersonation, token revocation, external preparation without approval, stale plan digest, changed-body replay and concurrent publish guards. The lifecycle test uses owner/external principals created by a test helper in this test file:

```python
def test_changed_submission_key_is_a_conflict(db, project_session, owner, provider_id):
    project, session = project_session
    submit_run(db, owner, project, session, 'same', 'first', [], provider_id, 'fixture')
    with pytest.raises(DomainError, match='idempotency_conflict'):
        submit_run(db, owner, project, session, 'same', 'changed', [], provider_id, 'fixture')

def test_external_cannot_approve(db, external, unapproved_run):
    with pytest.raises(DomainError, match='forbidden'):
        approve_run(db, external, unapproved_run.run_id,
                    unapproved_run.revision, unapproved_run.plan_digest)
```

Define `owner`, `external`, `provider_id`, and `unapproved_run` fixtures locally using B1 database writes plus B2 public setup functions. No fixture contains an actual secret.

- [ ] **2. Observe RED.** Run `rtk proxy uv run pytest backend/tests/test_auth.py backend/tests/test_lifecycle.py -q` and check failure arises from missing authorization/lifecycle code.

- [ ] **3. Implement the shared policy and lifecycle.** Use `secrets.token_urlsafe(32)` tokens, SHA-256 hashes for high-entropy tokens, per-project/action grants, UTC expiry/revocation, and constant-time comparison where needed. Owner bootstrap is generated by the trusted local launcher, consumed once via a same-origin POST, exchanged for an HttpOnly SameSite cookie and CSRF token. Deliver it in a local URL fragment cleared immediately by the frontend; never embed it in static assets, query parameters or logs. Reject non-loopback owner requests and foreign Host/Origin before bootstrap, with distinct owner/external authentication paths.

Encrypt stored provider/peer values with cryptography using a master key from the OS key store/operator-mounted secret; fail closed if missing. `read_secret` is used only in broker composition, never as a public route. Secret responses return identifier/label/masked presence only.

Lock run rows for deduplication, revisions, stop and approval. Canonical digests use UTF-8 JSON with sorted keys and fixed separators; bind file/instruction/finding versions, conversation prefix, model/scope/limits. External submissions cannot launch planning-model requests. Owner planning-only authorization has a visible bounded allowance and no research tools. State+event writes are one transaction.

```python
if row.revision != expected_revision or row.plan_digest != plan_digest:
    raise DomainError('revision_conflict', 409)
if principal.kind != 'owner':
    raise DomainError('forbidden', 403)
```

- [ ] **4. Verify and parent-commit.** Run both suites and the B1 records suite once, inspect logs for secret/content disclosure using synthetic markers, then commit `feat: enforce owner approval and scoped access`. Publish the function signatures and domain error map to the frontend/broker/protocol workers.

### B3 — Fail-closed effect broker and bounded usage

**Owns:** `backend/src/scientist/{broker,broker_api}.py`; `backend/tests/test_broker.py`. No public API route edits.

**Dependencies:** B1/B2. B4 later provides concrete object storage; use injected result-byte persistence in unit tests until that function is available.

**Interfaces:** `broker.execute(db, worker_capability: str, request: OperationRequest) -> OperationResult`; `broker.reconcile(db, run_id: UUID, operation_id: str) -> OperationResult`; `broker.issue_capability(db, run_id: UUID, generation: int, ttl_seconds: int) -> str`; `broker.resolve_unknown(db, owner: Principal, run_id: UUID, operation_id: str, decision: Literal['verified_result','retry','stop'], result: ObjectRef | None) -> RunView`. Internal HTTP `POST /effects` accepts the request/capability and returns this model. Broker composition owns HTTP clients and credentials, validates endpoint policy and applies controlled result storage.

- [ ] **1. Write the failure and race checks.** Use a local deterministic transport injected into the broker; it records calls and can lose a response. Do not turn off endpoint policy to run tests. Add a two-transaction concurrent reservation test against PostgreSQL:

```python
def test_committed_operation_is_not_sent_twice(broker_fixture, approved_request):
    first = broker_fixture.execute(approved_request)
    second = broker_fixture.execute(approved_request)
    assert first == second
    assert broker_fixture.transport.calls == 1

def test_unknown_response_stays_reserved(broker_fixture, approved_request):
    broker_fixture.transport.lose_response = True
    result = broker_fixture.execute(approved_request)
    assert result.state == 'unknown'
    assert broker_fixture.run().reserved_tokens == approved_request.reserve_tokens
    broker_fixture.reconcile(approved_request.operation_id)
    assert broker_fixture.transport.calls == 1
```

`broker_fixture` is a local test composition of real B2 policy/DB, byte-storage test function and deterministic transport. `approved_request` is created from an owner-approved plan and current lease generation. Cover storage/ledger outage, changed operation payload, expired generation/capability, unauthorized model/recipient/package, redirects/DNS rebinding/IPv6 metadata destinations and two reservations competing for the final allowance. A synthetic provider reporting usage above its reserved amount must leave truthful usage in the ledger and block new work rather than losing the report to a database constraint. Owner-provided unknown-outcome result references require same-project ownership, integrity and operation binding; reject forged cross-project objects and do not equate an arbitrary uploaded assertion with a verified provider result.

- [ ] **2. Observe RED.** Run `rtk proxy uv run pytest backend/tests/test_broker.py -q`; no external/provider network traffic is permitted in this suite.

- [ ] **3. Implement effect accounting before dispatch.** Lock the run, validate live generation and research/planning scope, insert the operation/fingerprint and reservation, then commit before network I/O. Dispatch only configured operation classes and destinations. On success, persist response bytes/hash and transactionally settle reported usage before returning. On lost response or failure to record a sent result, preserve the reservation and mark unknown; no automatic retry. Reject repeated identity with changed content. A new owner-authorized retry retains the original reservation and uses a linked new operation identity.

```python
if consumed + reserved + request.reserve_tokens > token_limit:
    raise DomainError('budget_exhausted', 409)
if request.generation != active_generation:
    raise DomainError('forbidden', 403)
```

LLM output limits and conservative input/output reservations precede dispatch. Secrets are attached only inside the trusted outbound client. Validate DNS resolution/address ranges and pinned connection targets; revalidate each redirect, cap response bytes/time and reject unauthorized authority changes. Peer LAN allowlists are separate explicit approvals. Package artifacts use approved exact source/name/version/hash; do not implement an open proxy or arbitrary URL fetcher. Internal `/effects` receives no owner's cookie or external token.

- [ ] **4. Verify and parent-commit.** Run the broker suite, B2 policy checks and the DB concurrent-reservation test. Inspect that telemetry outages/hooks cannot bypass effect authorization. Commit `feat: journal and bound outbound research effects`.

### B4 — Immutable object storage and bounded file preparation

**Owns:** `backend/src/scientist/{objects,files}.py`; `runtime/prepare.py`; `backend/tests/test_objects.py`; `backend/tests/test_file_preparation.py`. Parent owns any dependency-lock changes.

**Dependencies:** B1/B2; production worker containment supplied by B5. Tests use a deterministic object transport until the separately gated real MinIO check.

**Interfaces:** `objects.put(db, project_id: UUID, content: BinaryIO, content_type: str) -> ObjectRef`; `objects.open_verified(ref: ObjectRef) -> context manager[BinaryIO]`; `objects.delete_unreferenced(db, key: str) -> bool`; `files.attach(db, principal: Principal, project_id: UUID, filename: str, content: BinaryIO) -> UUID`; `files.mark_prepared(db, file_id: UUID, extracted: ObjectRef, status: Literal['ready','failed']) -> None`; `files.publish(db, owner: Principal, run_id: UUID, object_keys: list[str], expected_project_revision: int, publication_key: str) -> list[UUID]`. `prepare.py` reads one approved local file and writes bounded extracted text/metadata to its own workspace; it accepts no arbitrary command or network destination.

- [ ] **1. Write corruption, parsing and publication checks.** Tests use synthetic content only:

```python
def test_corrupt_object_is_rejected(object_fixture):
    ref = object_fixture.put(b'original')
    object_fixture.corrupt(ref.key)
    with pytest.raises(ValueError, match='integrity'):
        with open_verified(ref):
            pass

def test_zip_expansion_is_bounded(tmp_path):
    archive = make_oversized_xlsx(tmp_path)  # local helper builds compressed repeated bytes
    with pytest.raises(ValueError, match='expanded_size'):
        prepare_local_file(archive, tmp_path / 'output')
```

Define `prepare_local_file(path: Path, output_dir: Path) -> dict` in `runtime/prepare.py`. Test traversal/symlink archives, empty/corrupt PDFs, CSV formula text, JSON depth/size limits, XLSX external links/macros, mismatched extensions/MIME, interrupted upload, missing bytes and two concurrent publication revisions. Parser output never becomes a policy instruction.

- [ ] **2. Observe RED.** Run `rtk proxy uv run pytest backend/tests/test_objects.py backend/tests/test_file_preparation.py -q`. Resolve reviewed parser versions through parent before modifying the shared lockfile. Unit fake storage is explicitly identified as fake, not evidence that validated open-source MinIO is operational.

- [ ] **3. Implement immutable keys and preparation limits.** Keys include project UUID plus content SHA-256 and a unique logical reference; never overwrite existing bytes. Verify uploaded size/hash before creating a ready version record. Support the designed PDF, CSV, XLSX, JSON, TXT and Markdown flow with an initial advertised 25 MiB upload cap, bounded extracted text/rows/columns/decompressed bytes and parser timeout/memory limits. Validate zip metadata before XLSX parsing; disable external links, macros and formula evaluation. CSV/JSON/text handling uses stdlib. PDF/XLSX third-party parsers run only in the preparation sandbox when processing actual uploaded content.

Expose separate `uploading`, `preparing`, `ready`, `failed` file states. The service advertises supported types/limits; UI reads them. Failure preserves an actionable category without reporting the file ready. Stream downloads through authenticated routes with safe Content-Disposition and content-type handling. No same-origin executable HTML or unrestricted signed S3 URLs.

Publication writes new logical versions under the expected project revision, retains input/run/source provenance and deduplicates its key. Tombstone shared deletion; garbage collection deletes only objects unreferenced by retained inputs/checkpoints/backups. Never rely on paid native version-specific deletion, SSE, replication or lifecycle transitions.

Initial retention is explicit owner-directed deletion, with no timed automatic purge. Retain active/retained run input and checkpoint references and every object referenced by retained backup manifests; a shared-file tombstone removes it from future context without destroying those versions. Cleanup of an unreferenced object is an authenticated maintenance operation and cannot run concurrently with a coordinated backup. Advertise disk pressure as a readiness/queue condition rather than deleting research data automatically.

- [ ] **4. Verify and parent-commit.** Run object/parser tests. After source/host prerequisites and image checks, verify actual MinIO PUT/GET/ordinary DELETE, corrupt/missing references and storage-unavailable behavior in a task-specific bucket. If those prerequisites are absent, record the real-store acceptance as pending and block B5. Commit `feat: preserve immutable research files and provenance`.

### B5 — Prove containment, runtime continuation and stopping

**Owns:** `runtime/{Dockerfile,entrypoint.py,skills-manifest.json}`; `backend/src/scientist/{supervisor,checkpoints,runtime_adapter}.py`; `backend/tests/test_runtime_recovery.py`; `backend/tests/test_containment.py`. Parent owns deployment/network integration and runtime dependency lock/SBOM review. No native protocol gateway is exposed.

**Dependencies:** B3/B4; fixed/mitigated engine, validated pinned OSS storage build, encrypted storage prerequisites and image scans. **This is the blocking feasibility tranche.**

**Interfaces:**

```python
supervisor.claim(db, max_active: int) -> tuple[UUID, int] | None
supervisor.start(db, run_id: UUID, generation: int) -> str  # container id
supervisor.stop(db, run_id: UUID, grace_seconds: int) -> RunView
supervisor.recover(db, run_id: UUID) -> RunView
checkpoints.capture(db, run_id: UUID, generation: int,
                    context: bytes, workspace_dir: Path) -> CheckpointManifest
checkpoints.restore(db, manifest: CheckpointManifest, workspace_dir: Path) -> bytes
runtime_adapter.run(context: bytes, broker_url: str,
                    capability: str, workspace_dir: Path) -> None
```

`context` is versioned JSON containing durable conversation/compacted context, next committed operation boundary, pending assistant/tool-call identities and local environment metadata; validate its schema rather than unpickling arbitrary objects. Stable operation IDs are derived from persisted run/turn/tool-call identities, recorded before dispatch. Broker persists LLM responses as well as tool results so recovery can reconstruct the pending boundary.

- [ ] **1. Write actual container/fault-injection checks.** Use a bounded test provider behind the trusted broker and a named run fixture, not a paid model. Core requirement:

```python
def test_crash_after_broker_commit_does_not_repeat_effect(runtime_fixture):
    run = runtime_fixture.approve_fixture_run()
    runtime_fixture.crash_worker_after_committed_response(run.run_id)
    runtime_fixture.recover(run.run_id)
    assert runtime_fixture.provider_call_count(run.run_id) == 1
    assert runtime_fixture.run(run.run_id).state == 'completed'

def test_lost_remote_response_requires_owner(runtime_fixture):
    run = runtime_fixture.run_with_lost_remote_response()
    runtime_fixture.recover(run.run_id)
    view = runtime_fixture.run(run.run_id)
    assert (view.state, view.waiting_reason) == ('waiting_input', 'unknown_outcome')
    assert view.reserved_tokens > 0
```

`runtime_fixture` in this file composes real database/storage/broker/supervisor with a deterministic provider, isolated containers and per-test labels. Include direct IP/IPv6/DNS internet/host/LAN/metadata/socket denial, cross-run capability rejection, filesystem escape, package denial, hard CPU/memory/PID/disk enforcement, two runs in one project plus another project, supervisor restart, compressed context, corrupted checkpoint, stale lease, full isolated deployment restart, and hung-tool stop/completion races.

- [ ] **2. Observe RED under safe prerequisites.** Check `rtk proxy docker version --format '{{.Server.Version}}'` against the current advisory and verify the validated open-source storage readiness without printing private credentials. Do not pull images or run live sandbox tests on an unresolved affected engine. Run `rtk proxy uv run pytest backend/tests/test_containment.py backend/tests/test_runtime_recovery.py -q`; initial RED must identify missing enforcement/continuation. Shared host remediation remains operator-owned.

- [ ] **3. Build the smallest enforceable worker integration.** Fetch the exact Hermes/catalog revisions inside the image build, retain required attribution, lock actual runtime dependencies and review the three complete skill directories/resources. Disable runtime self-update, host skill mounts and native gateway endpoints. Use private run `HERMES_HOME`; private SQLite is not the platform state.

Restrict the pinned runtime to platform-wrapped tool entrypoints and broker-routed provider calls. Prove hooks failing open cannot route around the external broker/network boundary. Fail before broad product work if pinned runtime state/tool-call integration cannot support the required continuation; do not patch around it by relying only on telemetry.

Launch non-root/read-only/capability-dropped containers on a per-run internal network. Their only service is the authenticated broker; no other run/service network, database/storage route, mounted secret, Docker socket or host directory. Mount a read-only input snapshot and a dedicated quota-enforced writable workspace. Check actual driver/disk-limit enforcement instead of silently omitting it.

Use `SKIP LOCKED` leases with increasing generations. Capture checkpoint bytes at a quiescent boundary, hash/verify them, then atomically commit the manifest/current pointer with revision in PostgreSQL. Stop/reconcile any surviving executor and in-flight broker work before rotating generation and restoring a replacement. Compatible context/environment and known results resume automatically; unknown external outcomes wait for the selected owner decision. Never zero consumed/reserved usage on restart.

Cancellation closes dispatch, signals worker, then forcibly terminates after a documented bounded grace. Remain stopping until executor death is confirmed; preserve completed when it wins. Finish/reconcile sent broker requests separately from the canceled container.

- [ ] **4. Verify the gate and parent-commit.** Run both actual-container suites and B3/B4 checks, retaining compact container/network/cgroup evidence without sensitive payloads. Explicitly fail if a check is skipped for unsupported driver, storage build, missing image scan, unsupported runtime continuation or unknown state. Commit `feat: enforce isolated checkpointed run execution` only with a truthful gate record. Parent stops dependent waves if this tranche fails.

### B6 — Paper workflow, research resources and REST event delivery

**Owns:** `backend/src/scientist/{research,api}.py`; `backend/tests/test_research.py`; `backend/tests/test_rest_workflow.py`; parent integrates requested domain resource additions, mounts routes in `app.py` and regenerates shared contracts after this task. No edits to protocol adapter files.

**Dependencies:** B5; concrete broker, objects, domain contracts.

**Interfaces:** `research.build_plan(db, owner: Principal, run_id: UUID, search_terms: list[str]) -> PlanSpec`; `research.verify_citation(record: dict, retrieved_metadata: dict) -> dict`; `api.router` supplies the REST endpoints below. Add concrete shared domain resource functions in the existing `domain.py` through parent integration: `list_projects(db, principal: Principal, after: UUID | None, limit: int = 100) -> list[ProjectView]`, `list_artifacts(db, principal: Principal, run_id: UUID) -> list[ArtifactView]`, and `get_citation(db, principal: Principal, citation_id: UUID) -> CitationView`. Domain functions remain the single policy source used later by protocol adapters.

- [ ] **1. Write citation and full REST acceptance checks.** Fixture metadata includes a real-shaped DOI record, an abstract-only record, contradictory evidence and a nonexistent identifier, explicitly labeled test data:

```python
def test_unretrieved_doi_is_not_verified():
    result = verify_citation({'doi': '10.0000/example'}, {})
    assert result['verification'] == 'unverified'
    assert result['original_url'] is None

def test_abstract_is_not_presented_as_full_text():
    result = verify_citation({'title': 'Fixture'}, {'abstract': 'Text', 'access': 'abstract'})
    assert result['access'] == 'abstract'
```

Add a REST flow: create project/session, prepare file, explicitly select it, submit/revise/approve plan, receive real fixture-worker stage events, retain partial results, publish chosen output, save/remove finding, and open provenance. Test immutable running input after instruction/file changes, list/download cross-project denial, stale publication, retried run lineage, expired cursors and reconnect without duplicate messages. `client` fixture is an actual FastAPI TestClient with the B2 owner/session and isolated resources, not a policy-bypassing client.

- [ ] **2. Observe RED.** Run `rtk proxy uv run pytest backend/tests/test_research.py backend/tests/test_rest_workflow.py -q` with no paid calls.

- [ ] **3. Implement the exact initial REST surface.** Mount authenticated routes at `/api/v1`:

| Route | Operation |
|---|---|
| `GET /capabilities` | File formats/limits, supported protocol bindings and safe configuration state |
| `GET/POST /projects` | List/create granted projects |
| `GET/PATCH /projects/{project_id}` | Read/edit instructions using expected revision |
| `GET/POST /projects/{project_id}/sessions` | Separate chat sessions |
| `GET /sessions/{session_id}/messages` | Authorized session messages |
| `GET/POST /projects/{project_id}/files` | List or upload with bounded streaming |
| `GET /projects/{project_id}/files/{file_id}/content` | Authorized safe original-file download/preview |
| `DELETE /projects/{project_id}/files/{file_id}` | Owner tombstone with reference checks |
| `GET/POST /projects/{project_id}/findings` | List/save explicit findings with evidence provenance |
| `DELETE /projects/{project_id}/findings/{finding_id}` | Owner removes future shared context only |
| `GET /projects/{project_id}/sources` | Authorized source/citation collection |
| `GET /citations/{citation_id}` | Exact authorized CitationView provenance/access record |
| `POST /sessions/{session_id}/runs` | Deduplicated immutable submission |
| `GET /projects/{project_id}/runs` | Authorized run history |
| `GET /runs/{run_id}` | Consistent snapshot/cursor and safe error category |
| `GET /runs/{run_id}/plan` | Owner reviewable PlanView with exact revision/digest |
| `PATCH /runs/{run_id}/plan` | Owner revision-bound edits |
| `POST /runs/{run_id}/prepare-plan` | Owner bounded planning authorization |
| `POST /runs/{run_id}/approve` | Owner plan/input digest approval |
| `POST /runs/{run_id}/stop` | Idempotent authorized stop request |
| `POST /runs/{run_id}/decisions` | Owner budget/data/unknown-outcome resolution |
| `GET /runs/{run_id}/events?after={sequence}` | Persisted SSE replay and future events |
| `GET /runs/{run_id}/event-page?after={sequence}` | Bounded cursor fetch for protocol tools |
| `GET /runs/{run_id}/artifacts` | Scoped metadata |
| `GET /artifacts/{artifact_id}/content` | Authenticated safe streaming download |
| `POST /runs/{run_id}/publish` | Selected, deduplicated version publication |
| `GET/POST/DELETE /connections` | Owner masked credential configuration |
| `POST /connections/{connection_id}/check` | Owner explicit bounded readiness test |
| `GET/POST/DELETE /access-tokens` | Owner grant administration; create displays token once |
| `GET/POST/PATCH/DELETE /peers` | Owner configured A2A endpoints/scope |

Connection checks distinguish unconfigured/checking/ready/invalid credentials/unavailable provider/unavailable model; sanitize responses. Basic local bearer grant administration is implemented here; A2A actual peer checking arrives in I2. Research stages come from approved plan: search literature, verify references, synthesize evidence for the initial workflow. Execute scholarly queries through the broker, retain accessible source metadata/URLs/provenance and distinguish unread full text. Synthesis uses only approved evidence/content; verification checks identifiers against retrieved records and marks contradictions/missing evidence.

SSE authorizes initial replay and bounded rechecks, emits allowlisted public events and periodic non-content heartbeat, and returns fresh-snapshot resync on expired cursor. No artificial percentage, skill/runtime identity or hidden reasoning. OpenAPI and contract generator feed the frontend/protocol tasks.

- [ ] **4. Verify and parent-commit.** Run these suites and affected lifecycle/broker checks, regenerate the contract (`rtk proxy uv run python -m scientist.contracts`), inspect OpenAPI and SSE event payloads, then commit `feat: complete cited research workflow and REST events`. Live-provider acceptance remains a distinct I4 check.
