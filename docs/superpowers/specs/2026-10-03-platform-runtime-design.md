# AI Scientist Agent Platform — Runtime and Integration Design

Date: 2026-10-03 (Asia/Bangkok)

Status: Written specification approved by the owner on 2026-10-03. The owner selected MinIO AIStor Free single-node and pausing only an unknown-outcome step for an owner decision. Implementation-plan review remains outstanding. Runtime feasibility and acceptance must be demonstrated during implementation; no product implementation or deployment is authorized by this document alone.

## Scope and authoritative documents

The [backend foundation](2026-10-03-backend-foundation-design.md) records the approved purpose, workflow and policies. [DESIGN.md](../../../DESIGN.md) governs presentation and frontend behavior. This document defines the operational contracts needed to implement that foundation; it does not replace the frontend specification.

Build the first complete paper-search and evidence-synthesis workflow, including project/session management, plan approval, isolated concurrent runs, bounded execution, recovery, publication, REST, inbound MCP, and bidirectional A2A. Preserve the broader multidisciplinary direction without claiming every scientific workflow has been validated.

Initial deployment has one local owner and no login page. LAN/server exposure is an explicit deployment profile, with authenticated external callers; the owner interface stays local. Public multi-owner hosting requires an additional identity/authorization design before exposure.

## Components and ownership

Use React/TypeScript/Vite for the frontend and a Python 3.13 application package for FastAPI, domain operations, protocol adapters, the supervisor and the outbound broker. Use separate processes where privileges differ, with one package and shared domain functions. PostgreSQL supplies durable state and the initial queue; a separate queue service is unnecessary for this workload.

| Component | Responsibility | Authority |
|---|---|---|
| Web/API | Owner UI, scoped REST/MCP/A2A adapters, validation | Domain operations; no arbitrary shell execution |
| Supervisor | Claim queued work, launch/stop/recover run containers | Docker control; this is a trusted host-level component |
| Broker | Execute approved LLM, scholarly, package and peer requests | Selected credentials and outbound policy; no owner approval authority |
| Run worker | Pinned runtime, reviewed skills, local computation | Its immutable inputs and writable run workspace only |
| PostgreSQL | State, grants, approvals, queue leases, operation ledger, budgets, events | Authoritative application records |
| AIStor Free | Input/output/checkpoint bytes under immutable keys | Storage service only; no public bucket or worker credentials |

Docker access belongs only to the supervisor. Neither the public API nor a worker receives the Docker socket. The supervisor's Docker authority is equivalent to host administration and must not be exposed as a public API.

## Suggested repository structure

Create directories only when their first implementation lands:

```text
apps/web/                    React routes, components, styles and locale messages
backend/src/scientist/        API, domain, persistence, broker, supervisor, protocols
backend/tests/               Domain/security/integration acceptance checks
backend/migrations/          PostgreSQL migrations
runtime/                     Worker entrypoint and reviewed skills manifest
contracts/                   Versioned application event and interoperability schemas
deploy/                      Local container configuration and image references
docs/superpowers/specs/      Written design sources
docs/superpowers/plans/      Reviewed implementation plans
DESIGN.md                    Frontend source of truth
.integration-hub/            Ignored local operational memory
```

One backend package is sufficient; no independently versioned microservices or generic plugin framework. Keep production data, environments, license files, generated artifacts and credentials outside Git. Maintain Python and JavaScript lockfiles and immutable deployment image references.

The existing `.venv` reports Python 3.9.6, while `.python-version` selects 3.13.6. Recreate the environment with the selected compatible interpreter during the approved implementation setup; do not treat the existing virtual environment as runtime-ready. The inspected Hermes revision declares Python `>=3.11,<3.15`.

## Durable records and concurrency

PostgreSQL records projects, sessions, messages, findings, logical file versions, runs, input snapshots, plan revisions, approvals, limits, operation results, checkpoint manifests, run events, caller grants and peer delegation references. Use database constraints for ownership, uniqueness and valid references.

A run belongs to one project and session. The input snapshot identifies exact file/finding/instruction versions and the selected conversation prefix. A plan digest binds that snapshot, provider/model, tool operations, data recipients, packages and limits. Approval records bind the digest and owner decision. Scope changes create a new revision and invalidate the previous approval.

