import type { Page } from '@playwright/test';
import type { ArtifactView, ConnectionView, FileView, PlanView, RunEvent, RunView } from '../../../../contracts/api-types';
import { PROJECT_ID, SESSION_ID, installProjectFixtureRoutes } from './project';

const SUBMIT_FIELDS = ['submission_key', 'question', 'input_ids', 'provider_id', 'model'];

export { PROJECT_ID, SESSION_ID };
export const SESSION_URL = `/projects/${PROJECT_ID}/sessions/${SESSION_ID}`;
export const RUN_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
export const NEW_RUN_ID = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
export const PLOT_ID = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
export const REPORT_ID = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee';
export const CONNECTION_ID = '12121212-1212-4212-8212-121212121212';
export const DECISION_BUDGET = 'aaaaaaaa-0000-4000-8000-000000000001';
export const DECISION_UNKNOWN = 'aaaaaaaa-0000-4000-8000-000000000002';
export const READY_FILE = 'f1111111-1111-4111-8111-111111111111';
export const BAD_FILE = 'f2222222-2222-4222-8222-222222222222';
export const REPORT_MARKDOWN = 'Findings summary.\n\n$$E = mc^2$$\n\n```python\nprint("diffusion")\n```\n';

const artifact = (id: string, kind: ArtifactView['kind'], title: string, partial = false): ArtifactView => ({ artifact_id: id, project_id: PROJECT_ID, run_id: RUN_ID, title, kind, sha256: 'a'.repeat(64), size: 100, content_type: kind === 'report' ? 'text/markdown' : 'application/json', partial });
export const plot = artifact(PLOT_ID, 'plot', 'Concentration profile');
export const report = artifact(REPORT_ID, 'report', 'Evidence report');

export const makeRun = (patch: Partial<RunView> = {}): RunView => ({ run_id: RUN_ID, project_id: PROJECT_ID, session_id: SESSION_ID, revision: 1, state: 'completed', latest_cursor: 0, usage_tokens: 20, reserved_tokens: 0, planning_tokens: 4, token_limit: 100, artifacts: [plot, report], ...patch });
export const makePlan = (revision = 1, stages = ['search_literature', 'verify_references']): PlanView => ({ run_id: NEW_RUN_ID, revision, plan_digest: `${'d'.repeat(63)}${revision}`, plan: { input_snapshot_digest: 'e'.repeat(64), provider_id: CONNECTION_ID, model: 'fixture-model', stages, allowed_ops: ['llm'], data_recipients: ['fixture-provider'], packages: [{ name: 'numpy', version: '2.0.0', source: 'pypi', sha256: 'f'.repeat(64) }], token_limit: 100, elapsed_limit_ms: 60000 } });

export function event<K extends RunEvent['kind']>(sequence: number, kind: K, payload: Extract<RunEvent, { kind: K }>['payload']): RunEvent {
  return { schema_version: 1, run_id: RUN_ID, sequence, revision: 1, occurred_at: '2026-10-05T00:00:00Z', kind, payload } as RunEvent;
}

type Options = {
  run?: RunView | null; messages?: Array<{ id: string; sequence: number; role: string; content: string; created_at: string }>;
  connection?: ConnectionView['state'] | 'missing'; expireCursorOnce?: boolean; pageSize?: number; conflictOnApprove?: boolean; failFirstSubmit?: boolean; holdSubmit?: boolean; reportMarkdown?: string; failFirstDecision?: boolean;
};

