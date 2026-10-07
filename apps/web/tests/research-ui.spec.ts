import { expect, test, type Page } from '@playwright/test';
import type { RunEvent } from '../../../contracts/api-types';
import { CONNECTION_ID, DECISION_BUDGET, DECISION_UNKNOWN, NEW_RUN_ID, PROJECT_ID, RUN_ID, SESSION_ID, SESSION_URL, event, installResearchFixtureRoutes, makeRun, report } from './fixtures/research';
import { OTHER_PROJECT_ID, REPORT_ID } from './fixtures/project';

type ChatEventSource = { url: string; closed: boolean; emit: (item: unknown) => void; fail: () => void };

async function installChatEventSource(page: Page) {
  await page.addInitScript(() => {
    const w = window as typeof window & { __chatEventSources?: ChatEventSource[] };
    w.__chatEventSources = [];
    class FixtureEventSource extends EventTarget {
      url: string;
      closed = false;
      constructor(url: string) {
        super();
        this.url = url;
        w.__chatEventSources!.push(this);
        queueMicrotask(() => { if (!this.closed) this.dispatchEvent(new Event('open')); });
      }
      close() { this.closed = true; }
      emit(item: unknown) {
        const value = item as { kind: string };
        this.dispatchEvent(new MessageEvent(value.kind, { data: JSON.stringify(item) }));
      }
      fail() { this.dispatchEvent(new Event('error')); }
    }
    Object.defineProperty(window, 'EventSource', { configurable: true, value: FixtureEventSource });
  });
}

async function chatStreams(page: Page) {
  return page.evaluate(() => (window as typeof window & { __chatEventSources?: ChatEventSource[] }).__chatEventSources?.map(({ url, closed }) => ({ url, closed })) ?? []);
}

async function emitChatEvent(page: Page, index: number, item: unknown) {
  await page.evaluate(({ index, item }) => (window as typeof window & { __chatEventSources: ChatEventSource[] }).__chatEventSources[index].emit(item), { index, item });
}

async function failChatStream(page: Page, index: number) {
  await page.evaluate((index) => (window as typeof window & { __chatEventSources: ChatEventSource[] }).__chatEventSources[index].fail(), index);
}

test('notebook workspace keeps scoped navigation, messages, outputs, and expanded artifact usable on mobile', async ({ page }) => {
  await installResearchFixtureRoutes(page, { messages: [
    { id: 'm1', sequence: 1, role: 'user', content: 'Original question about diffusion' },
    { id: 'm2', sequence: 2, role: 'assistant', content: 'Compare the available evidence.' },
  ] });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(SESSION_URL);

  const projectNav = page.getByRole('complementary', { name: 'Project and sessions' });
  const conversation = page.getByRole('region', { name: 'Conversation' });
  const outputs = page.getByRole('complementary', { name: 'Outputs' });
  await expect(projectNav.getByText('Diffusion study', { exact: true })).toBeVisible();
  await expect(projectNav.getByRole('link', { name: 'Temperature effects', exact: true })).toHaveAttribute('href', `/projects/${PROJECT_ID}/sessions/44444444-4444-4444-8444-444444444444`);
  await expect(conversation.getByText('Original question about diffusion')).toBeVisible();
  await expect(conversation.getByText('Compare the available evidence.')).toBeVisible();
  expect(await conversation.locator('.notebook-message.owner').evaluate((node) => getComputedStyle(node).alignSelf)).toBe('flex-end');
  expect(await conversation.locator('.notebook-message.assistant').evaluate((node) => getComputedStyle(node).alignSelf)).toBe('flex-start');
  expect(await conversation.locator('ol').evaluate((node) => getComputedStyle(node).listStyleType)).toBe('none');
  await expect(outputs.getByText('Concentration profile', { exact: true })).toBeVisible();
  const columns = await Promise.all([projectNav, conversation, outputs].map((region) => region.boundingBox()));
  expect(columns.every(Boolean)).toBe(true);
  expect(columns[0]!.x + columns[0]!.width).toBeLessThan(columns[1]!.x);
  expect(columns[1]!.x + columns[1]!.width).toBeLessThan(columns[2]!.x);

  const expand = outputs.getByRole('button', { name: 'Expand visual: Concentration profile' });
  await expand.click();
  const dialog = page.getByRole('dialog', { name: 'Concentration profile' });
  await expect(dialog.getByText('Diffusion study', { exact: true })).toBeVisible();
  await expect(dialog.getByText('Project', { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/output=dddddddd-dddd-4ddd-8ddd-dddddddddddd/);
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(expand).toBeFocused();
  await expect(page).not.toHaveURL(/output=/);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  const mobileNav = page.locator('details.mobile-project-navigation');
  await mobileNav.locator('summary').click();
  await expect(mobileNav.getByRole('link', { name: 'Temperature effects', exact: true })).toBeVisible();
});

async function publishChatEvents(page: Page, fixture: { pushEvents: (...items: RunEvent[]) => void }, ...items: RunEvent[]) {
  fixture.pushEvents(...items);
  const streams = await chatStreams(page);
  const index = streams.length - 1;
  for (const item of items) await emitChatEvent(page, index, item);
}

test('expanded artifact preserves fetched values and focus', async ({ page }) => {
  await installResearchFixtureRoutes(page);
  await page.goto(SESSION_URL);
  await expect(page.getByRole('cell', { name: '8.25' })).toBeVisible();
  const expand = page.getByRole('button', { name: 'Expand visual: Concentration profile' });
  await expand.click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('cell', { name: '8.25' })).toBeVisible();
  await expect(dialog.getByRole('columnheader', { name: 'measured concentration' })).toBeVisible();
  await expect(dialog.getByRole('link', { name: 'Download output' })).toHaveAttribute('href', /\/artifacts\/.*\/content$/);
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(expand).toBeFocused();
  await expect(page.getByRole('cell', { name: '8.25' })).toBeVisible();
  await expect(page.getByLabel('Diffusion')).toHaveCount(0);
});

test('language change keeps the question, fetched values and messages', async ({ page }) => {
  await installResearchFixtureRoutes(page);
  await page.goto(SESSION_URL);
  await page.getByLabel('Research question').fill('Does temperature change diffusion?');
  await expect(page.getByRole('cell', { name: '8.25' })).toBeVisible();
  await page.getByRole('button', { name: 'TH', exact: true }).click();
  await expect(page.getByLabel('คำถามวิจัย')).toHaveValue('Does temperature change diffusion?');
  await expect(page.getByRole('cell', { name: '8.25' })).toBeVisible();
  await expect(page.getByText('Original question about diffusion')).toBeVisible();
  await page.getByRole('button', { name: 'EN', exact: true }).click();
  await expect(page.getByLabel('Research question')).toHaveValue('Does temperature change diffusion?');
});

test('approving an edited plan sends its exact new revision and digest', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: null });
  await page.goto(SESSION_URL);
  await page.getByLabel('Research question').fill('Is the reference real?');
  await page.getByRole('button', { name: 'Review plan' }).click();
  await expect(page.getByText('Fixture / fixture-model')).toBeVisible();
  await expect(page.getByText('numpy 2.0.0')).toBeVisible();
  await page.getByLabel('Research stages (one per line)').fill('search_literature');
  await expect(page.getByRole('button', { name: 'Approve plan' })).toBeDisabled();
  await page.getByRole('button', { name: 'Save edits' }).click();
  await expect(page.getByText('Edits saved')).toBeVisible();
  await page.getByRole('button', { name: 'Approve plan' }).click();
  await expect(page.getByRole('heading', { name: 'Queued to start' })).toBeVisible();
  const approve = fixture.writes.find((w) => w.path.endsWith('/approve'))!;
  expect(approve.body).toEqual({ expected_revision: 2, plan_digest: `${'d'.repeat(63)}2` });
  const patch = fixture.writes.find((w) => w.method === 'PATCH')!;
  expect(patch.body.expected_revision).toBe(1);
  expect(patch.body.plan.stages).toEqual(['search_literature']);
  expect(patch.body.plan.input_snapshot_digest).toHaveLength(64);
});