Submission deduplication uses a unique `(caller_identity, submission_key)` constraint and a canonical request fingerprint. Replaying the same key and body returns the original run. A different body with the same key returns a conflict. Retain the key for the retained lifetime of the run.

The supervisor claims ready work with PostgreSQL row locking and `SKIP LOCKED`, bounded by configured concurrency and available resources. Each lease has an expiry and increasing fencing generation. The broker rejects expired or superseded generations. A recovery supervisor first stops/reconciles a surviving container and in-flight broker operations before issuing a new generation; two workers cannot execute the same run concurrently.

Publication is an owner operation: select validated run artifacts, then transactionally create new logical project versions with provenance. Originals remain unchanged. Concurrent publication allocates distinct versions or returns a conflict for a supplied expected project revision.

## Run lifecycle, decisions and stopping

Use domain states `planning`, `awaiting_approval`, `queued`, `running`, `waiting_input`, `recovering`, `stopping`, `completed`, `failed`, `canceled`, and `rejected`. Waiting reasons identify budget, required data approval, and any chosen unknown-outcome decision. Presentation uses localized scientific stages independently of these technical states.

Only the owner can approve/revise plans, extend limits, approve additional data release, publish shared results, and administer credentials/grants. An external caller may cancel its own authorized submission when granted that operation. Disconnecting any client does not cancel a durable run.

Plan preparation is distinct from research execution. An owner submission may authorize a bounded planning-only call to the selected model under a visible configured planning allowance; the broker permits only that model request and selected input scope, with the same ledger and usage controls. No research tool, package installation or peer dispatch is allowed before plan approval. An external submission initially produces a non-executing draft/request; it cannot spend provider allowance until the owner authorizes plan preparation. Record planning usage separately and include it in the displayed total.

A stop request prevents new operations and moves a nonterminal run to `stopping`. Signal the worker, allow a bounded grace period, then terminate its container if necessary. Report `canceled` only after the executor is stopped and no further work can be scheduled. Already sent external work may require reconciliation; do not claim cancellation undoes remote effects. Preserve `completed` when completion wins the transaction race.

Errors retain verified partial outputs and a public actionable error category. Retrying a terminal failed/canceled run creates a new run linked to its predecessor and requires approval for its bound scope. Automatic recovery of interrupted active runs retains the existing run identity.

## Runtime containment and controlled effects

Pin Hermes to commit `bd0affe5e5f723579df8902852f5d0c47795f355` as the inspected candidate. Instantiate `run_agent.AIAgent` inside each run worker with a private `HERMES_HOME`. Its private SQLite persistence may exist within that worker; it is not the platform database and is not authoritative for approval or recovery.

Use a non-root container with a read-only root filesystem, read-only input mount, bounded writable workspace, dropped capabilities, no privileged mode, no host mounts, and enforced CPU/memory/PID/disk limits. Check actual engine enforcement before execution; reject a run if required limits cannot be enforced. Use a dedicated bounded workspace volume/filesystem when the storage driver cannot enforce a container disk limit.

Workers use an isolated internal network, with no direct internet, host/LAN, PostgreSQL, storage, metadata-service or Docker access. A dedicated broker endpoint is their only authorized network interface. Validate isolation with direct IPv4/IPv6, DNS, redirects and alternate endpoints; a runtime profile is not proof of containment.

Hermes hooks/middleware can report telemetry but are fail-open in the inspected revision. Authorization, durable effect recording and budget reservation therefore belong to the platform broker, which fails closed. A runtime adapter restricts exposed tools and routes approved effects through this broker. A worker's scoped, short-lived capability is bound to its run and fencing generation; it grants no general public API access.

The broker checks the approved research plan, or the narrowly authorized planning-only scope, before every operation. It persists an operation identity and reserves usage before sending the request. Failure to persist authorization, budget or ledger state blocks the effect. A repeated operation identity with a different payload fails. Persist the response/result reference before returning it to the worker.

Allow local computation and file writes only inside the run workspace. Arbitrary shell networking is unavailable. Approved package installation uses configured, validated sources through the broker; fetched artifacts are scanned and version/hash recorded before installation in the sandbox. Package build scripts execute as untrusted code within the same containment.

Endpoint validation must reject local/private/link-local targets for ordinary scholarly/provider requests, pin validated resolution through the connection, enforce TLS, and revalidate redirects. Explicit LAN A2A peers use a separate owner-configured destination policy; a client cannot turn a supplied URL into a trusted peer. Block metadata services and redirects that change authority. Apply request-size, response-size, duration and rate limits.

