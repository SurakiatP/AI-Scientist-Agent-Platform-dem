# Protocol Access and Release Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the same owner-approved domain through scoped MCP and bidirectional A2A, then prove deployment, restore, security and live-workflow acceptance.

**Architecture:** Official SDK transports/parsers wrap concrete domain operations, with no second orchestrator or separate authorization database. External listeners admit manually configured scoped bearer clients; the local owner interface remains separate. Deployment uses explicit trusted roles, private storage/database networks and owner-supplied operator prerequisites.

**Tech Stack:** mcp 2.3.0, a2a-sdk 1.2.1, FastAPI, PostgreSQL, Docker/Compose, AIStor Free, Trivy and Syft. Versions/digests are rechecked and scanned at execution; candidate inspection is not a scan pass.

**Spec:** [MCP/A2A/storage/security specification](../specs/2026-10-03-platform-runtime-design.md) and [master wave plan](2026-10-03-platform-build.md).

## Global Constraints

All master constraints apply. Native Hermes gateways, experimental MCP Tasks/WebSockets, OAuth conformance, extra A2A bindings and remote owner cookies are not advertised. Owner decisions remain exclusive to the local UI. A2A capabilities use product-level research descriptions, without publishing the internal skill inventory.

## Review Focus

- MCP transport disconnection must not cancel durable work: I1.
- A2A CancelTask must not report terminal canceled before worker acknowledgment: I2.
- An outbound A2A accepted message with lost task ID must pause, not duplicate work: I2.
- A proxy/network listener must never elevate an external bearer to owner cookie authority: I3.
- Restored database/objects and partial scanner coverage must not silently imply readiness: I3/I4.

### I1 — Scoped MCP tools and application event cursors

**Owns:** `backend/src/scientist/mcp_api.py`; `backend/tests/test_mcp.py`; `contracts/mcp-tools.json`. Parent owns adding `mcp==2.3.0` to root dependency lock and app route/lifespan composition after this task.

**Dependencies:** B2/B5/B6 completed; full domain/API functions are available before this wave. No edits to B6 or frontend files.

**Interfaces:** `create_mcp_app() -> ASGI application` using the pinned SDK; adapter tools call B2/B6 domain operations with the authenticated Principal and a per-request database session. The SDK middleware maps a verified scoped bearer into a principal; it cannot create an owner session. Tool names/arguments/results:

| Tool | Arguments | Result |
|---|---|---|
| `list_projects` | bounded page cursor | granted ProjectView records |
| `attach_input` | project_id, filename, declared size/content type, bounded supported content | FileView/upload status; same parser limits as REST |
| `submit_research` | project_id, session_id, submission_key, question, input_ids, provider_id, model | RunView; no execution before owner approval |
| `get_research` | run_id | authorized RunView |
| `get_research_events` | run_id, after, bounded limit | events plus cursor or explicit cursor_expired |
| `list_results` | run_id | scoped ArtifactView records |
| `request_stop` | run_id | authoritative RunView with pending stop when applicable |

Artifact resources use authenticated application artifact identities, not S3 credentials/keys. Do not add arbitrary fetch/shell, owner approve, publish or secret-administration tools. Bound encoded attachment size before decoding and stream ordinary file uploads through the domain rather than letting a caller nominate a host path.

- [ ] **1. Write real SDK transport checks.** Use the pinned MCP client against a loopback task-specific test app with real bearer policy/domain fixture. Include unauthorized listing, cross-project IDs/cursors/resources, revoked token during a stream, malformed tool schemas, removed experimental feature negotiation and request cancellation. Domain persistence check:

```python
async def test_mcp_submission_remains_after_client_disconnect(mcp_fixture):
    run_id = await mcp_fixture.submit('stable-key')
    await mcp_fixture.disconnect()
    view = mcp_fixture.domain_run(run_id)
    assert view.state == 'awaiting_approval'
    assert mcp_fixture.provider_calls == 0
```

`mcp_fixture` is defined in this test module: official SDK client/server, isolated B1 DB, scoped external token, and deterministic provider/broker; it uses no owner credential in tool calls. Replay the same submission key and assert the same run, then changed content gives conflict.

- [ ] **2. Observe RED and pin compatibility.** Parent adds exact SDK candidate and resolves a compatible audited lockfile. Run `rtk proxy uv run pytest backend/tests/test_mcp.py -q`; initial missing adapter/tool behavior fails. If the pinned transport no longer matches the inspected specification, document the source discrepancy before changing advertised support.