test('revision conflict refreshes the plan instead of approving it', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: null, conflictOnApprove: true });
  await page.goto(SESSION_URL);
  await page.getByLabel('Research question').fill('Q');
  await page.getByRole('button', { name: 'Review plan' }).click();
  await page.getByRole('button', { name: 'Approve plan' }).click();
  await expect(page.getByText('The plan changed')).toBeVisible();
  await expect(page.getByLabel('Research stages (one per line)')).toHaveValue('search_literature');
  await page.getByRole('button', { name: 'Approve plan' }).click();
  await expect.poll(() => fixture.writes.filter((w) => w.path.endsWith('/approve')).at(-1)!.body.expected_revision).toBe(3);
});

test('run states render distinctly with confirmed stage counts only', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ state: 'queued', artifacts: [] }) });
  await installChatEventSource(page);
  await page.goto(SESSION_URL);
  await expect(page.getByRole('heading', { name: 'Queued to start' })).toBeVisible();
  await expect.poll(async () => (await chatStreams(page)).length).toBe(1);

  const startedRun = event(1, 'run.state', { state: 'running' });
  const searchStarted = event(2, 'stage.started', { stage: 'search_literature' });
  const searchCompleted = event(3, 'stage.completed', { stage: 'search_literature', outcome: 'completed' });
  const verifyStarted = event(4, 'stage.started', { stage: 'verify_references' });
  fixture.setRun({ state: 'running', stage: 'verify_references' });
  await publishChatEvents(page, fixture, startedRun, searchStarted, searchCompleted, verifyStarted);
  await expect(page.getByRole('heading', { name: 'Running' })).toBeVisible();
  await expect(page.getByText('1 stages completed')).toBeVisible();
  await expect(page.getByText('Current stage: Verify references')).toBeVisible();
  await expect(page.getByText('%')).toHaveCount(0);

  const completedRun = event(5, 'run.state', { state: 'completed' });
  fixture.setRun({ state: 'completed', stage: null });
  await publishChatEvents(page, fixture, completedRun);
  await expect(page.getByRole('heading', { name: 'Completed' })).toBeVisible();
  await expect(page.getByText('1 stages completed')).toBeVisible();
});