## Checkpoint capture and restart recovery

Recovery means continuation from committed operation boundaries. It does not restore an arbitrary Python instruction pointer, dead process, or native filesystem rollback checkpoint.

A platform checkpoint contains schema/runtime/image/skills hashes, approved plan and snapshot digests, conversation/context needed for continuation (including compressed context), current stage, recorded tool-call and broker-operation identities, workspace manifest, artifact references, environment/package manifest, and consumed/reserved usage. Runtime-private state may supplement this checkpoint but cannot override it.

At a quiescent boundary, upload immutable workspace/context objects, verify hashes, and transactionally commit their manifest reference with the run revision and operation references in PostgreSQL. A failed object upload creates no usable checkpoint. A failed database commit leaves only unreferenced objects for later garbage collection. Never advance the checkpoint pointer before all required bytes are durable.

After restart, reconcile leases, containers and pending operations. Verify checkpoint integrity and exact compatible runtime/environment versions before restoring a replacement worker. Missing or incompatible state moves the run to an actionable waiting/failure condition; do not silently restart from the original prompt or reset its budget. Known committed results are returned from the ledger, and never executed again. Pending operations with a known provider/peer reference are queried/reconciled where supported.

**Owner-selected unknown-outcome policy:** when a provider or peer may have accepted an operation whose response was lost and reliable reconciliation is unavailable, pause that step and enter `waiting_input` with reason `unknown_outcome`. Do not automatically resend an LLM/read request merely because it is a read, and never blindly resend a state-changing request. Known-safe recovery proceeds automatically; dependent work waits while any independent work must remain within the approved plan and existing budget.

The owner can provide a verifiable result/reference, explicitly authorize a retry with possible duplicate work/cost, or stop the run. Keep the uncertain original operation and reservation in the ledger. A retry has a new identity linked to the original and requires remaining or newly approved allowance; it does not erase the original potential charge.

Never claim exactly-once remote effects from a database transaction, Hermes run-creation idempotency, or A2A message ID alone. No blind retry of a remote state change. Recovery retains outstanding reservations conservatively until reconciled or resolved by an owner decision.

## Usage and time ceilings

Serialize budget reservation per run in PostgreSQL. Every LLM request reserves a conservative input/output allowance before dispatch, constrains output tokens, and records reported usage afterward. Parallel requests cannot spend the same remaining allowance. Failed/unknown requests keep a conservative reservation until reconciled; do not treat missing usage as zero.

Enforce approved elapsed-execution and LLM-usage limits across recovery. Waiting for owner input is recorded separately; waiting does not reset consumed time. At a ceiling, prevent new operations, preserve partial results and enter `waiting_input`. An extension is a recorded owner decision. Provider pricing estimates must disclose uncertainty; a token/usage ceiling is not a guarantee against every external billing difference. Optional paid non-LLM services need an explicit approval and separate limit rather than being hidden inside the LLM allowance.

## Storage, credentials and deployment prerequisites

Use **MinIO AIStor Free single-node**, selected by the owner, rather than the archived Community distribution. The inspected Community advisory affects all releases through the final release; the named fix is AIStor `RELEASE.2026-04-11T03-20-12Z`.

Verified distribution candidate: `RELEASE.2026-09-19T17-05-25Z`, advertised by the official release API on 2026-10-03. OCI index reference:

```text
quay.io/minio/aistor/minio@sha256:107cf2014a9583c74c11e3cdbd6903d89c2244355886b89ce532f4ecae9c23f3
```

This is an inspected candidate, not an installed or scanned image. Resolve release notes/hotfixes and rescan exact artifacts before deployment.

The owner obtains the Free license from the vendor and accepts its terms directly. Do not download/install the software, submit the acquisition form, or accept vendor terms on the owner's behalf during design. Mount the license read-only into storage only; never send it to workers, source, logs or local Hub. Reference the official image in deployment instructions rather than bundling/redistributing the vendor software.

Free excludes native SSE/KMS encryption, replication, lifecycle transitions and version-specific deletion. Use application-level logical versions in PostgreSQL bound to immutable object keys and SHA-256 hashes. Do not enable native object versioning as a required dependency. Ordinary authorized PUT/GET/DELETE operates on those immutable keys; owner deletion first creates a logical tombstone, then deletes only unreferenced objects after retention checks.

