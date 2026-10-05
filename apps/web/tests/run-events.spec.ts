import { expect, test, type Page, type Route } from '@playwright/test';

const RUN_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const OTHER_RUN_ID = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';

type TestRun = { run_id: string; revision: number; state: string; latest_cursor: number; stage: string | null };
type TestEvent = { schema_version: 1; run_id: string; sequence: number; revision: number; occurred_at: string; kind: string; payload: Record<string, unknown> };

function makeRun(overrides: Partial<TestRun> = {}): TestRun {
  return { run_id: RUN_ID, revision: 1, state: 'running', latest_cursor: 0, stage: null, ...overrides };
}

function event(sequence: number, kind: string, payload: Record<string, unknown> = {}, runId = RUN_ID): TestEvent {
  return { schema_version: 1, run_id: runId, sequence, revision: sequence, occurred_at: `2026-10-05T00:00:0${sequence}Z`, kind, payload };
}

async function installEventSource(page: Page) {
  await page.addInitScript(() => {
    const w = window as typeof window & { __eventSources?: Array<{ url: string; closed: boolean; emit: (kind: string, value: unknown) => void; fail: () => void }> };
    w.__eventSources = [];
    class FixtureEventSource extends EventTarget {
      url: string;
      closed = false;
      constructor(url: string) {
        super();
        this.url = url;
        w.__eventSources!.push(this);
      }
      close() { this.closed = true; }
      emit(kind: string, value: unknown) {
        this.dispatchEvent(new MessageEvent(kind, { data: JSON.stringify(value) }));
      }
      fail() { this.dispatchEvent(new Event('error')); }
    }
    Object.defineProperty(window, 'EventSource', { configurable: true, value: FixtureEventSource });
  });
}

async function mountHook(page: Page) {
  await page.goto('/');
  await page.evaluate(async () => {
    const w = window as typeof window & { __runEventsSetId?: (value: string | null) => void; __runEventsRoot?: { unmount: () => void } };
    const transformedHook = await fetch('/src/useRunEvents.ts').then((response) => response.text());
    const reactPath = transformedHook.match(/from "([^\"]*react\.js\?v=[^\"]+)"/)?.[1];
    if (!reactPath) throw new Error('Vite did not expose the hook React dependency');
    const dependencyQuery = new URL(reactPath, location.href).search;
    const React = (await import(reactPath)).default;
    const { createRoot } = (await import(`/node_modules/.vite/deps/react-dom_client.js${dependencyQuery}`)).default;
    const { useRunEvents } = await import('/src/useRunEvents.ts');
    const container = document.createElement('div');
    container.id = 'run-events-harness';
    document.body.append(container);
    function Harness() {
      const [runId, setRunId] = React.useState((window as typeof window & { __initialRunId?: string }).__initialRunId ?? 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb');
      w.__runEventsSetId = setRunId;
      const state = useRunEvents(runId, { retryBaseDelayMs: 10, maxRetryDelayMs: 10, maxReconnectAttempts: 4 });
      return React.createElement('pre', { id: 'run-events-state' }, JSON.stringify({
        run: state.run,
        events: state.events,
        connected: state.connected,
        reconnecting: state.reconnecting,
      }));
    }
    w.__runEventsRoot = createRoot(container);
    w.__runEventsRoot.render(React.createElement(Harness));
  });
}

async function readState(page: Page) {
  return page.locator('#run-events-state').evaluate((node) => JSON.parse(node.textContent ?? '{}')) as Promise<{
    run: TestRun | null;
    events: TestEvent[];
    connected: boolean;
    reconnecting: boolean;
  }>;
}

async function emit(page: Page, sourceIndex: number, kind: string, item: TestEvent) {
  await page.evaluate(({ sourceIndex, kind, item }) => {
    const sources = (window as typeof window & { __eventSources?: Array<{ emit: (name: string, value: unknown) => void }> }).__eventSources!;
    sources[sourceIndex].emit(kind, item);
  }, { sourceIndex, kind, item });
}

async function failSource(page: Page, sourceIndex: number) {
  await page.evaluate((sourceIndex) => {
    const sources = (window as typeof window & { __eventSources?: Array<{ fail: () => void }> }).__eventSources!;
    sources[sourceIndex].fail();
  }, sourceIndex);
}

async function installApi(page: Page, handlers: (method: string, path: string, url: URL, route: Route) => Promise<void>) {
  const calls: Array<{ method: string; path: string; url: string }> = [];
  await page.route('**/api/v1/runs/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    calls.push({ method: request.method(), path: url.pathname, url: request.url() });
    await handlers(request.method(), url.pathname, url, route);
  });
  return calls;
}

const json = (route: Route, value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });

test('loads an ordered REST replay, then deduplicates and repairs gaps from SSE', async ({ page }) => {
  await installEventSource(page);
  const first = event(1, 'stage.started', { stage: 'search_literature' });
  const second = event(2, 'stage.completed', { stage: 'search_literature', outcome: 'completed' });
  const third = event(3, 'decision.required', { decision_id: 'aaaaaaaa-0000-4000-8000-000000000001', reason: 'budget_exhausted' });
  const pages: number[] = [];
  const calls = await installApi(page, async (method, path, url, route) => {
    if (method === 'GET' && path === `/api/v1/runs/${RUN_ID}`) return json(route, makeRun({ latest_cursor: 2 }));
    if (method === 'GET' && path === `/api/v1/runs/${RUN_ID}/event-page`) {
      const after = Number(url.searchParams.get('after'));
      pages.push(after);
      const events = after === 0 ? [first, second] : after === 2 ? [third] : after === 3 ? [event(4, 'stage.started', { stage: 'verify_references' }), event(5, 'usage.updated', { usage_tokens: 2 })] : [];
      return json(route, { events, latest_cursor: after >= 3 ? 5 : 3 });
    }
    throw new Error(`Unexpected request ${method} ${path}`);
  });
  await mountHook(page);
  await expect.poll(async () => (await readState(page)).events.map((item) => item.sequence)).toEqual([1, 2, 3]);
  await expect.poll(() => page.evaluate(() => (window as typeof window & { __eventSources?: unknown[] }).__eventSources?.length)).toBe(1);
  const sources = await page.evaluate(() => (window as typeof window & { __eventSources?: Array<{ url: string }> }).__eventSources!.map((source) => source.url));
  expect(sources[0]).toBe(`/api/v1/runs/${RUN_ID}/events?after=3`);
  expect(new URL(sources[0], 'http://127.0.0.1').search).not.toMatch(/token|secret|credential/i);
  await emit(page, 0, 'stage.completed', second);
  await emit(page, 0, 'decision.required', third);
  await expect.poll(async () => (await readState(page)).events.map((item) => item.sequence)).toEqual([1, 2, 3]);
  await emit(page, 0, 'usage.updated', event(5, 'usage.updated', { usage_tokens: 2 }));
  await expect.poll(async () => (await readState(page)).events.map((item) => item.sequence)).toEqual([1, 2, 3, 4, 5]);
  const state = await readState(page);
  expect(state.events.map((item) => item.sequence)).toEqual([1, 2, 3, 4, 5]);
  expect(calls.some((call) => call.method !== 'GET')).toBe(false);
  expect(pages).toEqual([0, 2, 3]);
});

test('reconnect snapshots and replays missed events without repeating work', async ({ page }) => {
  await installEventSource(page);
  const recovered = event(2, 'stage.completed', { stage: 'search_literature', outcome: 'completed' });
  let snapshots = 0;
  let pageCalls = 0;
  const calls = await installApi(page, async (method, path, url, route) => {
    if (method === 'GET' && path === `/api/v1/runs/${RUN_ID}`) { snapshots += 1; return json(route, makeRun({ latest_cursor: snapshots > 1 ? 2 : 1 })); }
    if (method === 'GET' && path.endsWith('/event-page')) { pageCalls += 1; return json(route, { events: pageCalls > 1 ? [recovered] : [event(1, 'stage.started', { stage: 'search_literature' })], latest_cursor: pageCalls > 1 ? 2 : 1 }); }
    throw new Error(`Unexpected request ${method} ${path}`);
  });
  await mountHook(page);
  await expect.poll(async () => (await readState(page)).events.map((item) => item.sequence)).toEqual([1]);
  await failSource(page, 0);
  await expect.poll(async () => (await readState(page)).events.map((item) => item.sequence)).toEqual([1, 2]);
  await expect.poll(() => page.evaluate(() => (window as typeof window & { __eventSources?: unknown[] }).__eventSources?.length)).toBe(2);
  expect(snapshots).toBeGreaterThanOrEqual(2);
  expect(calls.every((call) => call.method === 'GET')).toBe(true);
  expect(calls.map((call) => call.path).some((path) => /submit|decision|provider/i.test(path))).toBe(false);
});

