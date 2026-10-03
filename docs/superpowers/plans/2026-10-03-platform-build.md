# AI Scientist Agent Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a local scientific workspace that searches papers and synthesizes cited evidence through approved, isolated, recoverable runs, with scoped MCP and bidirectional A2A access.

**Architecture:** React communicates with one Python domain through REST and persisted stage events. PostgreSQL owns lifecycle, approvals, grants, queue leases and effect/budget records; MinIO AIStor Free stores immutable bytes. A trusted supervisor launches run containers, and a separate credential-holding broker enforces their permitted external effects.

**Tech Stack:** Python 3.13, FastAPI, PostgreSQL, AIStor Free single-node, Docker, React, TypeScript and Vite. Inspected protocol candidates are MCP specification 2026-07-28 / mcp 2.3.0 and A2A specification 1.0.1 / a2a-sdk 1.2.1.

**Spec:** [Runtime and integration specification](../specs/2026-10-03-platform-runtime-design.md), [backend foundation](../specs/2026-10-03-backend-foundation-design.md), and [DESIGN.md](../../../DESIGN.md). Owner approved written system specification on 2026-10-03. This plan awaits written review; no product code has been created.

## Global Constraints

- “Use **MinIO AIStor Free single-node**, selected by the owner, rather than the archived Community distribution.”
- “Recovery means continuation from committed operation boundaries.”
- “Do not automatically resend an LLM/read request merely because it is a read, and never blindly resend a state-changing request.”
- “Workers use an isolated internal network, with no direct internet, host/LAN, PostgreSQL, storage, metadata-service or Docker access.”
- “The supervisor's Docker authority is equivalent to host administration and must not be exposed as a public API.”
- “Only the owner can approve/revise plans, extend limits, approve additional data release, publish shared results, and administer credentials/grants.”
- “An external submission initially produces a non-executing draft/request; it cannot spend provider allowance until the owner authorizes plan preparation.”
- “Inbound platform tokens are generated with strong randomness, displayed once, stored as hashes, independently revocable, and never forwarded upstream.”
- “No unmitigated applicable critical/high finding may ship; a scanner outage or missing image analysis is a reported gap, not a pass.”
- “The existing `.venv` reports Python 3.9.6, while `.python-version` selects 3.13.6.” Recreate it only during approved execution, after preserving any needed local environment information without reading secret values.
- Runtime commit: `bd0affe5e5f723579df8902852f5d0c47795f355`; reviewed catalog revision: `154988403bb5a18e9d3c0ce4e6d5e2e4b184a298`; initial skill set: paper-lookup, literature-review, scientific-writing.
- App name is **AI Scientist Agent Platform**. TH/EN toggle at the upper right of every page; Ocean Blue; Light/Dark/System. Display research stages rather than runtime names, agent identities, skill/tool identifiers or hidden reasoning.
- Shell commands start with `rtk`; unsupported commands use `rtk proxy`. Never delegate secrets, environment files, license contents, private data or production exports.
- No automatic shared Docker/Colima upgrade/restart, vendor license acceptance, public deployment, push or merge. The parent owns shared manifests, migrations, contract integration, Git commits and Hub updates at wave boundaries.

## Review Focus

1. An external request arrives before approval: no provider charge or worker launch (B2 and I1/I2 tests).
2. A tab replays a stale plan/publication or a lost submission: conflict or original run, never duplicated work (B1/B2 and F3).
3. A remote effect succeeds immediately before a crash: recover its committed result or pause its unknown outcome with budget retained (B3/B5).
4. A malicious upload contains links, compressed oversized data or instructions: bounded parsing, no mount escape or authority change (B4).
5. User switches language/theme or expands a chart during streaming: keep authored text, artifact state, route/focus and scientific meaning (F1–F3).

## Plans and deliverables

The specification spans three independently reviewable deliverables. Execute their tasks in the wave order below rather than treating their documents as permission to run everything concurrently.

| Plan | Tasks | Working deliverable |
|---|---|---|
| [Backend and runtime](2026-10-03-backend-runtime-build.md) | B1–B6 | Typed REST service, policy/ledger, isolated fixture run, then paper workflow |
| [Frontend workspace](2026-10-03-frontend-workspace-build.md) | F1–F4 | Navigable bilingual workspace against contract fixtures, then actual API/events |
| [Protocols and release checks](2026-10-03-protocol-release-build.md) | I1–I4 | Scoped MCP/A2A adapters, local deployment, restore/security/live acceptance evidence |

No claim that all 177 catalog entries are installed or validated. Do not add accounts, billing, a generic plugin marketplace, distributed queue, Kubernetes or remote owner access to this slice.

## File ownership and execution allocation

