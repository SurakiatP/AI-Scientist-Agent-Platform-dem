# AI Scientist Agent Platform — Backend Foundation

Date: 2026-10-03 (Asia/Bangkok)

Status: Record of the approved architectural direction, policies, and workflow. The owner approved the written companion system specification on 2026-10-03. Implementation-plan review and runtime verification remain outstanding; this document alone does not authorize product implementation.

The companion [runtime and integration specification](2026-10-03-platform-runtime-design.md) records source-inspected constraints, reviewed operational contracts, security acceptance checks, and the owner's subsequent choices of local open-source MinIO single-node (superseding AIStor Free) and pausing unknown-outcome steps for an owner decision. Written review is approved; runtime verification remains an implementation prerequisite before dependent features are accepted. The staged [implementation plan](../plans/2026-10-03-platform-build.md) now awaits review.

## Intended outcome

Build a multidisciplinary scientific assistant whose first complete workflow searches papers, evaluates accessible evidence, and produces a synthesis with traceable citations. Begin with one owner on a local machine. Support future server deployment and authorized clients on other machines through MCP and bidirectional A2A.

The frontend direction is recorded in [DESIGN.md](../../../DESIGN.md). The product name is AI Scientist Agent Platform. The UI supports Thai/English and shows research stages rather than runtime names, agent identities, skill names, or hidden reasoning.

## Approved architecture

- One Python application service using FastAPI, with separately managed execution workers.
- PostgreSQL is the application database for projects, sessions, plan versions, approvals, run state, checkpoint records, usage accounting, and audit events.
- MinIO stores original files, versioned inputs, checkpoint payloads where appropriate, and output artifacts. Database records reference exact object versions and integrity information.
- Each project is an isolated research context. Each run has a separate Docker sandbox and working area restricted to its project input snapshot.
- Multiple runs may execute concurrently, including runs in different sessions of the same project. Execution limits and scheduling must enforce available resources.
- REST, MCP, and A2A enter the same application workflow and permission checks. They must not implement independent approval or execution rules.
- The intended underlying runtime is [Hermes](https://github.com/NousResearch/hermes-agent), using the [K-Dense scientific skills catalog](https://github.com/K-Dense-AI/scientific-agent-skills). Integration mechanics remain subject to source inspection and feasibility checks.

PostgreSQL and MinIO are approved design choices; they have not been installed or connected by this design work. The existing runtime's private persistence is not assumed to be PostgreSQL-compatible.

## Project and run data

Sessions share project files, project instructions, and explicitly saved findings. Their full conversation histories remain separate. A saved finding retains its originating session, output, and evidence references.

Accepting a research question and preparing its plan binds the selected inputs to a fixed snapshot. The approved plan references that snapshot, chosen provider/model, allowed operations, package requirements, and execution limits. Later project changes do not silently alter an approved run. Changing a bound input or plan requires a revised approval.

Workers write their own run outputs. The owner explicitly selects which outputs become shared project material. Publication creates a new version and preserves provenance; it does not silently overwrite project originals or another run's outputs.

## Approved workflow

1. Receive the question and selected project files; bind their versions for this run.
2. Prepare a reviewable plan identifying the model, allowed tools and packages, data scope, and execution limits.
3. The owner approves through the web interface. The run enters the execution queue.
4. Start the run in its isolated Docker sandbox. Persist state and checkpoint references in PostgreSQL and files in MinIO; deliver research-stage updates to connected clients.
5. Following interruption, reconcile the original run and automatically recover from an available checkpoint. Check existing external-operation results before attempting an operation again.
6. Present outputs with their evidence and completion status. The owner selects material to publish back to the project.

An approval is bound to the plan and input versions, not merely a session ID. Recovery retains the approved scope and previously consumed budget.

## Model, data, and package policy

- The owner chooses a primary model and may override it for a particular run before approving the plan. The plan records its provider/model; switching outside the approved scope requires review.
- Send only necessary research content to the configured, selected LLM. Sending file content to other analysis services requires an explicit owner decision. The complete policy for literature queries and outbound A2A payloads must be specified before implementation.
- Package installation is permitted inside the run sandbox when included in the approved plan, using configured package sources. Record versions and environment information. Packages outside the approved plan require additional review.
- Runs have adjustable time and LLM-usage ceilings. At the ceiling, retain results and request a decision to extend execution or stop. Usage accounting and safe stopping boundaries require detailed contracts.
- A paper available only as an abstract may contribute a limited summary. Mark its access level and distinguish it from full-text evidence. Never invent bibliographic identifiers, unavailable details, or a claim that full text was read.

## External clients

The initial owner interface has no login and is local. Network exposure and server access are separate deployment decisions.

Each MCP/A2A caller has a distinct platform access token and only the project permissions granted by the owner. Reading, attaching files, and submitting work are separately controlled. These tokens are distinct from provider API keys and MinIO credentials.

External callers can submit work and follow its authorized state/results. Every plan still waits for the owner to approve through the web interface. A2A supports inbound work and outbound delegation to configured peers, subject to the same data and permission policies.

## Recovery requirement and validation frontier

Automatic continuation from recorded checkpoints is required. Transcript persistence alone is not evidence that a worker can resume tool execution safely. A runtime profile alone is not accepted as proof of filesystem isolation.

Complete these design and feasibility checks before calling the backend specification implementation-ready:

1. Define checkpoint granularity, durable state, capture/restore format, and compatibility with the selected runtime revision. Demonstrate recovery after process and host interruption.
2. Define external-operation identities and reconciliation for an unknown outcome. Demonstrate that recovery does not blindly repeat a completed or unresolved side effect.
3. Verify two concurrent runs in one project and runs in two different projects, including input isolation, separate contexts, scoped object access, and collision-free publication.
4. Specify the run/approval/budget state transitions and test stopping acknowledgments, disconnection, recovery, and limit enforcement.
5. Write versioned REST/event, MCP, and A2A contracts, including authorization, cancellation, approval handoff, event resynchronization, and artifact access.
6. Define credential storage, sandbox egress enforcement, package-source access, outbound peer permissions, resource limits, retention, and backup/restore of PostgreSQL with MinIO references.

These are outstanding design and validation tasks, not verified defects or implemented capabilities. No product source, dependency installation, database schema, or deployment has been created during this phase.