test('stop stays pending until acknowledged and a lost connection never claims it stopped', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ state: 'running', artifacts: [] }) });
  await installChatEventSource(page);
  await page.goto(SESSION_URL);
  await expect.poll(async () => (await chatStreams(page)).length).toBe(1);
  await page.getByRole('button', { name: 'Stop', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Stopping, waiting for confirmation' })).toBeVisible();
  await expect.poll(() => fixture.writes.some((w) => w.path.endsWith('/stop'))).toBe(true);
  await expect(page.getByRole('button', { name: 'Stopping…' })).toBeDisabled();
  await failChatStream(page, 0);
  await expect(page.getByText(/Connection lost.*may still be active/)).toBeVisible();
  await page.getByRole('button', { name: 'TH', exact: true }).click();
  await expect(page.getByText(/การเชื่อมต่อขาดหาย.*งานอาจยังทำงานอยู่/)).toBeVisible();
  await page.getByRole('button', { name: 'EN', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Stopping, waiting for confirmation' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Stopped' })).toHaveCount(0);
  await expect.poll(async () => (await chatStreams(page)).length).toBe(2);
  await expect(page.getByText('Connected', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Stopping, waiting for confirmation' })).toBeVisible();
  fixture.acknowledgeStop();
  await emitChatEvent(page, 1, event(1, 'run.state', { state: 'canceled' }));
  await expect(page.getByRole('heading', { name: 'Stopped' })).toBeVisible();
});

test('duplicate creation is blocked while pending and reconciled with one key', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: null, holdSubmit: true });
  await page.goto(SESSION_URL);
  await page.getByLabel('Research question').fill('Q');
  await page.getByRole('button', { name: 'Review plan' }).click();
  await expect(page.getByRole('button', { name: 'Creating…' })).toBeDisabled();
  fixture.releaseSubmit();
  await expect(page.getByText('Review the plan')).toBeVisible();
  expect(fixture.submitCount()).toBe(1);
});

test('new run URL survives refresh and preserves conversation query state', async ({ page }) => {
  await installResearchFixtureRoutes(page, { run: null });
  await page.goto(`${SESSION_URL}?output=${REPORT_ID}&source=plan`);
  await page.getByLabel('Research question').fill('Q');
  await page.getByRole('button', { name: 'Review plan' }).click();
  await expect(page.getByRole('region', { name: 'Plan review' })).toBeVisible();

  const createdUrl = new URL(page.url());
  expect(createdUrl.pathname).toBe(SESSION_URL);
  expect(createdUrl.searchParams.get('run')).toBe(NEW_RUN_ID);
  expect(createdUrl.searchParams.get('output')).toBe(REPORT_ID);
  expect(createdUrl.searchParams.get('source')).toBe('plan');

  await page.reload();
  await expect(page.getByRole('region', { name: 'Plan review' })).toBeVisible();
  expect(new URL(page.url()).searchParams.get('run')).toBe(NEW_RUN_ID);
});

test('late run creation does not navigate away after the conversation changes', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: null, holdSubmit: true });
  await page.goto(SESSION_URL);
  await page.getByLabel('Research question').fill('Q');
  await page.getByRole('button', { name: 'Review plan' }).click();
  await expect.poll(() => fixture.submitCount()).toBe(1);

  const response = page.waitForResponse((item) =>
    item.url().includes(`/api/v1/sessions/${SESSION_ID}/runs`) && item.status() === 201,
  );
  await page.getByRole('link', { name: 'Project details' }).click();
  fixture.releaseSubmit();
  await response;
  await expect(page).toHaveURL(new RegExp(`/projects/${PROJECT_ID}$`));
});

test('late run creation keeps an owner-selected run in the URL', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { holdSubmit: true });
  await page.goto(SESSION_URL);
  await page.getByLabel('Research question').fill('Q');
  await page.getByRole('button', { name: 'Review plan' }).click();
  await expect.poll(() => fixture.submitCount()).toBe(1);

  await page.evaluate((path) => {
    window.history.pushState(window.history.state, '', path);
    window.dispatchEvent(new PopStateEvent('popstate', { state: window.history.state }));
  }, `${SESSION_URL}?run=${RUN_ID}`);
  await expect(page).toHaveURL(new RegExp(`run=${RUN_ID}`));

  const response = page.waitForResponse((item) =>
    item.url().includes(`/api/v1/sessions/${SESSION_ID}/runs`) && item.status() === 201,
  );
  fixture.releaseSubmit();
  await response;
  await expect(page).toHaveURL(new RegExp(`run=${RUN_ID}`));
});

test('an unconfirmed submission reuses its key', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: null, failFirstSubmit: true });
  await page.goto(SESSION_URL);
  await page.getByLabel('Research question').fill('Q');
  await page.getByRole('button', { name: 'Review plan' }).click();
  await expect(page.getByRole('alert')).toContainText('could not confirm');
  fixture.setRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [] });
  await page.getByRole('button', { name: 'Review plan' }).click();
  await expect(page.getByText('Review the plan')).toBeVisible();
  const keys = fixture.writes.filter((w) => w.path.endsWith('/runs')).map((w) => w.body.submission_key);
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
});

test('composer waits for model readiness before allowing plan review', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: null });
  let releaseConnections!: () => void;
  let signalConnectionsRequested!: () => void;
  const connectionGate = new Promise<void>((resolve) => { releaseConnections = resolve; });
  const connectionsRequested = new Promise<void>((resolve) => { signalConnectionsRequested = resolve; });
  await page.route('**/api/v1/connections', async (route) => {
    signalConnectionsRequested();
    await connectionGate;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([{
      id: CONNECTION_ID, label: 'Fixture', provider: 'fixture-provider', model: 'fixture-model',
      state: 'ready', has_secret: true,
    }]) });
  });

  await page.goto(SESSION_URL);
  await page.getByLabel('Research question').fill('Does temperature change diffusion?');
  await connectionsRequested;
  const reviewButton = page.getByRole('button', { name: 'Review plan' });
  await expect(reviewButton).toBeDisabled();

  releaseConnections();
  await expect(reviewButton).toBeEnabled();
  await expect(page.getByRole('alert')).toHaveCount(0);
  await reviewButton.click();
  await expect(page.getByRole('region', { name: 'Plan review' })).toBeVisible();
  expect(fixture.writes.filter((write) => write.path.endsWith('/runs'))).toHaveLength(1);
});