test('cursor expiry reconciles from snapshot and replays from zero without discarding retained events', async ({ page }) => {
  await installEventSource(page);
  const history = [1, 2, 3].map((sequence) => event(sequence, sequence === 1 ? 'stage.started' : 'usage.updated', { usage_tokens: sequence }));
  let expired = false;
  let pageCalls = 0;
  const cursors: number[] = [];
  await installApi(page, async (method, path, url, route) => {
    if (method === 'GET' && path === `/api/v1/runs/${RUN_ID}`) return json(route, makeRun({ latest_cursor: expired ? 3 : 2 }));
    if (method === 'GET' && path.endsWith('/event-page')) {
      pageCalls += 1;
      const after = Number(url.searchParams.get('after'));
      cursors.push(after);
      if (pageCalls === 1) return json(route, { events: history.slice(0, 2), latest_cursor: 2 });
      if (pageCalls === 2) {
        expired = true;
        return json(route, { code: 'cursor_expired', message: 'Resync required.', snapshot: makeRun({ latest_cursor: 3 }) }, 410);
      }
      return json(route, { events: history.filter((item) => item.sequence > after), latest_cursor: 3 });
    }
    throw new Error(`Unexpected request ${method} ${path}`);
  });
  await mountHook(page);
  await expect.poll(async () => (await readState(page)).events.map((item) => item.sequence)).toEqual([1, 2]);
  await failSource(page, 0);
  await expect.poll(async () => (await readState(page)).events.map((item) => item.sequence)).toEqual([1, 2, 3]);
  expect(pageCalls).toBeGreaterThanOrEqual(3);
  expect(cursors).toEqual([0, 2, 0]);
});

test('run changes close obsolete streams and unmount aborts active requests', async ({ page }) => {
  await installEventSource(page);
  let releaseSnapshot!: () => void;
  let secondSnapshot = false;
  const calls = await installApi(page, async (method, path, _url, route) => {
    if (method !== 'GET') throw new Error(`Unexpected ${method} ${path}`);
    if (path === `/api/v1/runs/${RUN_ID}`) return json(route, makeRun());
    if (path === `/api/v1/runs/${OTHER_RUN_ID}`) {
      secondSnapshot = true;
      await new Promise<void>((resolve) => { releaseSnapshot = resolve; });
      try { return await json(route, makeRun({ run_id: OTHER_RUN_ID })); } catch { return; }
    }
    if (path.endsWith('/event-page')) return json(route, { events: [], latest_cursor: 0 });
    throw new Error(`Unexpected request ${method} ${path}`);
  });
  await mountHook(page);
  await expect.poll(() => page.evaluate(() => (window as typeof window & { __eventSources?: unknown[] }).__eventSources?.length)).toBe(1);
  await page.evaluate((id) => (window as typeof window & { __runEventsSetId?: (value: string | null) => void }).__runEventsSetId!(id), OTHER_RUN_ID);
  await expect.poll(() => secondSnapshot).toBe(true);
  const oldClosed = await page.evaluate(() => (window as typeof window & { __eventSources?: Array<{ closed: boolean }> }).__eventSources![0].closed);
  expect(oldClosed).toBe(true);
  expect(calls.some((call) => call.path === `/api/v1/runs/${OTHER_RUN_ID}`)).toBe(true);
  await page.evaluate(() => (window as typeof window & { __runEventsRoot?: { unmount: () => void } }).__runEventsRoot!.unmount());
  await expect.poll(() => page.evaluate(() => (window as typeof window & { __eventSources?: Array<{ closed: boolean }> }).__eventSources!.every((source) => source.closed))).toBe(true);
  releaseSnapshot();
});

test('terminal state drains the final replay page before closing its stream', async ({ page }) => {
  await installEventSource(page);
  const finalEvent = event(3, 'artifact.ready', { artifact: { artifact_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd' } });
  let terminal = false;
  const calls = await installApi(page, async (method, path, url, route) => {
    if (method === 'GET' && path === `/api/v1/runs/${RUN_ID}`) return json(route, makeRun({ state: terminal ? 'completed' : 'running', latest_cursor: terminal ? 3 : 1 }));
    if (method === 'GET' && path.endsWith('/event-page')) {
      const after = Number(url.searchParams.get('after'));
      if (after === 0) return json(route, { events: [event(1, 'stage.completed', { stage: 'search_literature', outcome: 'completed' })], latest_cursor: terminal ? 2 : 1 });
      if (terminal && after === 2) return json(route, { events: [finalEvent], latest_cursor: 3 });
      return json(route, { events: [], latest_cursor: terminal ? 3 : 1 });
    }
    throw new Error(`Unexpected request ${method} ${path}`);
  });
  await mountHook(page);
  await expect.poll(() => page.evaluate(() => (window as typeof window & { __eventSources?: unknown[] }).__eventSources?.length)).toBe(1);
  terminal = true;
  await emit(page, 0, 'run.state', event(2, 'run.state', { state: 'completed' }));
  await expect.poll(async () => (await readState(page)).run?.state).toBe('completed');
  await expect.poll(async () => (await readState(page)).events.map((item) => item.sequence)).toEqual([1, 2, 3]);
  await expect.poll(() => page.evaluate(() => (window as typeof window & { __eventSources?: Array<{ closed: boolean }> }).__eventSources![0].closed)).toBe(true);
  expect(calls.some((call) => call.method !== 'GET')).toBe(false);
});