Require an encrypted operator-managed host volume for real research data; do not label this native S3 encryption. Backups include both database and object bytes, encrypted separately. License availability, allowed features, renewal and expiry affect readiness; do not assume unlimited offline operation. Surface storage unavailable before accepting execution if S3 access is blocked.

Storage uses generated credentials, private buckets and loopback/private-service administration. Workers never receive S3 keys or unrestricted object URLs. Downloads use the authenticated application gateway, with project/run authorization on every request and safe filenames/content types. Treat uploaded archives and reports as untrusted: reject path traversal, links escaping the workspace, decompression bombs and executable HTML previews. Keep downloaded research content outside the application origin when rendered.

Store provider/peer secret values encrypted using a vetted library with a master key outside PostgreSQL and Git, obtained from the local OS key store or an operator-mounted secret in a server profile. Missing master key blocks credential use; no silent plaintext fallback. Key-entry UI returns only masked metadata after save. Secrets never enter prompts, worker environments, artifacts, events or logs. Inbound platform tokens are generated with strong randomness, displayed once, stored as hashes, independently revocable, and never forwarded upstream.

For backup, stop new dispatch, quiesce writers and reconcile in-flight operations; capture PostgreSQL and referenced immutable objects as one documented recovery set. Test restoration into a fresh isolated environment, including pending operations, consumed usage, grants and missing-object detection. Backup does not imply automatic remote-effect replay.

## Owner and external authentication

No login page does not mean an unauthenticated network owner interface. Use loopback-only owner endpoints with Host/Origin validation, a local bootstrap handoff to an HttpOnly SameSite owner session, and CSRF protection for mutations. Do not distribute a bootstrap value in public static assets or keep it in query strings/logs. An external bearer token never creates an owner session.

External tokens grant explicit projects and separate `project:read`, `file:attach`, `work:submit`, `result:read`, and `work:cancel` permissions. Check grants on lists, IDs, snapshots, events, subscriptions and downloads. Attaching a file does not publish it as shared project material or confer approval. Revocation blocks new reads/effects and closes active streams at bounded authorization rechecks. Already running work remains owner-managed.

Initial MCP HTTP authentication uses manually configured scoped bearer tokens. This is not a claim of full OAuth discovery/authorization conformance. An OAuth profile and remote owner access require separate work before that deployment profile is advertised. LAN/server protocol listeners require explicit configuration, TLS (directly or at a trusted reverse proxy), restricted origins/hosts and authenticated callers.

## REST and event contract

Use `/api/v1` and OpenAPI schemas generated from typed requests/responses. Domain operations cover projects, sessions/messages, findings/instructions, selected inputs, run submission, plan review, approvals, decisions/limits, stopping, artifacts/publication, connections and external grants. Owner-only operations remain owner-only through every transport.

Run snapshots return `run_id`, `revision`, domain state, localized stage key, waiting/error category, plan/snapshot versions, usage/limits, artifact metadata and latest event cursor. Events have an increasing per-run sequence, timestamp, schema version and a validated public payload. Write state changes and their event in the same database transaction. Do not expose prompts containing private content, runtime identities, skill names, secrets or hidden reasoning as status telemetry.

The REST SSE endpoint replays persisted events after a supplied application cursor, then streams new events. Snapshot revision/cursor form a consistent baseline. An expired cursor returns an explicit resynchronization response requiring a fresh snapshot; do not pretend history was replayed. Reconnect restores UI state without duplicating messages or discarding partial outputs.

Errors use stable codes and safe user-facing messages. Object IDs do not confer access. Mutations use revision checks where a stale tab could overwrite a newer plan or project version. Cancel and decision requests are idempotent domain operations; publishing the same selection twice requires an explicit deduplication key.

## MCP profile

Inspected candidates: MCP specification `2026-07-28`, Python SDK `mcp==2.3.0`. Verify exact SDK/spec compatibility with lockfiles and protocol tests during implementation.

Mount Streamable HTTP at `/mcp` using the SDK parser/transport. The inspected version uses POST, with no standalone GET event stream, protocol sessions or Last-Event-ID replay. Do not expose the removed experimental Tasks/WebSockets or advertise an unimplemented task extension.