test('composer rejects empty questions and unconfigured models with next actions', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: null, connection: 'unconfigured' });
  await page.goto(SESSION_URL);
  await page.getByRole('button', { name: 'Review plan' }).click();
  await expect(page.getByRole('alert')).toContainText('Enter a research question');
  await page.getByLabel('Research question').fill('Q');
  await page.getByRole('button', { name: 'Review plan' }).click();
  await expect(page.getByRole('alert')).toContainText('No model is ready');
  await expect(page.getByRole('link', { name: 'Open Settings' })).toBeVisible();
  expect(fixture.writes.filter((w) => w.path.endsWith('/runs'))).toHaveLength(0);
});

test('shared files are selected only when ready and removal from the question deletes nothing', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: null });
  await page.goto(`/projects/${PROJECT_ID}/library`);
  await page.locator('#library-upload').setInputFiles({ name: 'uploaded.csv', mimeType: 'text/csv', buffer: Buffer.from('a,b\n') });
  await expect(page.getByText('uploaded.csv')).toBeVisible();
  await page.goto(SESSION_URL);
  await expect(page.getByRole('button', { name: 'Add to question: broken.csv' })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Add to question: uploaded.csv' })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Add to question: uploaded.csv' })).toBeEnabled({ timeout: 10_000 });
  await page.getByRole('button', { name: 'Add to question: example.csv' }).click();
  await expect(page.getByRole('button', { name: 'Remove from question: example.csv' })).toBeVisible();
  await page.getByRole('button', { name: 'Remove from question: example.csv' }).click();
  await expect(page.getByRole('button', { name: 'Remove from question: example.csv' })).toHaveCount(0);
  await page.getByRole('link', { name: 'Project details' }).click();
  await expect(page.getByRole('link', { name: 'example.csv' })).toBeVisible();
  expect(fixture.deletes).toEqual([]);
});

test('retry preloads the prior question and keeps partial outputs separate', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ state: 'failed', error_code: 'storage_unavailable', artifacts: [{ ...report, partial: true }] }) });
  await page.goto(SESSION_URL);
  await expect(page.locator('.artifact-provenance', { hasText: 'Partial output' })).toBeVisible();
  await page.getByRole('button', { name: 'Review and retry' }).click();
  await expect(page.getByLabel('Research question')).toHaveValue('Original question about diffusion');
  await expect(page.getByText('Previous run (kept)')).toBeVisible();
  await expect(page.locator('.artifact-provenance', { hasText: 'Partial output' })).toBeVisible();
  await page.getByRole('button', { name: 'Review plan' }).click();
  await expect(page.getByText('Review the plan')).toBeVisible();
  await expect.poll(() => fixture.writes.find((w) => w.path.endsWith('/runs'))?.body.question).toBe('Original question about diffusion');
  expect(fixture.writes.find((w) => w.path.endsWith('/runs'))!.body).not.toHaveProperty('retry_of');
});

test('reports expose copyable code and selectable equation source', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await installResearchFixtureRoutes(page);
  await page.goto(SESSION_URL);
  await expect(page.locator('.report-text .katex annotation')).toHaveText('E = mc^2');
  await page.getByRole('button', { name: 'Copy code' }).click();
  await expect(page.getByText('Copied', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('print("diffusion")');
});

test('submit sends exactly the server fields and no retry_of', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: null });
  await page.goto(SESSION_URL);
  await page.getByLabel('Research question').fill('Q');
  await page.getByRole('button', { name: 'Add to question: example.csv' }).click();
  await page.getByRole('button', { name: 'Review plan' }).click();
  await expect(page.getByText('Review the plan')).toBeVisible();
  const body = fixture.writes.find((w) => w.path === `/sessions/22222222-2222-4222-8222-222222222222/runs`)!.body;
  expect(Object.keys(body).sort()).toEqual(['input_ids', 'model', 'provider_id', 'question', 'submission_key']);
  expect(body.input_ids).toEqual(['f1111111-1111-4111-8111-111111111111']);
});

test('messages and files still load when the connections route is missing', async ({ page }) => {
  await installResearchFixtureRoutes(page, { run: null, connection: 'missing' });
  await page.goto(SESSION_URL);
  await expect(page.getByText('Original question about diffusion')).toBeVisible();
  await expect(page.getByText('example.csv · Ready')).toBeVisible();
  await expect(page.getByText('Settings unavailable.')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open Settings' })).toBeVisible();
});

test('event paging drains every page after the run is terminal and never fetches the SSE route', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ state: 'completed', artifacts: [], latest_cursor: 3 }), pageSize: 1 });
  fixture.pushEvents(event(1, 'stage.started', { stage: 'search_literature' }), event(2, 'stage.completed', { stage: 'search_literature', outcome: 'completed' }), event(3, 'stage.completed', { stage: 'verify_references', outcome: 'partial' }));
  await page.goto(SESSION_URL);
  await expect(page.getByText('2 stages completed')).toBeVisible();
  expect(fixture.pageRequests()).toEqual([0, 1, 2]);
});

