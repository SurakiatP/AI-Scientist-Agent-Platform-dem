# Frontend Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the approved Ocean Blue bilingual landing page and scientific workspace, then connect them to real durable research events.

**Architecture:** A React/TypeScript/Vite application uses generated domain DTOs and a small REST client. Routes preserve project/session scope and browser history; reusable dialogs preserve focus and artifact state. Fixture data is explicit and used only in tests/development previews, while normal product mode shows actual API loading/errors/empty states.

**Tech Stack:** React, TypeScript, Vite, React Router, semantic HTML/CSS/SVG, react-markdown without raw HTML, remark-math/rehype-katex with KaTeX trust disabled, Playwright and axe accessibility checks.

**Spec:** [DESIGN.md](../../../DESIGN.md) and [runtime/event contract](../specs/2026-10-03-platform-runtime-design.md#rest-and-event-contract). Dependencies, wave order, branch and models are in the [master plan](2026-10-03-platform-build.md).

## Global Constraints

All master constraints apply. No public product copy identifies Hermes, internal agents, skills/tools or delegation. TH/EN controls persist only interface language, never rewrite user text. Preference storage contains language/appearance only, never credentials/tokens. Generated types live at `contracts/api-types.ts`; the parent regenerates them after backend integration.

## Review Focus

- Loading/error collections must not render as empty success: F2/F4.
- Back/Forward and dialog closure must restore selected content, scroll/focus: F1/F2/F3.
- Changing theme/language during streaming must not reset drafts/artifacts: F1/F3.
- A failed upload/parser must not become a selected usable attachment: F2.
- Expanding/reducing a scientific visual must preserve its variable/play state: F3.

## Route and file map

| URL | Owning component |
|---|---|
| `/` | Landing and illustrative interactive lab |
| `/projects` | Project collection and onboarding |
| `/projects/:projectId` | Instructions, shared files and saved findings |
| `/projects/:projectId/sessions/:sessionId` | Chat/plan/run view |
| `/projects/:projectId/outputs` | Sources, outputs and optional selected artifact query |
| `/projects/:projectId/history` | Run history and original-chat links |
| `/settings/appearance` | Light/Dark/System and interface language |
| `/settings/models` | Masked provider/model setup and verified connection checks |
| `/settings/tools` | Configured tool/data-source connections |
| `/settings/mcp` | Bearer grants/access details, once-only token creation |
| `/settings/a2a` | Inbound access and approved outbound peers |

Project/file/finding edits never change a running snapshot. Direct links resolve authorization/loading/not-found explicitly. A chart or citation opens a dialog over its current route; changing route closes it without starting/canceling a run.

### F1 — Accessible app shell, preferences and landing lab

**Owns:** `apps/web/{package.json,package-lock.json,index.html,vite.config.ts,tsconfig.json,playwright.config.ts}`; `apps/web/src/{main.tsx,App.tsx,preferences.tsx,locales.ts,styles.css,Landing.tsx,LabDemo.tsx}`; `apps/web/tests/shell.spec.ts`; licensed font assets under `apps/web/public/fonts/` only after verification. Parent coordinates later dependency edits.

**Dependencies:** B1 generated contracts. No backend credentials or actual API effects needed for shell tests.

**Interfaces:** `usePreferences() -> {language: 'th'|'en', appearance: 'light'|'dark'|'system', setLanguage, setAppearance}`; `t(key: string, language: 'th'|'en') -> string`; `AppearanceSettings` from `preferences.tsx` renders local non-secret preference controls at `/settings/appearance` and is reused by F4. `LabDemo({state, onChange})` takes controlled `{diffusion: number, paused: boolean, seed: number}` state. `App` mounts routes, upper-right language switch, accessible global project/session navigation and a mobile drawer. API readiness arrives in F4.

- [ ] **1. Write the focused browser checks.** Use product routes and accessible names, with explicit test-only API fixtures; verify 320/375/1280 px, keyboard drawer and History API, persisted preferences, OS changes and reduced motion:

```typescript
test('system appearance follows OS without losing language', async ({ page }) => {
  await page.goto('/settings/appearance');
  await page.getByRole('radio', { name: 'System', exact: true }).check();
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.getByRole('button', { name: 'TH', exact: true }).click();
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('lang', 'th');
});
```

The test starts in English via a non-secret preference fixture. It also asserts no page-level horizontal overflow and drawer close restores focus. No fake connection-ready badge is allowed.

- [ ] **2. Bootstrap only the files needed by this task and observe RED.** F1 is the sole writer of its package/lockfile in this wave. Add scripts `dev`, `build`, `typecheck`, `test:e2e` and install the reviewed compatible React/Vite/TS/Router/Playwright dependencies. Configure the test server and loopback dev binding, then run `rtk npm --prefix apps/web run test:e2e -- tests/shell.spec.ts`; expected failure is absent shell behavior. Browser installation/network prerequisites are reported honestly.

- [ ] **3. Implement the visual system and native control behavior.** Copy exact semantic tokens, spacing, radii and typography roles from DESIGN.md into CSS variables. Self-host verified licensed IBM Plex/Noto/Source Serif font files with system fallbacks and nonblocking display. Maintain text contrast and 44 px touch targets. Preferences use localStorage for only language/appearance and a `matchMedia` listener for system changes:

```typescript
const resolved = appearance === 'system'
  ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
  : appearance;
document.documentElement.dataset.theme = resolved;
document.documentElement.lang = language;
```

The listener runs this same update when OS preference changes. Use React Router and proper links, route headings, focus/scroll restoration and a labeled mobile drawer. Keep language toggle in the upper-right header on every page. Avoid a custom routing framework.

Landing follows the approved editorial/reference direction: clear scientific purpose, Get started at upper right, accessible product preview, grounded workflow explanation and an adjustable illustrative diffusion lab. Use original CSS/SVG visuals, not copied artwork/testimonials. The lab exposes diffusion, play/pause/reset, accessible numeric/text readout and a clear illustrative/unvalidated label. Deterministic seed makes reset reproducible; reduced motion starts paused. No simulated animation pretends a real research job is running.

- [ ] **4. Verify and parent-commit.** Run shell browser checks, `rtk npm --prefix apps/web run typecheck`, and `rtk npm --prefix apps/web run build`. Parent reviews Light/Dark/mobile captures and font license files, then commits `feat: add bilingual ocean blue shell and landing lab`.

### F2 — Project/session routes, context and file/source management

**Owns:** `apps/web/src/{Projects.tsx,ProjectDetails.tsx,Library.tsx,RunHistory.tsx,api.ts}`; `apps/web/tests/project-flows.spec.ts`; test fixtures under `apps/web/tests/fixtures/`. Does not edit F1 shell files concurrently; parent mounts these routes at wave integration.

**Dependencies:** B1/B2 contracts plus F1. B4/B6 API completion is not assumed; tests intercept explicit fixture responses matching generated DTOs.

**Interfaces:** `api.request<T>(path: string, init?: RequestInit) -> Promise<T>` validates safe error responses and includes owner cookie/CSRF on authorized same-origin mutations; `ApiError` carries `code`, `status`, `requestId`. Public upload/selection uses FileView IDs, not S3 keys. Project/session/file/finding DTOs come from the parent-generated contract.

- [ ] **1. Write route/context/file checks.** Verify project scoping, session history separation, instructions/findings provenance/removal, Back/Forward and citation exactness. A representative flow:

```typescript
test('removing a saved finding does not delete its source report', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.goto('/projects/11111111-1111-4111-8111-111111111111');
  await page.getByRole('button', { name: 'Remove finding: Example finding' }).click();
  await page.getByRole('button', { name: 'Confirm removal' }).click();
  await page.getByRole('link', { name: 'Sources & outputs' }).click();
  await expect(page.getByText('Example report', { exact: true })).toBeVisible();
});
```

`installProjectFixtureRoutes(page)` lives in `apps/web/tests/fixtures/project.ts`, serves generated-type-compatible data with project UUID `11111111-1111-4111-8111-111111111111` and session UUID `22222222-2222-4222-8222-222222222222`, and records writes for assertions. No fixture mode can enable production privileged actions. Add unsupported/oversize files, upload vs preparing vs failed/ready, and forbidden/not-found/loading/error states. Chat attachment selection is covered in F3, after Chat exists. Citation fixture URLs are explicit and unverified, never fabricated real sources; fixtures obey B1's UUID validators.

- [ ] **2. Observe RED.** Run `rtk npm --prefix apps/web run test:e2e -- tests/project-flows.spec.ts`; expected failures identify missing routes/context selection, not absent backend credentials.

- [ ] **3. Implement minimal route pages and secure client.** Build create-project/session and first-use states; project details edits instructions and explicitly saved findings with source/session provenance. Confirm shared deletion/removal effects; preserve original messages. Collection requests display separate loading/empty/failed states. Abort old project reads on navigation or reject stale responses by project/request identity so one project cannot flash another's content.

`api.request` sends only same-origin relative `/api/v1` requests and formats known error codes; it never forwards the inbound bearer token to provider/peer URLs. Upload reads server-advertised types/limit; display distinct byte-transfer and parsing statuses. Show selected attachments separately from shared library, with Remove from question as a nondestructive action. Disable failed/unprepared inputs for submission.

Library previews safe text, bounded tables and PDFs via authorized gateway; no raw HTML injection. Sources show exact title/authors/year/known identifiers/access/verification and original links only when present. History opens original session/run and partial outputs; retry links to a prefilled review flow without changing the failed run. External callers' unsupported UI actions remain inaccessible server-side even if they manipulate the client.

- [ ] **4. Verify and parent-commit.** Run the project-flow suite and typecheck. Parent mounts route imports, validates scope under browser Back/Forward and a rapid project-switch fixture, then commits `feat: add project evidence and session navigation`.

### F3 — Chat, plan review, actual stage rendering and expanded artifacts

**Owns:** `apps/web/src/{Chat.tsx,RunProgress.tsx,ArtifactViewer.tsx}`; `apps/web/tests/research-ui.spec.ts`. Parent integrates route imports; no protocol/settings edits.

**Dependencies:** F2 and B5; uses B6 contract at wave integration, fixtures during isolated UI tests.

**Interfaces:** `RunProgress({run: RunView, events: RunEvent[], connected: boolean, onStop})`; `ArtifactViewer({artifact: ArtifactView, state, onStateChange, onClose})`. Plan review reads owner-only `GET /runs/{run_id}/plan` as generated `PlanView`, submits its exact revision/digest and refreshes on conflict. Shared visual state remains in Chat by artifact ID, so an expanded view and embedded card observe the same controlled state. Selected files and question text stay in the session draft; no secrets enter it.

- [ ] **1. Write stateful scientific UI checks.** Use deterministic contract events, not production timers. Test approve-edited-digest, queued/running/waiting/stopping/terminal states, disconnect/reconnect, unknown-outcome decisions, partial output retry lineage, copyable code/selectable math and expanded continuity:

```typescript
test('expanded artifact preserves controls and focus', async ({ page }) => {
  await installResearchFixtureRoutes(page);
  await page.goto('/projects/11111111-1111-4111-8111-111111111111/sessions/22222222-2222-4222-8222-222222222222');
  await page.getByLabel('Diffusion').fill('0.7');
  const expand = page.getByRole('button', { name: 'Expand visual' });
  await expand.click();
  await expect(page.getByRole('dialog').getByLabel('Diffusion')).toHaveValue('0.7');
  await page.keyboard.press('Escape');
  await expect(expand).toBeFocused();
  await expect(page.getByLabel('Diffusion')).toHaveValue('0.7');
});
```

Define the fixture helper in `apps/web/tests/fixtures/research.ts`. Also assert changing TH/EN preserves the original question, chart values and messages; a stop request keeps `stopping` until server acknowledgment; a disconnected badge does not claim the run stopped; duplicate creation is disabled/reconciled using the stable submission key. Scan only status/progress product copy for forbidden implementation identities, without censoring user-authored content.

Reuse F2's valid UUID constants for project/session fixture URLs. Add a selection test that uploads through the library without attaching, adds through the composer only after preparation, removes `example.csv` from the question, then visits project details and verifies the shared file remains. Failed/preparing files cannot be selected. The fixture tracks DELETE requests and proves removal from the question sends none.

- [ ] **2. Observe RED.** Run `rtk npm --prefix apps/web run test:e2e -- tests/research-ui.spec.ts`.

- [ ] **3. Implement the conversation and scientific viewer.** Composer rejects empty question, blocks unconfigured model/unready files with clear next actions, keeps draft by session and preserves the stable submission key across uncertain request outcomes. Plan review binds shown revision/digest; edits invalidate old approval. Show model/scope/data recipients/packages/limits where they help owner approval, but progress shows only localized scientific stages. Approved work receives confirmed stage events/counts; never invent percentages, agents or tool activity.

Render queued, active, waiting budget/data/unknown outcomes, pending stop, terminal and connection states distinctly. Unknown outcome shows retained reservation and choices verified result, retry with possible duplicate cost, or stop. Retry of a terminal failure preloads its previous question/input/plan into a new approval flow, preserving old partial outputs.

Use a native accessible dialog with explicit close/Escape/focus restoration and bounded 1180 px desktop/almost-full mobile content. Keep artifact title/project/provenance/axis units/legends visible. A controlled artifact state owns play/pause/reset/variables; expansion does not remount/reset it or start a run. Honor reduced motion. Scientific reports use readable adjustable text, bounded wide-table scrolling, code copy, safe Markdown and accessible citation links. Raw HTML and unsafe URL schemes are disabled; reviewed remark-math/rehype-katex uses KaTeX trust=false and exposes selectable original equation source/text descriptions. Parsing/rendering failures show safe original text. Do not execute scripts from report content or load unapproved remote images from Markdown.

- [ ] **4. Verify and parent-commit.** Run research UI checks/typecheck and keyboard/reduced-motion manual review; parent integrates B6 DTO changes and commits `feat: add approved research chat and expanded outputs`.

### F4 — Settings and real REST/SSE integration

**Owns:** `apps/web/src/{Settings.tsx,useRunEvents.ts}`; `apps/web/tests/live-workspace.spec.ts`; parent owns any `api.ts`/generated-contract/shell integration changes requested by this task.

**Dependencies:** B6/F3/I1/I2. Actual peer/settings/event checks run only after both protocol adapters are integrated; no backend file edits by this worker.

**Interfaces:** `useRunEvents(runId: string | null) -> {run: RunView | null, events: RunEvent[], connected: boolean, reconnecting: boolean}`. It uses a consistent snapshot/latest_cursor, then authorized REST SSE, deduplicates by sequence and resets from snapshot on `cursor_expired`. Native EventSource uses the HttpOnly owner cookie, never a secret-bearing URL.

- [ ] **1. Write actual-service UI checks.** Run against an isolated backend with deterministic provider, real auth/database/events and separate project fixtures. Include first-use, invalid/unavailable provider/model, secret-mask persistence, once-only MCP token display/revocation, permitted A2A peers/data scope, stale configuration, network failure, event replay and browser refresh mid-run:

```typescript
test('refresh restores confirmed run state without a second submission', async ({ page }) => {
  await launchAuthorizedFixtureRun(page);
  await expect(page.getByText('Verify references', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText('Verify references', { exact: true })).toBeVisible();
  await expectSingleSubmissionInFixtureBackend();
});
```

These helpers are defined in this test file using the test deployment's owner bootstrap and domain setup, without printing credentials. Explicitly test UI draft preservation while theme/language changes and state resynchronization after lost SSE/expired cursor.

- [ ] **2. Observe RED.** Run `rtk npm --prefix apps/web run test:e2e -- tests/live-workspace.spec.ts`. A missing validated/isolated test deployment is a blocked check, never silently replaced by static mocks.

- [ ] **3. Wire actual endpoints and connection states.** Settings consumes B6 connection/token/peer/capability routes and I2 actual peer checks. Password inputs are cleared after submission and never persisted in local/session storage. Save returns masked metadata only. Show checking/error/ready only from verified responses; do not infer connection success from saving a value. Tokens display once with copy action and scope; external enablement clearly shows local/network access policy.

`useRunEvents` loads snapshot, opens the per-run cursor stream, deduplicates ordered events, rechecks on navigation/refresh/reconnect and aborts obsolete runs. On an expired cursor fetch a new snapshot, preserving messages/outputs. Normal product mode always uses the real API; no fabricated prior chat/outputs or fallback connection-ready state. Parent connects the owner bootstrap handoff, SPA route fallbacks and same-origin CSRF header.

- [ ] **4. Verify and parent-commit.** Run all frontend suites once, typecheck/build, axe checks and rendered contrast checks in Light/Dark at 320/375/1280 px. Preserve screenshots of the landing, empty/configuration, active run, waiting/partial output and expanded artifact states. Parent commits `feat: connect workspace settings and durable research events`.
