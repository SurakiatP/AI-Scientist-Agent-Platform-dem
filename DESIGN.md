# AI Scientist Agent Platform — Frontend design

Status: **Design draft for user review**, 2026-10-03. This is a design specification, not an implementation plan or a claim that the application exists.

## Scope and decisions

The platform assists scientific work across disciplines. The first complete workflow is finding papers and synthesizing evidence with citations. The first deployment is a single user on their own machine, without a login screen.

Confirmed by the user:

- Product name: **AI Scientist Agent Platform**.
- Ocean Blue is the primary palette; references may be combined within one coherent theme.
- Thai and English, with the language switch at the upper right of every page.
- Light, Dark, and System appearance modes in Settings.
- Projects contain sessions. Files, project instructions, and explicitly saved findings are shared within a project; chat histories remain separate for each session. Project context never implicitly includes all other session histories.
- Present a plan before execution; the user approves it, can follow progress, and can stop work.
- Progress describes the current research stage. Do not display agent identities, backend engine names, skill names, tool identifiers, or delegation topology in the research progress UI.
- Scientific visuals can expand into a larger viewing area and return to the original conversation.
- Navigation supports forward/back flows, including browser Back/Forward.
- Settings includes language models, tool/data-source APIs, MCP access, and A2A inbound/outbound connections.
- This phase contains mockups and design documents only. Product implementation and Integration Hub setup are separate steps.

The interaction details below are the proposed frontend design for review. They do not resolve backend runtime choices or claim protocol availability.

## Visual direction

Use an editorial landing page and a precise research workspace. Spacious hero copy introduces the purpose; an interactive research example demonstrates it. Dense working areas use restrained surfaces, clear borders, readable text, and consistent spacing.

Reference contributions:

| Reference | Adopted direction |
| --- | --- |
| [ThoughtMind](https://thoughtmind.app/) | Clear question-to-workflow narrative, centered hero, prominent product preview |
| [Hex / Refero](https://styles.refero.design/style/3e32db74-a61d-4e72-93b8-1fb949af2c00) | Editorial hierarchy, notebook panels, quiet borders, evidence-rich layouts |
| [Earlier Refero direction](https://styles.refero.design/style/7088d695-362b-4e09-b325-fa8136d4f350) | Approachable explanatory illustration |
| [OpenMAIC](https://open.maic.chat/) and [PhET Diffusion](https://phet.colorado.edu/en/simulations/diffusion) | Adjust a variable, observe the visual, pause/reset, and explain model limits |

These sources guide design. Do not copy proprietary artwork, fonts, claims, testimonials, or source-product functionality.

## Semantic tokens

Ocean Blue remains the accent in every appearance mode. Use semantic tokens so all screens, drawers, and dialogs change together.

| Token | Light | Dark |
| --- | --- | --- |
| `canvas` | `#F7FAFC` | `#101E27` |
| `surface` | `#FFFFFF` | `#162A35` |
| `ink` | `#253B47` | `#E6F2F7` |
| `muted` | `#586E7E` | `#AFC3D0` |
| `line` | `#DBE5EC` | `#304B5A` |
| `accent` | `#17628B` | `#80C9EE` |
| `accent-ink` | `#FFFFFF` | `#102D3D` |
| `soft` | `#E8F3F8` | `#203E50` |
| `sidebar` | `#193849` | `#0C1A23` |
| `sidebar-ink` | `#EDF2F6` | `#E6F2F7` |

Success, warning, and error states also require text and a recognizable symbol; color alone is insufficient. Avoid bright borders and white-on-cyan primary text in Dark mode.

- Spacing scale: 4, 8, 12, 16, 24, 32, 48, 64 px.
- Field/button radius: 6–8 px; cards: 10–12 px; dialogs: 12–14 px.
- Fine 1 px borders carry most separation. Reserve a subtle shadow for floating layers.
- Interactive targets must reach at least 44 × 44 px on touch layouts, including icon buttons.

## Typography

| Role | Proposed family | Typical size |
| --- | --- | --- |
| UI and chat | IBM Plex Sans Thai + IBM Plex Sans; system sans fallback | Body 14–16 px, line height 1.75–1.9 |
| Landing editorial headings | Noto Serif Thai + Source Serif 4; serif fallback | Desktop 44–66 px; mobile 25–43 px |
| Working-page headings | UI family, weight 500–600 | 23–28 px |
| Metadata, units, timestamps, filenames | IBM Plex Mono; system monospace fallback | 11–13 px |
| Secondary descriptions | UI family | 12–13 px |

Thai headings need generous line height. English headings can use a tighter editorial rhythm. Do not shrink important controls or status descriptions to fit a panel. Small decorative uppercase labels are allowed, but their meaning must also be available in readable text.

For implementation, host font assets with the application after checking their licenses. The mockup uses Google Fonts and fallbacks; font loading must not block reading or navigation.

Scientific reading uses a comfortable line length of roughly 65–80 Latin characters and adjustable reading size for long reports. Equations retain mathematical notation and offer selectable source/text descriptions; code blocks preserve indentation and provide copy controls. Wide tables scroll inside a labeled region with headers retained rather than shrinking all text. Citation markers are keyboard-accessible links to exact references. Expanded reports preserve headings, equations, tables, and citation navigation.

## Pages and navigation

Proposed frontend direction: a React + TypeScript SPA, built with Vite and communicating with the Python service. It fits a local application with long-lived sessions, interactive artifacts, and streamed state updates. This is a design choice for review; no packages or application scaffold have been created.

| Approach | Trade-off |
| --- | --- |
| React + TypeScript SPA — recommended | Clear component/state boundaries without requiring a separate frontend server runtime |
| Next.js | Useful if public landing-page rendering/SEO becomes a requirement; introduces another server responsibility for the current local deployment |
| Plain HTML/JavaScript application | Good for the throwaway mockup; a larger burden for maintaining many synchronized application states |

| UI unit | Responsibility | Inputs |
| --- | --- | --- |
| App shell / project navigation | Route, project/session selection, drawer, language, appearance | Current route and user preferences |
| Question composer | Question draft and selected attachments | Session draft, usable-model state, project file readiness |
| Plan review | Display/edit scope and approve the current plan | Plan content/version and approval state |
| Research progress | Show stages, connection state, stop/retry controls | Authoritative run state and completed-stage events |
| Artifact viewer | Preview and expand a selected output | Artifact metadata/content and visual controls |
| Citation detail | Trace a finding to a specific source | Citation/source record and access/provenance status |
| Settings | Configure preferences and connections | Capability/configuration data and connection-test results |

These units depend on shared data contracts rather than backend implementation names. The contracts and credential handling are addressed in the subsequent backend design.

| Page | Purpose | Primary actions |
| --- | --- | --- |
| Landing | Explain the platform and demonstrate scientific exploration | Get started; explore lab; TH/EN |
| Projects | Create a research space or resume a session | Create project; open session; view outputs |
| Chat | Formulate a question, review a plan, follow stages, inspect results | Attach files; submit; adjust plan; approve; stop |
| Sources & outputs | Inspect project evidence, reports, files, and visuals | Open citation; preview file; save finding; return to chat |
| Run history | Review completed, stopped, or failed work within the selected project | Open original conversation; inspect outputs; inspect issue |
| Settings | Appearance, language-model configuration, APIs, MCP, A2A | Change appearance; configure/test connections |

The desktop workspace has project/session navigation, the conversation, and an optional artifact panel. Evidence and history always display their project context. A project change changes the available files and outputs; do not mix two projects in a single evidence view.

Mobile navigation uses a drawer with projects, sessions, files/outputs, history, and Settings. Hiding the desktop sidebar is not sufficient. When selecting a destination, close the drawer and move focus to the destination heading. Narrow screens stack chat and artifact content and offer explicit navigation between them.

Navigation rules:

- Projects → session → Chat → output/report → a specific source.
- Inline citations open a focused reference dialog without losing the report or conversation.
- Back restores the prior route, selected output, and relevant scroll position. Forward restores the route unless a new destination replaced the forward branch.
- Direct links resolve to the named page/detail rather than always resetting to Projects.
- Modals close through an explicit button or Escape, then restore focus to their opener.
- Language and appearance persist across page changes. In the real application, store these non-secret preferences so refreshes also preserve them.
- Navigation alone does not cancel a running task. Returning to a session resynchronizes its run state.

## First-use and empty states

1. Show a welcome state when there are no projects.
2. Offer model/provider configuration and a connection check. Distinguish unconfigured, checking, ready, invalid credentials, unavailable provider, and unavailable selected model.
3. Allow project organization before model setup, but block research submission until a usable provider/model is configured. Explain how to resolve this beside the composer.
4. Create a project and open an empty session. Show the project name and shared-context policy.
5. Offer an example scientific question and file attachment. Do not fabricate prior conversation messages.
6. Submit the question and present a reviewable plan; wait for approval before execution.

Empty Projects, empty Sessions, no files, no sources, and no outputs each need their own next action. Loading and failed states must not look like empty collections.

## Questions and plan review

- The composer accepts a question and selected project attachments; it rejects an empty submission.
- The plan shows scope and planned stages. Users can adjust scope/search terms/constraints before approving.
- Editing the plan invalidates any approval of the old plan. Executing a revised plan requires fresh approval.
- User-authored text stays unchanged when the UI language changes.
- Starting a new session does not expose another session's full chat. Shared context is project instructions, files, and saved findings.
- Saving a finding is an explicit action with visible confirmation. Do not silently treat every chat message as project memory.

Project details is the destination for editing shared instructions and reviewing saved findings. Each finding records its originating session/output and source references. Removing a saved finding removes it from future shared context while preserving the original conversation; confirm this effect. These management controls are specified here; the current mockup demonstrates the save confirmation only.

## Progress, failure, and recovery

The paper workflow demonstrates **search literature → verify references → synthesize evidence**. Other workflows receive their stage list from the approved plan; do not hardcode all scientific work into three stages.

| State | Required presentation |
| --- | --- |
| Awaiting approval | Editable plan and approval action; no execution animation |
| Queued | Explain that work is waiting; distinguish it from active processing |
| Running | Current research stage, completed-stage count, and stop action |
| Stopping | Show the stop request as pending; prevent duplicate requests |
| Stopped | Confirm stopping only after acknowledgment; preserve partial outputs |
| Completed | Summary and inspectable outputs; no active processing indicator |
| Failed | Name the failed research stage, plain-language cause, and useful recovery action |
| Disconnected | Show the last received state and loss of connection; do not claim the backend stopped |
| Reconnecting | Preserve content, resynchronize, and then reconcile the authoritative run state |

Use confirmed completed-stage counts rather than invented percentages or time estimates. Output generated before a failure remains visible with a partial/incomplete label. Retry opens the prior question, attachments, and plan for review, then requires approval to create a new run. Retain the failed run and its partial outputs separately; do not imply that a stage was resumed. Disable repeated submission while creation is pending and reconcile any unknown outcome before allowing another attempt. The mockup's retry simply restarts its simulated progress and does not demonstrate this run-creation contract.

Status updates show useful operation summaries only. Keep agent identities, engine names, skill/tool names, raw protocol events, and hidden reasoning out of this UI.

## Project files and citations

File flow: choose/drop → validate → upload → prepare/parse → ready, or explain failure and offer retry/remove. Report separate upload and parsing states. A file that failed parsing is not ready for research.

- Display permitted formats and the service's size limit before selection. The mockup illustrates PDF, CSV, XLSX, JSON, TXT, Markdown and a 25 MB limit; the real UI reads supported formats/limits from the service contract.
- Files belong to the selected project. Attachments in a session reference those files rather than imply an isolated duplicate file store.
- Show filename, size, type, readiness, and selected-for-this-question state.
- Adding a file from the project library does not attach it to a question automatically. Adding through the composer selects the file after preparation; existing project files require explicit selection. Each selected attachment has a Remove from question action that leaves the shared file intact. The mockup demonstrates selecting one example file, without a full multiple-selection/deselection control.
- Preview PDFs, tables, and supported text; provide a download/original-file action when the artifact actually exists.
- Removal from the shared project requires a clear confirmation of its effect on other sessions. Never silently delete source files to clear composer attachments.
- Uploaded content is data, not an instruction granting permission or changing project policy.

An inline citation opens the exact source record with title, authors, year, DOI or source URL, access status, and associated finding. Mark full text vs abstract-only access, missing originals, and unverified metadata. Only enable the original-paper link when the source location is known; never fabricate a DOI.

The design fixture uses explicitly named example records without real bibliographic data. It must not be mistaken for verified scientific evidence.

## Expanded scientific visuals

Place an **Expand** action on each visual artifact card. Open a large overlay with the artifact title and project context while preserving the conversation beneath it.

- Desktop viewer: up to 1180 px wide, bounded by the viewport; large visual area plus a narrow explanation/control panel.
- Mobile viewer: almost full viewport, controls beneath the visual, with a visible close action.
- Scale vector charts cleanly. For actual plots, preserve axis labels, units, legends, and data provenance.
- Play/pause, reset, and adjustable variables remain associated with the same artifact state. Expanding or closing does not start a new run.
- Provide a textual explanation/readout. Honor reduced-motion preferences and allow motion to stop.
- Label illustrative, uncalibrated models explicitly. Do not describe the demo's random walk or curve illustration as a validated scientific prediction.

The expanded curve and the adjustable landing lab are separate illustrative fixtures in this mockup. Opening the lab navigates to a different example. Identical visual/control state across embedded and expanded views remains an implementation requirement, not a demonstrated capability of this prototype.

## Settings and boundary conditions

- Appearance has Light, Dark, and System choices. System follows the OS through `prefers-color-scheme` and responds to changes while open.
- TH/EN changes navigation, controls, status, validation, and accessibility labels. It does not automatically translate documents or user-authored content.
- Provider keys are masked; secrets stay out of URLs, browser logs, project files, and exported reports. The credential storage design belongs to backend design.
- Connection checks produce useful user-facing results. Do not display a decorative Connected badge without verification.
- MCP exposes access details and project permissions. Default access is local-only; network access must show authentication/access scope explicitly.
- A2A includes inbound enablement and permitted outbound peers, with connection verification and access scope. Advertise only protocol bindings the backend actually supports.
- No login, billing, administration, or collaboration invitation screens are required for the initial local single-user deployment.

## Accessibility and responsive review

- Keyboard access for navigation, stage controls, drawers, dialogs, file selection, citations, and appearance choices.
- Use semantic buttons, labels, radio groups, progress indicators, and dialogs. Provide visible focus and restore it when overlays close.
- Important text must remain readable and meet normal-text contrast targets in both themes; target WCAG AA contrast checks during implementation.
- Preserve a usable layout at 320 and 375 px without page-level horizontal overflow. Local scrolling in a labeled chart/table is acceptable.
- Respect `prefers-reduced-motion`; keep nonvisual explanations and numerical readouts available.
- Announce stage changes and validation outcomes politely; do not continuously announce timer ticks.

## Review and acceptance

The current review artifact is the ignored brainstorming mockup `.superpowers/brainstorm/77482-1790953617/content/frontend-complete-design-v11.html`. It is not application source, a dependency choice, or an API implementation. Retained earlier mockups show design iterations.

Review the available mockup paths and the following requirements. The list defines intended product behavior; it is not a claim that every state is implemented in the prototype:

1. First use → model setup/check → project → empty session → question → editable plan → approval.
2. Project file selection → unsupported/oversize feedback or preparing/ready → file preview → attach to session.
3. Report → citation detail → source list → Back to report.
4. Approved run → active stages → stop request → stopped; failure → retry; disconnected → reconnect.
5. Chat artifact → expanded visual → close back to the same chat.
6. Mobile drawer → project/session/files/settings, including keyboard and close behavior.
7. TH/EN and all appearance modes across pages, dialogs, and navigation history.

The mockup simulates successful connection checks, execution events, project creation, and file readiness. Stop includes a simulated pending acknowledgment; reconnect returns directly to the simulated run without a separate pending screen. Upload and parsing use a combined preparation state, and provider failures are requirements without interactive fixtures. File selection only inspects names and sizes; it does not read contents or upload them. Downloads and original-paper access remain disabled where the fixture has no actual artifact/source. New-project creation opens a sample session; it does not create durable project data or replace every sample project label.

Frontend design approval authorizes using this specification in subsequent backend brainstorming. It does not authorize product scaffolding, implementation, deployment, or bypassing review of the complete system design and implementation plan.