test('an expired cursor re-pages from cursor 0 because events are never deleted', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ state: 'running', artifacts: [] }), expireCursorOnce: true });
  fixture.pushEvents(event(1, 'stage.started', { stage: 'search_literature' }));
  await installChatEventSource(page);
  await page.goto(SESSION_URL);
  await expect(page.getByRole('listitem').filter({ hasText: 'Search literature' })).toBeVisible();
  fixture.pushEvents(event(2, 'stage.completed', { stage: 'search_literature', outcome: 'completed' }));
  await failChatStream(page, 0);
  await expect.poll(async () => (await chatStreams(page)).length).toBe(2);
  await expect.poll(() => fixture.pageRequests().filter((n) => n === 0).length).toBeGreaterThanOrEqual(2);
  await expect(page.getByText('Connected', { exact: true })).toBeVisible();
  await expect(page.getByText('1 stages completed')).toBeVisible();
  await expect(page.getByRole('listitem').filter({ hasText: 'Search literature' })).toHaveCount(1);
});

test('stop and its acknowledgement never change the revision, and 422 uses detail', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ state: 'running', artifacts: [] }) });
  await page.goto(SESSION_URL);
  await page.getByRole('button', { name: 'Stop', exact: true }).click();
  await expect.poll(() => fixture.writes.some((w) => w.path.endsWith('/stop'))).toBe(true);
  const snapshot = await page.evaluate(async (id) => (await fetch(`/api/v1/runs/${id}`)).json(), makeRun().run_id);
  expect(snapshot.revision).toBe(1);
  fixture.acknowledgeStop();
  expect((await page.evaluate(async (id) => (await fetch(`/api/v1/runs/${id}`)).json(), makeRun().run_id)).revision).toBe(1);
  const bad = await page.evaluate(async (id) => { const r = await fetch(`/api/v1/runs/${id}/decisions`, { method: 'POST', body: '{"x":1}', headers: { 'Content-Type': 'application/json' } }); return r.json(); }, makeRun().run_id);
  expect(Array.isArray(bad.detail)).toBe(true);
});

test('budget decision sends the required amounts, revision and one reused key', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ state: 'waiting_input', artifacts: [], revision: 4 }), failFirstDecision: true });
  fixture.pushEvents(event(1, 'decision.required', { decision_id: DECISION_BUDGET, reason: 'budget_exhausted', required_tokens: 37, required_elapsed_ms: 2500 }));
  await page.goto(SESSION_URL);
  const extend = page.getByRole('button', { name: 'Extend limit and continue' });
  await extend.click();
  await expect(page.getByRole('alert')).toBeVisible();
  await extend.click();
  await expect(page.getByRole('heading', { name: 'Queued to start' })).toBeVisible();
  const calls = fixture.writes.filter((w) => w.path.endsWith('/decisions'));
  expect(calls).toHaveLength(2);
  expect(calls[0].body).toEqual({ decision_id: DECISION_BUDGET, expected_revision: 4, idempotency_key: expect.stringMatching(/^[0-9a-f-]{36}$/), choice: 'extend', add_tokens: 37, add_elapsed_ms: 2500 });
  expect(calls[1].body.idempotency_key).toBe(calls[0].body.idempotency_key);
});

test('double-click on a decision sends one request', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ state: 'waiting_input', artifacts: [] }) });
  fixture.pushEvents(event(1, 'decision.required', { decision_id: DECISION_UNKNOWN, reason: 'unknown_outcome' }));
  await page.goto(SESSION_URL);
  await page.getByRole('button', { name: /Retry \(may duplicate cost\)/ }).dblclick();
  await expect(page.getByRole('heading', { name: 'Queued to start' })).toBeVisible();
  expect(fixture.writes.filter((w) => w.path.endsWith('/decisions'))).toHaveLength(1);
});

test('unknown outcome offers retry and stop; verified result stays disabled with a reason', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ state: 'waiting_input', artifacts: [] }) });
  fixture.pushEvents(event(1, 'decision.required', { decision_id: DECISION_UNKNOWN, reason: 'unknown_outcome' }));
  await page.goto(SESSION_URL);
  await expect(page.getByRole('button', { name: 'Use a verified result' })).toBeDisabled();
  await expect(page.getByText(/not available yet/)).toBeVisible();
  await page.getByRole('button', { name: 'Stop without retrying' }).click();
  await expect(page.getByRole('heading', { name: 'Stopping, waiting for confirmation' })).toBeVisible();
  const body = fixture.writes.find((w) => w.path.endsWith('/decisions'))!.body;
  expect(body).toMatchObject({ decision_id: DECISION_UNKNOWN, choice: 'stop' });
  expect(body).not.toHaveProperty('add_tokens');
});

test('a zero required amount is not shown', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ state: 'waiting_input', artifacts: [] }) });
  fixture.pushEvents(event(1, 'decision.required', { decision_id: DECISION_BUDGET, reason: 'budget_exhausted', required_tokens: 0, required_elapsed_ms: 0 }));
  await page.goto(SESSION_URL);
  await expect(page.getByText('The approved usage limit has been reached.')).toBeVisible();
  await expect(page.getByText(/Required/)).toHaveCount(0);
});