Use short tools for listing granted projects, attaching authorized input, submitting work, fetching run snapshots/events/results, and requesting stop. A submission returns durable run identity and approval status promptly. Application cursors supply historical events; MCP request disconnection cancels that request, not the submitted run. Artifact resources pass the same domain authorization as REST.

Do not provide tools for owner approval, credential administration, arbitrary URLs/shell execution or shared publication. Tool schemas and errors are versioned in `contracts/` and tested against the pinned SDK; procedural scientific skills do not each need an MCP wrapper.

## A2A profile

Inspected candidates: A2A specification `1.0.1`, Python SDK `a2a-sdk==1.2.1`. Select JSON-RPC with SSE only, `A2A-Version: 1.0`, at `/a2a`, with an Agent Card at `/.well-known/agent-card.json`. Advertise product-level research capabilities, not internal skill/runtime identities.

Use SDK route/parser support and a thin domain `RequestHandler`. Do not use the SDK's in-memory active-task registry as the run database or its premature terminal cancellation behavior as the platform stop contract. Hermes's native protocol gateway is not exposed independently, since it would bypass platform approval/grants.

Support exact v1 methods `SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`, `CancelTask`, and `SubscribeToTask`. Map queued work to `TASK_STATE_SUBMITTED`, active/recovering work to `TASK_STATE_WORKING`, owner decisions to `TASK_STATE_INPUT_REQUIRED`, and acknowledged terminal outcomes to their corresponding v1 task state. `stopping` remains nonterminal; completion races preserve `TASK_STATE_COMPLETED`. Owner plan rejection maps to `TASK_STATE_REJECTED`; approval is not authentication.

An A2A subscription begins with the current Task snapshot. Retrieve terminal tasks with `GetTask`; do not promise complete historical event replay through standard subscriptions. Persist caller-scoped message identity, content fingerprint and task mapping to deduplicate inbound submission.

Outbound work uses only configured peers, with owner-approved recipient, purpose, data/payload versions, allowed operations and limits. A configured peer is not blanket approval to transmit all project content. Persist operation/message IDs and any returned remote task ID. A remote task can be reconciled by ID; a lost submission with no remote ID needs an explicit peer deduplication/reconciliation agreement or the selected unknown-outcome policy. Standard A2A alone does not guarantee exactly-once submission.

## Scientific skills and evidence

Inspected catalog revision: `154988403bb5a18e9d3c0ce4e6d5e2e4b184a298`, with **177** `SKILL.md` files. This is a revision-specific catalog count, not an installed/tested capability count. Individual skill terms and external data/tool licenses require review; do not assume the whole catalog is commercially usable solely because the repository has an MIT license.

Use the proposed initial three skills `paper-lookup`, `literature-review`, and `scientific-writing`, together with platform citation verification. `citation-management` is a possible addition only if the selected workflow needs it. Preserve complete selected directories and referenced resources from the pinned revision, record hashes, and expose them only in the private worker skill directory. Disable automatic source updates. Skill installation does not install every scientific dependency.

Public scholarly metadata queries may use configured, reviewed endpoints within the approved research plan. Show the owner any sensitive question text included in outgoing queries. Only necessary approved content goes to the chosen LLM; analysis services and A2A peers receive file content only under explicit release approval. Optional Parallel/Perplexity, image generation, paid analyses and unapproved package stacks remain disabled.

Retain title/authors/year/identifier/source URLs and retrieval provenance. Verify claimed DOI/PMID/arXiv identifiers against retrieved records rather than inferring them. Label abstract-only evidence separately from accessible full text. Contradictions and missing evidence remain visible; no fabricated sources or assertion that unread full text was reviewed.

Future catalog expansion enables reviewed capabilities individually, with dependencies, licenses, endpoints and workflow acceptance checks. Loading 177 skill descriptions does not establish that all 177 workflows are usable.

## Security and acceptance gates

Source inspection is evidence for the design, not product verification. No lockfile, installed dependency tree or image scan exists yet; there is no clean-CVE claim.

Before sandbox execution, recheck the host engine. Observed Docker `29.5.2` is in the affected `<29.8.2` range for CVE-2026-92543. Use a fixed compatible engine or an explicitly validated advisory mitigation; do not update/restart the owner's shared Docker/Colima during design. Digest pinning does not prevent registry credential disclosure in that advisory.