- [ ] **3. Implement SDK mounting and thin domain wrappers.** Register these tools and resources using SDK-provided schemas/validation, and use its Streamable HTTP application at `/mcp`. Transport requests use POST; do not build standalone GET replay or protocol session semantics removed in this version. A synchronous domain wrapper has this shape:

```python
def get_research(db, principal: Principal, run_id: UUID) -> RunView:
    return get_run(db, principal, run_id)
```

The adapter supplies `db` and `principal` from validated request context, never accepts them as caller-controlled tool arguments. Use **MCPServer**, not the removed FastMCP interface. The pinned SDK's verified mounting pattern is:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from mcp.server import MCPServer
from starlette.routing import Route

mcp = MCPServer('scientist-platform')
mcp_app = mcp.streamable_http_app(streamable_http_path='/mcp')

@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield

app = FastAPI(lifespan=lifespan)
app.router.routes.append(Route('/mcp', endpoint=mcp_app))
```

This excerpt is route composition, not the full secured application; parent merges it with the authentication wrapper and other lifespans. Create the streamable app before accessing its session manager. Mounting alone does not execute nested lifespan. Tool registration uses `@mcp.tool()` or `mcp.add_tool(fn, name=...)`. A mount at `/mcp` with child path `/` actually exposes `/mcp/` and redirects the unslashed URL; an exact ASGI Route preserves the selected `/mcp` endpoint. Test POST with redirects disabled and ensure it is not `/mcp/mcp`. This initial bearer profile forwards only the endpoint and claims no OAuth discovery routes. [Pinned SDK app source](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/src/mcp/server/lowlevel/server.py) and [SDK mounting example](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/examples/stories/starlette_mount/server.py).

Short submission/status/event-fetch tools return promptly; long research execution is the durable supervisor's responsibility. Encode domain failures as safe tool errors. Application cursors, not Last-Event-ID MCP sessions, provide replay. Revalidate grants on resource/download and stream boundaries. Publish the bearer profile honestly without claiming OAuth discovery.

- [ ] **4. Verify and parent-commit.** Run real SDK tests and shared auth/lifecycle checks. Inspect advertised initialization/tool/resource schemas against `contracts/mcp-tools.json`; parent integrates routes/lifespan and commits `feat: expose scoped scientific workflows through MCP`.

### I2 — A2A v1 inbound tasks and approved outbound peers

**Owns:** `backend/src/scientist/a2a_api.py`; `backend/tests/test_a2a.py`; `contracts/a2a-profile.json`. Parent adds `a2a-sdk==1.2.1`, mounts routes and integrates broker peer-dispatch changes requested by this task sequentially.

**Dependencies:** B5/B6; caller grants, effect ledger and stable domain task IDs. No MCP dependency; I1 and I2 can run in parallel after parent resolves both SDKs in one lockfile update.

**Interfaces:** `create_a2a_app() -> ASGI application`; `task_from_run(run: RunView) -> SDK Task`; `send_peer(db, owner: Principal, run_id: UUID, peer_id: UUID, approved_payload: dict, operation_id: str) -> OperationResult`; `check_peer(db, owner: Principal, peer_id: UUID) -> dict`. `send_peer` goes through B3 `execute(kind='peer')`; it never independently sends HTTP or forwards inbound credentials. `check_peer` returns verified binding/version/security metadata without approving data release.

- [ ] **1. Write real v1 lifecycle and peer-loss tests.** Exercise exact methods SendMessage, SendStreamingMessage, GetTask, ListTasks, CancelTask and SubscribeToTask using the pinned SDK models/client. Cover foreign context/task IDs, forged approve messages, version/member discriminators, snapshot-first subscriptions, terminal GetTask, disconnected stream and revoked grants. Core domain mapping:

```python
def test_pending_stop_is_not_terminal(a2a_fixture):
    task = a2a_fixture.cancel_hung_run()
    assert task.status.state != a2a_fixture.canceled_state
    a2a_fixture.confirm_executor_exit()
    assert a2a_fixture.get_task().status.state == a2a_fixture.canceled_state

def test_lost_peer_task_id_waits_for_owner(a2a_fixture):
    result = a2a_fixture.send_to_peer_that_loses_response()
    assert result.state == 'unknown'
    a2a_fixture.recover()
    assert a2a_fixture.peer_submission_count == 1
    assert a2a_fixture.run().waiting_reason == 'unknown_outcome'