test('session draft is isolated per session and survives corrupt storage', async ({ page }) => {
  await installResearchFixtureRoutes(page, { run: null });
  await page.addInitScript(() => sessionStorage.setItem('research-draft:44444444-4444-4444-8444-444444444444', '{broken'));
  await page.goto(SESSION_URL);
  await page.getByLabel('Research question').fill('only for session one');
  await page.evaluate(() => { history.pushState({}, '', '/projects/11111111-1111-4111-8111-111111111111/sessions/44444444-4444-4444-8444-444444444444'); dispatchEvent(new PopStateEvent('popstate')); });
  await expect(page.getByLabel('Research question')).toHaveValue('');
});

test('progress copy never names implementation identities', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ state: 'running', artifacts: [] }) });
  fixture.pushEvents(event(1, 'stage.started', { stage: 'verify_references' }));
  await page.goto(SESSION_URL);
  const region = page.getByRole('region', { name: 'Research progress' });
  await expect(region).toContainText('Verify references');
  expect((await region.innerText()).toLowerCase()).not.toMatch(/hermes|a2a|mcp|agent|skill|container|docker|broker/);
});

const canceledRun = () => makeRun({ state: 'canceled', reserved_tokens: 12, artifacts: [], revision: 3 });
const unknown = () => event(1, 'decision.required', { decision_id: DECISION_UNKNOWN, reason: 'unknown_outcome' });

test('canceled run with reserved tokens confirms usage with csrf and one reused key', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: canceledRun(), failFirstDecision: true });
  fixture.pushEvents(unknown());
  await page.goto(SESSION_URL);
  await page.getByLabel('Tokens actually used').fill('7');
  await page.getByRole('checkbox', { name: 'I checked the provider records for this operation and confirm the amount above is actual usage.' }).check();
  const confirm = page.getByRole('button', { name: 'Confirm usage' });
  await confirm.click();
  await expect.poll(() => fixture.writes.filter((w) => w.path.endsWith('/decisions')).length).toBe(1);
  await expect(page.getByRole('alert')).toHaveCount(2); // terminal notice + request error
  await confirm.click();
  await expect(page.getByRole('button', { name: 'Confirm usage' })).toHaveCount(0);
  const calls = fixture.writes.filter((w) => w.path.endsWith('/decisions'));
  expect(calls).toHaveLength(2);
  expect(calls[0].body).toEqual({ decision_id: DECISION_UNKNOWN, expected_revision: 3, idempotency_key: expect.stringMatching(/^[0-9a-f-]{36}$/), choice: 'confirm_usage', usage_tokens: 7 });
  expect(calls[1].body.idempotency_key).toBe(calls[0].body.idempotency_key);
  expect(calls[0].headers['x-csrf-token']).toBeTruthy();
});

test('confirm usage panel is hidden unless canceled with reservations; Thai labels; conflict shows error', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ state: 'running', reserved_tokens: 12, artifacts: [] }) });
  fixture.pushEvents(unknown());
  await page.goto(SESSION_URL);
  await expect(page.getByRole('button', { name: 'Confirm usage' })).toHaveCount(0);
  fixture.setRun({ state: 'canceled', reserved_tokens: 0 });
  await page.reload();
  await expect(page.getByRole('button', { name: 'Confirm usage' })).toBeDisabled();
  fixture.setRun({ reserved_tokens: 12 });
  await page.addInitScript(() => localStorage.setItem('scientist-platform.language', 'th'));
  await page.reload();
  await expect(page.getByRole('button', { name: 'ยืนยันการใช้งานจริง' })).toBeVisible();
  fixture.setRun({ revision: 9 });
  await page.getByLabel('โทเคนที่ใช้จริง').fill('1');
  await page.getByRole('checkbox', { name: 'ฉันตรวจสอบบันทึกของผู้ให้บริการสำหรับขั้นตอนนี้แล้ว และยืนยันว่าจำนวนข้างต้นคือการใช้งานจริง' }).check();
  await page.getByRole('button', { name: 'ยืนยันการใช้งานจริง' }).click();
  await expect(page.getByRole('alert')).toHaveCount(2);
  await expect(page.getByRole('button', { name: 'ยืนยันการใช้งานจริง' })).toBeVisible();
});

test('confirm usage needs an explicit records check and is capped by this operation reservation', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, {
    run: makeRun({ state: 'canceled', reserved_tokens: 12, artifacts: [], revision: 3 }),
  });
  fixture.pushEvents(unknown());
  await page.route(`**/api/v1/runs/${RUN_ID}/pending-decisions`, (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify([{
      decision_id: DECISION_UNKNOWN,
      reason: 'unknown_outcome',
      required_tokens: null,
      required_elapsed_ms: null,
      operation_reserved_tokens: 6,
    }]),
  }));
  await page.goto(SESSION_URL);

  const usage = page.getByLabel('Tokens actually used');
  const confirm = page.getByRole('button', { name: 'Confirm usage' });
  await expect(usage).toHaveValue('');
  await usage.fill('8');
  const acknowledgement = page.getByRole('checkbox', { name: 'I checked the provider records for this operation and confirm the amount above is actual usage.' });
  await acknowledgement.check();
  await expect(confirm).toBeDisabled();
  await usage.fill('6');
  await expect(confirm).toBeDisabled();
  await acknowledgement.check();
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect.poll(() => fixture.writes.filter((w) => w.path.endsWith('/decisions')).length).toBe(1);
  expect(fixture.writes.find((w) => w.path.endsWith('/decisions'))?.body.usage_tokens).toBe(6);
});

