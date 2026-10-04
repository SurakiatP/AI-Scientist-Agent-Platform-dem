import { expect, test } from '@playwright/test';
import { DECISION_BUDGET, DECISION_UNKNOWN, PROJECT_ID, SESSION_URL, event, installResearchFixtureRoutes, makeRun, report } from './fixtures/research';

test('expanded artifact preserves controls and focus', async ({ page }) => {
  await installResearchFixtureRoutes(page);
  await page.goto(SESSION_URL);
  await page.getByLabel('Diffusion').fill('0.7');
  const expand = page.getByRole('button', { name: 'Expand visual: Concentration profile' });
  await expand.click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByLabel('Diffusion')).toHaveValue('0.7');
  await expect(dialog.getByText('Position (mm)')).toBeVisible();
  await expect(dialog.getByText('Illustrative model profile')).toBeVisible();
  await dialog.getByRole('button', { name: 'Play' }).click();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(expand).toBeFocused();
  await expect(page.getByLabel('Diffusion')).toHaveValue('0.7');
  await expect(page.getByRole('button', { name: 'Pause' })).toBeVisible();
});

test('language change keeps the question, chart value and messages', async ({ page }) => {
  await installResearchFixtureRoutes(page);
  await page.goto(SESSION_URL);
  await page.getByLabel('Research question').fill('Does temperature change diffusion?');
  await page.getByLabel('Diffusion').fill('0.7');
  await page.getByRole('button', { name: 'TH', exact: true }).click();
  await expect(page.getByLabel('คำถามวิจัย')).toHaveValue('Does temperature change diffusion?');
  await expect(page.getByLabel('การแพร่')).toHaveValue('0.7');
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
  expect(fixture.writes.filter((w) => w.path.endsWith('/approve')).at(-1)!.body.expected_revision).toBe(3);
});

test('run states render distinctly with confirmed stage counts only', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ state: 'queued', artifacts: [] }) });
  await page.goto(SESSION_URL);
  await expect(page.getByRole('heading', { name: 'Queued to start' })).toBeVisible();
  fixture.pushEvents(event(1, 'stage.started', { stage: 'search_literature' }), event(2, 'stage.completed', { stage: 'search_literature', outcome: 'completed' }), event(3, 'stage.started', { stage: 'verify_references' }));
  fixture.setRun({ state: 'running', stage: 'verify_references' });
  await expect(page.getByRole('heading', { name: 'Running' })).toBeVisible();
  await expect(page.getByText('1 stages completed')).toBeVisible();
  await expect(page.getByText('Current stage: Verify references')).toBeVisible();
  await expect(page.getByText('%')).toHaveCount(0);

  fixture.pushEvents(event(4, 'decision.required', { decision_id: DECISION_BUDGET, reason: 'budget_exhausted', required_tokens: 37, required_elapsed_ms: null }));
  fixture.setRun({ state: 'waiting_input', reserved_tokens: 5 });
  await expect(page.getByRole('heading', { name: 'Waiting for your decision' })).toBeVisible();
  await expect(page.getByText('Required to continue: 37 more tokens.')).toBeVisible();

  fixture.pushEvents(event(5, 'decision.required', { decision_id: DECISION_UNKNOWN, reason: 'unknown_outcome' }));
  await expect(page.getByText(/5 reserved tokens are retained/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Use a verified result' })).toBeDisabled();
  await expect(page.getByRole('button', { name: /Retry \(may duplicate cost\)/ })).toBeVisible();
  await page.getByRole('button', { name: /Retry \(may duplicate cost\)/ }).click();
  await expect.poll(() => fixture.writes.find((w) => w.path.endsWith('/decisions'))?.body).toMatchObject({ decision_id: DECISION_UNKNOWN, choice: 'retry' });

  fixture.pushEvents(event(6, 'stage.completed', { stage: 'verify_references', outcome: 'failed' }));
  fixture.setRun({ state: 'failed', error_code: 'storage_unavailable', artifacts: [{ ...report, partial: true }] });
  await expect(page.getByRole('alert').filter({ hasText: 'Verify references' })).toContainText('Partial outputs are kept below.');
});

test('stop stays pending until acknowledged and a lost connection never claims it stopped', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ state: 'running', artifacts: [] }) });
  await page.goto(SESSION_URL);
  await page.getByRole('button', { name: 'Stop', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Stopping, waiting for confirmation' })).toBeVisible();
  await expect.poll(() => fixture.writes.some((w) => w.path.endsWith('/stop'))).toBe(true);
  await expect(page.getByRole('button', { name: 'Stopping…' })).toBeDisabled();
  fixture.setOffline(true);
  await expect(page.getByText(/Connection lost.*may still be active/)).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Stopping, waiting for confirmation' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Stopped' })).toHaveCount(0);
  fixture.setOffline(false);
  await expect(page.getByText('Connected', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Stopping, waiting for confirmation' })).toBeVisible();
  fixture.acknowledgeStop();
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

test('an unconfirmed submission reuses its key', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: null, failFirstSubmit: true });
  await page.goto(SESSION_URL);
  await page.getByLabel('Research question').fill('Q');
  await page.getByRole('button', { name: 'Review plan' }).click();
  await expect(page.getByRole('alert')).toContainText('could not confirm');
  await page.getByRole('button', { name: 'Review plan' }).click();
  await expect(page.getByText('Review the plan')).toBeVisible();
  const keys = fixture.writes.filter((w) => w.path.endsWith('/runs')).map((w) => w.body.submission_key);
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
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
  await page.goto(SESSION_URL);
  await expect(page.getByRole('listitem').filter({ hasText: 'Search literature' })).toBeVisible();
  fixture.pushEvents(event(2, 'stage.completed', { stage: 'search_literature', outcome: 'completed' }));
  await expect.poll(() => fixture.pageRequests().filter((n) => n === 0).length).toBeGreaterThanOrEqual(2);
  await expect(page.getByText('Connected', { exact: true })).toBeVisible();
  await expect(page.getByText('1 stages completed')).toBeVisible();
  await expect(page.getByRole('listitem').filter({ hasText: 'Search literature' })).toHaveCount(1);
});

test('a stale snapshot is ignored by latest_cursor, not revision', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ state: 'running', artifacts: [], latest_cursor: 5 }) });
  await page.goto(SESSION_URL);
  await expect(page.getByRole('heading', { name: 'Running' })).toBeVisible();
  fixture.setRun({ latest_cursor: 2, state: 'waiting_input' });
  const seen = fixture.pageRequests().length;
  await expect.poll(() => fixture.pageRequests().length).toBeGreaterThan(seen + 2);
  await expect(page.getByRole('heading', { name: 'Running' })).toBeVisible();
  fixture.setRun({ latest_cursor: 6 });
  await expect(page.getByRole('heading', { name: 'Waiting for your decision' })).toBeVisible();
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