// Deterministic contract events and snapshots; nothing here depends on production timers.
export async function installResearchFixtureRoutes(page: Page, options: Options = {}) {
  await installProjectFixtureRoutes(page);
  let run: RunView | null = options.run === undefined ? makeRun() : options.run;
  let events: RunEvent[] = [];
  let plan = makePlan(1);
  let offline = false;
  let stopAcknowledged = false;
  let uploaded: FileView | null = null;
  let fileListCount = 0;
  let submitCount = 0;
  let cursorExpired = false;
  let decisionCalls = 0;
  const receipts = new Map<string, { payload: string; run: RunView }>();
  const pageRequests: number[] = [];
  let release: (() => void) | null = null;
  const gate = options.holdSubmit ? new Promise<void>((resolve) => { release = resolve; }) : null;
  const submitted = new Map<string, RunView>();
  const writes: Array<{ method: string; path: string; body?: any }> = [];
  const deletes: string[] = [];
  const messages = options.messages ?? [{ id: 'm1', sequence: 1, role: 'user', content: 'Original question about diffusion', created_at: '2026-10-05T00:00:00Z' }];
  const connection: ConnectionView = { id: CONNECTION_ID, label: 'Fixture', provider: 'fixture-provider', model: 'fixture-model', state: options.connection === 'missing' ? 'unconfigured' : options.connection ?? 'ready', has_secret: true };
  const baseFiles: FileView[] = [
    { id: READY_FILE, project_id: PROJECT_ID, filename: 'example.csv', size: 10, content_type: 'text/csv', state: 'ready' },
    { id: BAD_FILE, project_id: PROJECT_ID, filename: 'broken.csv', size: 10, content_type: 'text/csv', state: 'failed', error_code: 'preparation_failed' },
  ];

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace('/api/v1', '');
    const method = request.method();
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    const err = (code: string, status: number) => json({ code, message: code, request_id: 'fixture' }, status);
    let body: any; if (method !== 'GET') { try { body = request.postDataJSON(); } catch { body = undefined; } writes.push({ method, path, body }); }

    if (method === 'GET' && path === '/connections') return options.connection === 'missing' ? err('not_found', 404) : json([connection]);
    if (method === 'GET' && path === `/sessions/${SESSION_ID}/messages`) return json(messages);
    if (method === 'GET' && path === `/projects/${PROJECT_ID}/runs`) return json(run ? [run] : []);
    if (method === 'GET' && path === `/projects/${PROJECT_ID}/files`) {
      fileListCount += 1;
      return json([...baseFiles, ...(uploaded ? [{ ...uploaded, state: fileListCount > 3 ? 'ready' : 'preparing' }] : [])]);
    }
    if (method === 'POST' && path === `/projects/${PROJECT_ID}/files`) { uploaded = { id: 'f3333333-3333-4333-8333-333333333333', project_id: PROJECT_ID, filename: 'uploaded.csv', size: 5, content_type: 'text/csv', state: 'preparing' }; fileListCount = 0; return json(uploaded, 201); }
    if (method === 'DELETE') { deletes.push(path); return json({}); }
    if (method === 'GET' && path === `/artifacts/${REPORT_ID}/content`) return route.fulfill({ status: 200, contentType: 'text/markdown', body: options.reportMarkdown ?? REPORT_MARKDOWN });
    if (method === 'POST' && path === `/sessions/${SESSION_ID}/runs`) {
      const unknown = Object.keys(body ?? {}).filter((k) => !SUBMIT_FIELDS.includes(k));
      if (unknown.length || SUBMIT_FIELDS.some((k) => !(k in (body ?? {})))) return json({ detail: [{ type: 'extra_forbidden', loc: ['body'], msg: 'Extra inputs are not permitted' }] }, 422);
      submitCount += 1;
      if (gate) await gate;
      const existing = submitted.get(body.submission_key);
      if (existing) return json(existing, 201);
      if (options.failFirstSubmit && submitCount === 1) { submitted.set(body.submission_key, makeRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [] })); return route.abort('failed'); }
      const created = makeRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [] });
      submitted.set(body.submission_key, created); run = created; return json(created, 201);
    }
    const runMatch = /^\/runs\/([0-9a-f-]+)(\/.*)?$/.exec(path);
    if (runMatch) {
      const sub = runMatch[2] ?? '';
      if (offline) return err('request_failed', 503);
      if (method === 'GET' && sub === '') return run ? json(run) : err('not_found', 404);
      if (method === 'GET' && sub === '/events') return err('not_found', 404); // SSE only: a fetch client must use event-page
      if (method === 'GET' && sub === '/event-page') {
        const params = new URL(request.url()).searchParams; const after = Number(params.get('after') ?? 0); pageRequests.push(after);
        if (options.expireCursorOnce && !cursorExpired && after > 0) { cursorExpired = true; run = { ...run!, latest_cursor: 9, revision: run!.revision + 1 }; return json({ code: 'cursor_expired', message: 'cursor_expired', request_id: 'fixture', snapshot: run }, 410); }
        const size = options.pageSize ?? 100; const latest = events.at(-1)?.sequence ?? 0;
        return json({ events: events.filter((e) => e.sequence > after).slice(0, size), latest_cursor: latest });
      }
      if (method === 'GET' && sub === '/plan') return json(plan);
      if (method === 'PATCH' && sub === '/plan') { plan = makePlan(plan.revision + 1, body.plan.stages); run = { ...run!, revision: run!.revision + 1 }; return json(run); }
      if (method === 'POST' && sub === '/approve') {
        if (options.conflictOnApprove && body.expected_revision === 1) { plan = makePlan(3, ['search_literature']); return err('revision_conflict', 409); }
        run = makeRun({ run_id: NEW_RUN_ID, state: 'queued', artifacts: [] }); return json(run);
      }
      if (method === 'POST' && sub === '/stop') { run = { ...run!, state: 'stopping' }; return json(run); }
      if (method === 'POST' && sub === '/decisions') { // mirrors DecisionSubmit: exact fields, per-key idempotency
        const allowed = ['decision_id', 'expected_revision', 'idempotency_key', 'choice', 'result', 'add_tokens', 'add_elapsed_ms'];
        const bad = !body || Object.keys(body).some((k) => !allowed.includes(k)) || !['decision_id', 'expected_revision', 'idempotency_key', 'choice'].every((k) => k in body) || !/^[0-9a-f-]{36}$/.test(body.decision_id);
        if (bad) return json({ detail: [{ type: 'value_error', loc: ['body'], msg: 'invalid decision' }] }, 422);
        decisionCalls += 1;
        if (options.failFirstDecision && decisionCalls === 1) return route.abort('failed');
        const payload = JSON.stringify({ ...body, idempotency_key: undefined });
        const prior = receipts.get(body.idempotency_key);
        if (prior) return prior.payload === payload ? json(prior.run) : err('idempotency_conflict', 409);
        if (body.expected_revision !== run!.revision) return err('revision_conflict', 409);
        run = { ...run!, state: body.choice === 'stop' ? 'stopping' : 'queued', revision: run!.revision + 1 };
        receipts.set(body.idempotency_key, { payload, run: run! }); return json(run);
      }
    }
    return route.fallback();
  });

  return {
    writes, deletes,
    setRun: (patch: Partial<RunView>) => { run = { ...(run ?? makeRun()), ...patch }; },
    pushEvents: (...next: RunEvent[]) => { events = [...events, ...next]; },
    setOffline: (value: boolean) => { offline = value; },
    acknowledgeStop: () => { stopAcknowledged = true; run = { ...run!, state: 'canceled' }; },
    stopAcknowledged: () => stopAcknowledged,
    releaseSubmit: () => release?.(),
    submitCount: () => submitCount, pageRequests: () => pageRequests,
  };
}