Resolve patched compatible application dependencies, database minors and images; record lockfiles, exact digests, source licenses, SBOM and dated advisory scans. Include the actual runtime/transitive dependencies, not just direct FastAPI/npm packages. No unmitigated applicable critical/high finding may ship; a scanner outage or missing image analysis is a reported gap, not a pass.

The implementation plan must include these runnable acceptance checks:

1. Search/synthesize a bounded fixture corpus and a separately configured live scholarly query; verify citations and abstract/full-text labels. Live LLM acceptance requires an owner-configured key and bounded usage; fixture success alone is not full live acceptance.
2. Two runs in one project plus a run in another project have separate contexts/workspaces, fixed input versions and collision-free selected publication.
3. External callers cannot read another project's/task's artifacts, approve work, extend budgets or impersonate the owner through REST, MCP or A2A. Revocation, Host/Origin/CSRF and malformed protocol inputs are exercised.
4. Broker/ledger outage prevents effects. Direct internet/host/metadata/socket access, unapproved peers/packages and cross-run capabilities are denied by actual containment.
5. Kill a worker after a broker response is committed but before worker delivery: recover the recorded result without a duplicate request. Kill after remote acceptance but before durable response: exercise the selected unknown-outcome policy without resetting usage.
6. Restart the supervisor and the isolated deployment; restore workspace and compressed context from a verified checkpoint. Missing/corrupt/incompatible bytes produce an explicit safe failure. Old fencing generations cannot continue dispatch.
7. Concurrent budget reservations cannot exceed approval. Limit extension preserves prior consumption; hung tool cancellation remains pending until enforced executor stop, including completion races.
8. Pin/version-test MCP submission/status/application cursors and A2A v1 task mapping, snapshot-first subscriptions, cancellation and outbound reconciliation. Disconnect never silently cancels durable work.
9. Restore a database/object backup into a fresh test deployment and validate references, permissions, license/storage readiness and pending operations before dispatch.
10. Verify Thai/English, Ocean Blue Light/Dark/System, responsive navigation, keyboard/focus accessibility, reduced-motion animation, actual stage events and expanded artifact continuity against DESIGN.md. Keep runtime/agent/skill identity out of product UI.

The first implementation tranche must prove the pinned runtime's broker-only execution and context/checkpoint continuation with fault injection before dependent feature work. This is an untested integration, not a native-runtime guarantee. If the runtime cannot preserve the required operation/context identities or bypass prevention, stop dependent waves, document the concrete failing check, and revise the adapter/design without silently weakening the accepted safety or recovery contract.

## Evidence references

- [Hermes pinned source](https://github.com/NousResearch/hermes-agent/tree/bd0affe5e5f723579df8902852f5d0c47795f355): `agent/tool_executor.py`, `agent/session_persistence.py`, `gateway/platforms/api_server_runs.py`, `tools/process_registry_checkpoint.py`, and developer docs on integration/hooks/middleware. Native checkpoints concern filesystem rollback; hooks fail open; dead native HTTP owners become interrupted.
- [Scientific catalog pinned source](https://github.com/K-Dense-AI/scientific-agent-skills/tree/154988403bb5a18e9d3c0ce4e6d5e2e4b184a298).
- [MCP specification release](https://github.com/modelcontextprotocol/modelcontextprotocol/releases/tag/2026-07-28) and [SDK release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0).
- [A2A specification](https://github.com/a2aproject/A2A/tree/v1.0.1) and [Python SDK](https://github.com/a2aproject/a2a-python/tree/v1.2.1).
- [MinIO Community advisory CVE-2026-41145](https://github.com/minio/minio/security/advisories/GHSA-hv4r-mvr4-25vw).
- [AIStor Free Agreement](https://www.min.io/legal/aistor-free-agreement), [license/features documentation](https://docs.min.io/aistor/operations/licenses/), [container installation](https://docs.min.io/aistor/installation/container/install/), [release API](https://dl.min.io/api/releases/aistor/latest), and [release artifacts/SBOM documentation](https://docs.min.io/aistor/operations/release-artifacts/).
- [Docker Engine advisory CVE-2026-92543](https://github.com/moby/moby/security/advisories/GHSA-7cfq-22r6-qp73).

All upstream checks were read-only. No image was pulled, vendor agreement accepted, product dependency installed, paid API called, or runtime acceptance test executed during this design phase.