test('resolving one canceled operation advances to the next durable pending decision', async ({ page }) => {
  const secondDecision = 'aaaaaaaa-0000-4000-8000-000000000003';
  const fixture = await installResearchFixtureRoutes(page, {
    run: makeRun({ state: 'canceled', reserved_tokens: 12, artifacts: [], revision: 3 }),
    pendingDecisions: [
      { decision_id: DECISION_UNKNOWN, reason: 'unknown_outcome', operation_reserved_tokens: 6 },
      { decision_id: secondDecision, reason: 'unknown_outcome', operation_reserved_tokens: 4 },
    ],
  });
  await page.goto(SESSION_URL);
  const usage = page.getByLabel('Tokens actually used');
  const acknowledgement = page.getByRole('checkbox', { name: 'I checked the provider records for this operation and confirm the amount above is actual usage.' });
  const confirm = page.getByRole('button', { name: 'Confirm usage' });

  await expect(usage).toHaveValue('');
  await usage.fill('2');
  await acknowledgement.check();
  await confirm.click();
  await expect.poll(() => fixture.writes.filter((write) => write.path.endsWith('/decisions')).length).toBe(1);
  await expect(usage).toHaveValue('');

  await usage.fill('1');
  await acknowledgement.check();
  await confirm.click();
  await expect.poll(() => fixture.writes.filter((write) => write.path.endsWith('/decisions')).length).toBe(2);
  await expect(page.getByRole('group', { name: 'Confirm usage' })).toHaveCount(0);
  expect(fixture.writes.filter((write) => write.path.endsWith('/decisions')).map((write) => write.body.decision_id))
    .toEqual([DECISION_UNKNOWN, secondDecision]);
});

test('decision conflict refreshes run and pending state without resubmitting', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, {
    run: makeRun({ state: 'canceled', reserved_tokens: 12, artifacts: [], revision: 3 }),
    pendingDecisions: [{ decision_id: DECISION_UNKNOWN, reason: 'unknown_outcome', operation_reserved_tokens: 6 }],
    conflictOnDecision: true,
  });
  await page.goto(SESSION_URL);
  await page.getByLabel('Tokens actually used').fill('1');
  await page.getByRole('checkbox', { name: 'I checked the provider records for this operation and confirm the amount above is actual usage.' }).check();
  await page.getByRole('button', { name: 'Confirm usage' }).click();

  await expect(page.getByRole('group', { name: 'Confirm usage' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Stopped' })).toBeVisible();
  expect(fixture.writes.filter((write) => write.path.endsWith('/decisions'))).toHaveLength(1);
});


test('zero usage requires and submits the provider-record acknowledgement', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, {
    run: makeRun({ state: 'canceled', reserved_tokens: 12, artifacts: [], revision: 3 }),
    pendingDecisions: [{ decision_id: DECISION_UNKNOWN, reason: 'unknown_outcome', operation_reserved_tokens: 6 }],
  });
  await page.goto(SESSION_URL);
  const usage = page.getByLabel('Tokens actually used');
  const acknowledgement = page.getByRole('checkbox', { name: 'I checked the provider records for this operation and confirm the amount above is actual usage.' });
  const confirm = page.getByRole('button', { name: 'Confirm usage' });

  await usage.fill('0');
  await expect(confirm).toBeDisabled();
  await acknowledgement.check();
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect.poll(() => fixture.writes.filter((write) => write.path.endsWith('/decisions')).length).toBe(1);
  expect(fixture.writes.find((write) => write.path.endsWith('/decisions'))?.body.usage_tokens).toBe(0);
});

test('Chat applies stage and state SSE, then reconnect replay keeps the conversation and stage count stable', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ state: 'queued', artifacts: [] }) });
  await installChatEventSource(page);
  await page.goto(SESSION_URL);
  await expect(page.getByRole('heading', { name: 'Queued to start' })).toBeVisible();
  await expect.poll(async () => (await chatStreams(page)).length).toBe(1);

  const running = event(1, 'run.state', { state: 'running' });
  const started = event(2, 'stage.started', { stage: 'search_literature' });
  fixture.pushEvents(running, started);
  await emitChatEvent(page, 0, running);
  await emitChatEvent(page, 0, started);
  await expect(page.getByRole('heading', { name: 'Running' })).toBeVisible();
  await expect(page.getByText('Current stage: Search literature')).toBeVisible();

  const completed = event(3, 'stage.completed', { stage: 'search_literature', outcome: 'completed' });
  const terminal = event(4, 'run.state', { state: 'completed' });
  fixture.pushEvents(completed, terminal);
  fixture.setRun({ state: 'completed', stage: null });
  await emitChatEvent(page, 0, completed);
  await failChatStream(page, 0);

  await expect(page.getByRole('heading', { name: 'Completed' })).toBeVisible();
  await expect(page.getByText('1 stages completed')).toBeVisible();
  await expect(page.getByRole('region', { name: 'Conversation' }).getByText('Original question about diffusion', { exact: true })).toHaveCount(1);
});