Keep source in one repository. At the owner's subsequent instruction, branch `feat/scientist-platform` was created from local `develop` at `ebda91f361298858cdc9e7748306986ef3ba9392` on 2026-10-03, using the owner's `feat/...` naming preference (bug fixes use `fix/...`). Continue editing that branch in this checkout. The pre-existing `.python-version` remains untracked during planning; B1 reconciles it with a patched compatible Python 3.13 interpreter before tracking it during approved setup. If execution later needs a separate managed worktree, use the git-worktrees skill and existing-artifact checks with this explicit branch state, never the remote default branch. Develop is the eventual integration target; do not merge/push before the completed work is reviewed.

`delegate-build` is the user-selected execution method. After plan review, dispatch a planner using **gpt-6.1-sol / high**, workers using **gpt-6-luna / high**, and an independent final reviewer using **gpt-6.1-sol / high**. Announce each role before dispatch. The planner validates ownership/dependencies against these plans; it does not silently change approved contracts or safety gates.

There are three worker slots beside the parent. Only tasks in the same row may overlap. A worker owns exactly the files in its task; root manifests/lockfiles are edited only by the parent or their designated single task. No concurrent Git commits, shared test-database migrations, Docker daemon reconfiguration or changes to the same file. Use distinct test database names and container labels per task.

| Wave | Tasks allowed in parallel | Prerequisite / exit gate |
|---|---|---|
| 0 | Planner only | Plan approval, feature branch/source instructions, ownership validation, available isolated test PostgreSQL; no image pull on an unresolved affected engine |
| 1 | B1 | Runnable database/typed contracts; parent locks dependencies and publishes TS contract |
| 2 | B2, F1 | B1 contracts fixed; separate backend vs frontend manifests |
| 3 | B3, B4, F2 | B2 authorization/ledger API; no shared migrations or lockfile edits |
| 4 | B5 only | B3/B4; patched engine, owner-obtained AIStor license, scanned candidate images; actual containment/recovery tests pass |
| 5 | B6, F3 | B5 integration validated; F3 uses B1 generated DTO fixtures independently of B6 implementation |
| 6 | I1, I2 | B6 domain resource functions available; parent resolves both SDK dependencies once before dispatch |
| 7 | F4 | Actual B6/I1/I2 services and F3 UI ready; generated contract remains parent-owned |
| 8 | I3 | All component tasks integrated; real local deployment and backup/restore proof |
| 9 | I4, then independent reviewer | Complete acceptance, live provider configuration and dated vulnerability evidence |

B4's object/parser tests and F2's fixtures can be written before licensed storage is available. Their real AIStor acceptance remains pending and blocks B5, I3 and release acceptance. An unavailable license/fixed engine/provider key is recorded as a concrete prerequisite, never hidden by substituting a different storage backend or asserting fixture success proves a live workflow.

## Wave protocol and commit policy

- [ ] Parent supplies each worker its task, global constraints, approved spec sections, exact interfaces, tests and assigned files; never fork the entire conversation into implementation workers.
- [ ] Worker writes the meaningful failing check first, observes its expected failure, implements within its assigned scope, then returns file list/test evidence and any gate failure. Do not claim an unrun integration check passed.
- [ ] Parent waits for all workers in the wave, inspects aggregate diff, resolves contract integration, runs only affected checks and commits that wave with an intent-specific message.
- [ ] Parent updates ignored Hub TRACKING/WORKING_LOG/contract registry and only durable LEARNINGS/ADRs. No force-add of the Hub.
- [ ] A failed runtime safety/recovery gate stops dependent waves. Fix the responsible layer and rerun that check; changed architecture/data policy needs renewed written review.
- [ ] Final independent reviewer receives approved specs/plans and final diff, runs relevant checks, and reports explicit PASS or FAIL with command evidence. Release remains incomplete if operator prerequisites/live workflow/required scans are missing.

## Acceptance coverage

| Specification requirement | Owning tasks |
|---|---|
| Immutable inputs, plan digests, deduplication, concurrent publication | B1, B2, B4, B6 |
| Owner session, grants, secret storage and revocation | B2, I1, I2 |
| Effect journal, budgets, deny-by-default broker and package policy | B3, B5 |
| Runtime context, compressed checkpoints, unknown outcomes, fencing and stop races | B5 |
| Scientific search, verified citations, abstract/full-text provenance | B6 |
| Thai/English, appearance, lab animation, accessible scientific reading | F1–F3 |
| Project/session/file/finding routes, real SSE, decisions, history | B6, F2–F4 |
| MCP 2026-07-28 profile and application cursors | I1 |
| A2A v1 inbound/outbound, approval, cancellation/reconciliation | I2 |
| AIStor license/features, patched hosts/images, encrypted storage, backup/restore | B4, I3 |
| Lockfiles, SBOM, transitive CVEs, containment and live acceptance | B1, B5, I3, I4 |

## Plan review and handoff

Review all three linked component plans as one staged implementation. The next action after owner confirmation is delegate-build's planner/wave procedure. Execution method is already settled; do not ask the owner to choose it again.