```

`a2a_fixture` uses an isolated SDK peer server, actual domain/broker/authorization and a recorded approved payload; v1 enum constants come from that pinned SDK. Test completion winning cancellation, changed-body messageId replay and a peer redirect to an unapproved authority. No real third-party peer or file content is used.

- [ ] **2. Observe RED.** Parent pins the compatible SDK and audited lock, then run `rtk proxy uv run pytest backend/tests/test_a2a.py -q`.

- [ ] **3. Implement the thin RequestHandler and persistent mapping.** Use the SDK parser/FastAPI route factories, not removed application wrapper classes, its default in-memory run registry or premature terminal cancellation path. `create_a2a_app` constructs a FastAPI app with authenticated handler context and these verified factories:

```python
from a2a.server.routes import (
    add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes,
)

add_a2a_routes_to_fastapi(
    app,
    agent_card_routes=create_agent_card_routes(agent_card),
    jsonrpc_routes=create_jsonrpc_routes(request_handler, rpc_url='/a2a'),
)
```

`app`, `agent_card` and `request_handler` are the application/card/domain handler constructed by this task, not user-supplied values. No REST/gRPC routes or v0.3 compatibility are enabled. [Pinned FastAPI route helper](https://github.com/a2aproject/a2a-python/blob/v1.2.1/src/a2a/server/routes/fastapi_routes.py). Core RequestHandler methods use protobuf types from `a2a.types.a2a_pb2`:

```python
async def on_get_task(self, params: GetTaskRequest, context: ServerCallContext) -> Task | None:
async def on_list_tasks(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
async def on_cancel_task(self, params: CancelTaskRequest, context: ServerCallContext) -> Task | None:
async def on_message_send(self, params: SendMessageRequest, context: ServerCallContext) -> Task | Message:
async def on_message_send_stream(self, params: SendMessageRequest, context: ServerCallContext) -> AsyncGenerator[Event]:
async def on_subscribe_to_task(self, params: SubscribeToTaskRequest, context: ServerCallContext) -> AsyncGenerator[Event]:
```

These are signatures from the [pinned interface](https://github.com/a2aproject/a2a-python/blob/v1.2.1/src/a2a/server/request_handlers/request_handler.py); streaming implementations must actually yield SDK Event objects. Implement required abstract push-configuration/extended-card methods as unsupported when those capabilities are disabled, rather than failing handler instantiation. Map each v1 request to the B2/B6 domain and a caller-scoped task/message ID. `A2A-Version: 1.0`; `/a2a` JSON-RPC+SSE; `/.well-known/agent-card.json` advertises only this binding, bearer access requirements and product research capabilities.

Import `ServerCallContext` from `a2a.server.context`, these protobuf models from `a2a.types.a2a_pb2`, and `UnsupportedOperationError` from `a2a.utils.errors`. The concrete handler supplies all remaining abstract methods:

```python
async def on_create_task_push_notification_config(self, params: TaskPushNotificationConfig,
        context: ServerCallContext) -> TaskPushNotificationConfig:
    raise UnsupportedOperationError

async def on_get_task_push_notification_config(self, params: GetTaskPushNotificationConfigRequest,
        context: ServerCallContext) -> TaskPushNotificationConfig:
    raise UnsupportedOperationError

async def on_list_task_push_notification_configs(self, params: ListTaskPushNotificationConfigsRequest,
        context: ServerCallContext) -> ListTaskPushNotificationConfigsResponse:
    raise UnsupportedOperationError

async def on_delete_task_push_notification_config(self, params: DeleteTaskPushNotificationConfigRequest,
        context: ServerCallContext) -> None:
    raise UnsupportedOperationError

async def on_get_extended_agent_card(self, params: GetExtendedAgentCardRequest,
        context: ServerCallContext) -> AgentCard:
    raise UnsupportedOperationError
```

Verify unsupported requests return a defined safe protocol error and that the handler actually instantiates; no push-notification/extended-card capability is advertised.

```python
DOMAIN_TO_A2A = {
    'queued': 'TASK_STATE_SUBMITTED',
    'running': 'TASK_STATE_WORKING',
    'recovering': 'TASK_STATE_WORKING',
    'waiting_input': 'TASK_STATE_INPUT_REQUIRED',
    'awaiting_approval': 'TASK_STATE_INPUT_REQUIRED',
    'completed': 'TASK_STATE_COMPLETED',
    'failed': 'TASK_STATE_FAILED',
    'canceled': 'TASK_STATE_CANCELED',
    'rejected': 'TASK_STATE_REJECTED',
}
```

`planning` is submitted/working according to whether bounded owner-authorized preparation is active. `stopping` remains working with a safe pending-stop message until acknowledgment. Convert this semantic mapping using the pinned SDK enum, not guessed 0.3 wire strings. Owner approval is input-required rather than auth-required. Subscriptions begin with the current authoritative task; completed/failed/canceled tasks are retrieved rather than promising subscription history.

Outbound peer requests require exact approved recipient/payload/input versions/limits, configured endpoint credentials and explicit LAN destination policy. Persist message/operation IDs before dispatch and returned task ID afterward. Reconcile by remote task ID or an explicit peer agreement. A lost remote acceptance without recoverable task identity follows the chosen unknown-outcome policy; no standard exactly-once claim. Artifact URLs resolve through the scoped application gateway. Incoming bearer values never become outbound peer/provider values.

- [ ] **4. Verify and parent-commit.** Run SDK/client/peer tests plus broker/cancellation regression checks. Parent integrates actual peer check/dispatch and Settings capability metadata, updates contract profile and commits `feat: add approved bidirectional A2A task access`.

### I3 — Local deployment, operator readiness and coordinated restore

**Owns:** `deploy/{compose.yaml,Dockerfile.api,README.md}`; `backend/src/scientist/{launch,backup}.py`; `backend/tests/test_deployment_restore.py`. Parent owns application role composition and host integration. Secret/license/backup files are not created in source or delegated to workers.

**Dependencies:** B1–B6/F1–F4/I1/I2 integrated. Actual fixed engine/license/encrypted storage are required for live deployment acceptance.

**Interfaces:** `launch.preflight() -> dict` validates compatible engine, image references, secret mounts, free license readiness, encrypted storage operator declaration and allowed network profile; `launch.owner_url() -> None` opens the one-time local bootstrap without logging it; `backup.create(destination: Path) -> dict` quiesces dispatch/writers and records a coordinated manifest; `backup.restore(source: Path) -> dict` restores into an explicitly isolated target and validates references before dispatch. No arbitrary host-path/API invocation of backup or Docker controls.

- [ ] **1. Write readiness/restore rejection checks.** Test missing license/master key, storage permission/expiry failure, host affected version, invalid image digest, incomplete backup, wrong hashes, pending unknown operations and empty/partial restore:

```python
def test_restore_missing_object_does_not_dispatch(restore_fixture):
    backup = restore_fixture.make_backup_with_checkpoint()
    restore_fixture.remove_referenced_object(backup)
    with pytest.raises(ValueError, match='missing_object'):
        restore_fixture.restore(backup)
    assert restore_fixture.started_workers == 0
```

The fixture restores a real test PostgreSQL database and task-specific AIStor bucket/volume into a separate isolated Compose project. Host reboot is not required; stop/restart this test project to simulate full deployment loss. Do not touch the user's other containers or research data.

- [ ] **2. Observe RED and verify operator prerequisites.** Run `rtk proxy uv run pytest backend/tests/test_deployment_restore.py -q`. Check fixed/mitigated Docker, exact scanned images and mounted license readiness; do not execute default insecure examples or vendor signup/acceptance. A missing prerequisite is a blocked integration check. Owner acquisition of AIStor Free license is required before software download/install according to its terms.

- [ ] **3. Implement one documented deployment profile.** Compose roles: local-owner API/static frontend, trusted supervisor, broker, PostgreSQL and AIStor Free. A single application image may serve API/broker/supervisor entrypoints but only the supervisor receives Docker control. Workers and preparation containers are launched dynamically per run with bounded workspace volumes and internal networks. Put S3/database on a private service network, attach broker deliberately to run networks, disable packet forwarding/capabilities, and verify no cross-run route. Do not expose database/storage administration to workers.

Publish the owner UI only on `127.0.0.1`; separate external protocol listener/app composition never accepts owner cookies/bootstrap/admin routes. Optional LAN profile requires explicit bind address/TLS reverse proxy/allowed hosts/origins/scoped bearer grants; advertise no remote owner UI. Trust proxy headers only from explicitly configured proxy IPs. Production serves the built frontend and SPA fallbacks from the API origin; Vite is development-only.

Mount provider master key, generated service credentials and AIStor license outside source, read-only where possible. Store actual data on encrypted operator volumes, and backups on an encrypted destination. No `minioadmin`, default Postgres passwords or shared credentials; generation does not print values. Use app-level immutable keys and PG versions, no paid SSE/version-specific delete assumption. Surface license renewal/expiry/read-only/unavailable states.

Backup: pause dispatch, settle/quiesce writers, capture PG plus every referenced immutable object and manifest hash, persist pending operations/usage/grants, then resume only after successful completion or safe rollback of the backup operation. Restore checks DB/object/manifest compatibility and refuses execution until all required references validate. Unknown remote effects remain pending decisions. Document exact local start/stop/restore commands and operator-supplied secret file paths, without sample secret values.

- [ ] **4. Verify and parent-commit.** Run the deployment/restore suite, `rtk proxy docker compose -f deploy/compose.yaml config --quiet`, actual PUT/GET/ordinary DELETE and loopback/external privilege tests, then start an isolated full stack to verify frontend/REST/MCP/A2A. Capture safe readiness evidence. Commit `feat: add secure local deployment and coordinated restore`.

### I4 — Security evidence, full acceptance and final review

**Owns:** `scripts/verify-release.py`; `.github/workflows/verify.yml`; `docs/validation/2026-10-03-platform-acceptance.md`; `.gitignore` additions for generated evidence. Parent owns aggregate fixes; verification outputs go to ignored `.local/security/` and `.local/acceptance/` directories.

**Dependencies:** All earlier tasks. This is verification/tooling, not another feature wave.

**Interfaces:** `scripts/verify-release.py` is a bounded command runner with explicit target images/Compose project and no production targets. It records exit status, scanner/package/image versions, lock hashes, SBOM/advisory coverage and required test results, exiting nonzero on a failure or missing required check. It never reads/exposes secret file values or treats a skipped scanner/live run as PASS.

- [ ] **1. Prepare exact scanner evidence and prove failure handling.** Read-only preparation found Trivy `v0.75.0` and Syft `v1.54.0` as latest official release tags on 2026-10-03. Resolve their official signed/checksummed distributions/digests into a separate tooling environment at execution, recheck advisories and record exact versions. Do not use mutable scanner images without recording their digest. Self-check the runner with a harmless failing subprocess:

```python
completed = subprocess.run([sys.executable, '-c', 'raise SystemExit(7)'],
                           capture_output=True, text=True, check=False)
assert completed.returncode == 7
```

The runner must preserve that nonzero result as a failed check. CI performs offline deterministic domain/frontend/protocol checks and scans; local licensed-container/live-LLM checks are separate and must appear as pending when CI cannot run them.

- [ ] **2. Run appropriate aggregate verification once.** Commands executed by the runner start with `rtk`:

```bash
rtk proxy uv run pytest backend/tests -q
rtk npm --prefix apps/web run typecheck
rtk npm --prefix apps/web run build
rtk npm --prefix apps/web run test:e2e
rtk proxy npm --prefix apps/web audit --json
rtk git diff --check
```

Test selection separates deterministic checks from licensed/actual-container tests clearly; none are silently skipped in the final acceptance report. Recheck generated DTOs/OpenAPI/tool schemas against the committed files. Run actual component-image SBOM and scans using the installed verified tooling, with bounded files rather than noisy console dumps:

```bash
rtk proxy syft scan --from docker --output cyclonedx-json --file .local/security/runtime.sbom.json scientist-runtime:review
rtk proxy trivy image --scanners vuln --severity HIGH,CRITICAL --exit-code 1 --format json --output .local/security/runtime.vulns.json scientist-runtime:review
```

The named tag is a local build whose recorded immutable image ID/digest appears in the report; repeat for API, database, AIStor and required scanner/build images. Supply the exact AIStor/database digest from deployment configuration, not a mutable latest tag. Validate coverage includes OS packages, installed Python/runtime dependencies and frontend/build lockfiles. Inspect current advisories and applicability; patch unmitigated applicable critical/high findings. A database/scanner outage or unsupported package ecosystem is an explicit evidence gap.

- [ ] **3. Prove the live paper workflow and redact evidence.** Owner configures a selected provider key through the actual local Settings UI; do not request the key in chat. Run one explicit small paper question under a visible approved token/time ceiling. Check real scholarly source metadata, citation identifiers and access labels, approve/review stages, preserve partial outcomes where relevant, publish one selected result, and follow a citation to its known source. Record provider/model identifier, configured ceiling, actual reported usage, source retrieval dates and outcome without question/private file/secret payloads. No third-party paid analyses or peers are called without their explicit data/scope approval.

Compare actual TH/EN, all appearance modes, responsive/keyboard/contrast/reduced-motion, browser Back/Forward and expanded artifact state against DESIGN.md. Include safe screenshots. List any product limitation or missing prerequisite honestly; fixture completion alone is not a fully accepted scientific workflow.

- [ ] **4. Parent commits evidence; independent reviewer returns PASS/FAIL.** Save concise dated validation evidence and reproducible commands, commit `test: verify platform acceptance and dependency security`, then dispatch the final reviewer as gpt-6.1-sol/high with original specs/plans and final diff. Require it to inspect aggregate behavior, execute relevant verification and state explicit PASS or FAIL with gaps. On FAIL, repair only authorized scope or request the concrete missing decision. On PASS, report branch/results and prepare integration into develop; do not push or merge before the completed work is reviewed as requested by the owner.