test('a session switch ignores its late stream and refresh resumes the current run snapshot', async ({ page }) => {
  await installResearchFixtureRoutes(page, { run: makeRun({ state: 'running', artifacts: [] }) });
  await installChatEventSource(page);
  await page.goto(SESSION_URL);
  await expect.poll(async () => (await chatStreams(page)).length).toBe(1);

  const otherSessionUrl = `${SESSION_URL.slice(0, SESSION_URL.lastIndexOf('/'))}/44444444-4444-4444-8444-444444444444`;
  await page.evaluate((url) => {
    history.pushState({}, '', url);
    dispatchEvent(new PopStateEvent('popstate'));
  }, otherSessionUrl);
  await expect.poll(async () => (await chatStreams(page))[0]?.closed).toBe(true);
  await emitChatEvent(page, 0, event(1, 'run.state', { state: 'failed' }));
  await expect(page.getByRole('heading', { name: 'Running' })).toHaveCount(0);

  await page.evaluate((url) => {
    history.pushState({}, '', url);
    dispatchEvent(new PopStateEvent('popstate'));
  }, SESSION_URL);
  await expect(page.getByRole('heading', { name: 'Running' })).toBeVisible();
  await expect.poll(async () => (await chatStreams(page)).length).toBe(2);
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Running' })).toBeVisible();
  await expect.poll(async () => (await chatStreams(page)).some((source) => source.url.includes(`/runs/${RUN_ID}/events?`))).toBe(true);
});

test('terminal run snapshots show final status instead of a disconnected active-run warning', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ state: 'completed', artifacts: [] }) });
  await page.goto(SESSION_URL);
  const progress = page.getByRole('region', { name: 'Research progress' });
  await expect(progress.getByText('Run finished.', { exact: true })).toBeVisible();
  await expect(progress.getByText(/may still be active/)).toHaveCount(0);

  fixture.setRun({ state: 'canceled', revision: 2 });
  await page.reload();
  await page.getByRole('button', { name: 'TH', exact: true }).click();
  const thaiProgress = page.getByRole('region', { name: 'ความคืบหน้าการวิจัย' });
  await expect(page.getByRole('heading', { name: 'หยุดแล้ว' })).toBeVisible();
  await expect(thaiProgress.getByText('งานสิ้นสุดแล้ว', { exact: true })).toBeVisible();
  await expect(thaiProgress.getByText(/งานอาจยังทำงานอยู่/)).toHaveCount(0);
});

test('an unlisted requested run is restored only when its project and session match', async ({ page }) => {
  await installResearchFixtureRoutes(page);
  let directRun = makeRun({ project_id: OTHER_PROJECT_ID, artifacts: [] });
  await page.route(`**/api/v1/projects/${PROJECT_ID}/runs`, (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
  await page.route(`**/api/v1/runs/${RUN_ID}`, (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(directRun) }));
  await page.goto(`${SESSION_URL}?run=${RUN_ID}`);
  await expect(page.getByRole('alert').filter({ hasText: 'requested run is unavailable' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Waiting approval' })).toHaveCount(0);

  directRun = makeRun({ state: 'awaiting_approval', artifacts: [] });
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Waiting approval' })).toBeVisible();
  await expect(page.getByRole('alert').filter({ hasText: 'requested run is unavailable' })).toHaveCount(0);
});

test('a delayed empty session bootstrap cannot erase a run submitted while it was pending', async ({ page }) => {
  await installResearchFixtureRoutes(page, { run: null });
  let releaseBootstrap!: () => void;
  let reportBootstrapStarted!: () => void;
  let reportBootstrapResponded!: () => void;
  const bootstrapGate = new Promise<void>((resolve) => { releaseBootstrap = resolve; });
  const bootstrapStarted = new Promise<void>((resolve) => { reportBootstrapStarted = resolve; });
  const bootstrapResponded = new Promise<void>((resolve) => { reportBootstrapResponded = resolve; });
  await page.route(`**/api/v1/projects/${PROJECT_ID}/runs`, async (route) => {
    reportBootstrapStarted();
    await bootstrapGate;
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    reportBootstrapResponded();
  });
  await page.goto(SESSION_URL);
  await bootstrapStarted;
  await page.getByLabel('Research question').fill('A fresh question');
  await page.getByRole('button', { name: 'Review plan' }).click();
  await expect(page.getByRole('heading', { name: 'Review the plan' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Waiting approval' })).toBeVisible();

  releaseBootstrap();
  await bootstrapResponded;
  await expect(page.getByRole('heading', { name: 'Review the plan' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Waiting approval' })).toBeVisible();
});

test('a stale REST snapshot cannot replace progress confirmed by a newer SSE event', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ state: 'running', artifacts: [] }) });
  await installChatEventSource(page);
  let snapshotCount = 0;
  await page.route(`**/api/v1/runs/${RUN_ID}`, async (route) => {
    snapshotCount += 1;
    const snapshot = snapshotCount === 1
      ? makeRun({ state: 'running', revision: 1, latest_cursor: 0, stage: null, artifacts: [] })
      : makeRun({ state: 'waiting_input', revision: 2, latest_cursor: 0, stage: 'search_literature', artifacts: [] });
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(snapshot) });
  });
  await page.goto(SESSION_URL);
  await expect(page.getByRole('heading', { name: 'Running' })).toBeVisible();
  await expect.poll(async () => (await chatStreams(page)).length).toBe(1);

  const confirmed = event(1, 'stage.started', { stage: 'verify_references' });
  fixture.pushEvents(confirmed);
  await emitChatEvent(page, 0, confirmed);
  await expect(page.getByText('Current stage: Verify references')).toBeVisible();
  await failChatStream(page, 0);

  await expect.poll(async () => (await chatStreams(page)).length).toBe(2);
  await expect(page.getByRole('heading', { name: 'Running' })).toBeVisible();
  await expect(page.getByText('Current stage: Verify references')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Waiting for your decision' })).toHaveCount(0);
  expect(snapshotCount).toBeGreaterThanOrEqual(2);
});
