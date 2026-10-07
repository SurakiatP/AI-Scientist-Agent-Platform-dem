import { expect, test } from '@playwright/test';
import {
  CONNECTION_ID,
  NEW_RUN_ID,
  READY_FILE,
  SESSION_URL,
  installResearchFixtureRoutes,
  makePlan,
  makeRun,
} from './fixtures/research';

const SECOND_CONNECTION = '13131313-1313-4313-8313-131313131313';

test('Review plan sends one preparation request using the submitted workflow, inputs, question, and model', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: null, messages: [], holdSubmit: true });
  await page.route('**/api/v1/connections', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify([
      { id: CONNECTION_ID, label: 'First fixture', provider: 'fixture-provider', model: 'first-model', state: 'ready', has_secret: true },
      { id: SECOND_CONNECTION, label: 'Second fixture', provider: 'second-provider', model: 'second-model', state: 'ready', has_secret: true },
    ]),
  }));
  const prepareBodies: any[] = [];
  await page.route(`**/api/v1/runs/${NEW_RUN_ID}/prepare-plan`, async (route) => {
    prepareBodies.push(route.request().postDataJSON());
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(makeRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [] })) });
  });
  await page.goto(SESSION_URL);
  await page.locator('.chat-options > summary').click();
  await page.getByLabel('Research workflow').selectOption('crossref_csv');
  await page.getByLabel('Crossref query').fill('submitted query');
  await page.getByLabel('CSV input file').selectOption(READY_FILE);
  await page.getByLabel('Numeric columns').fill('control');
  await page.getByLabel('Current model').selectOption(CONNECTION_ID);
  await page.getByLabel('Research question').fill('Submitted question');
  await page.getByRole('button', { name: 'Review plan' }).click();
  await expect.poll(() => fixture.submitCount()).toBe(1);

  await page.getByLabel('Research question').fill('Changed after submit');
  await page.getByLabel('Current model').selectOption(SECOND_CONNECTION);
  await page.getByLabel('Crossref query').fill('changed query');
  fixture.releaseSubmit();

  await expect.poll(() => prepareBodies.length).toBe(1);
  const createBody = fixture.writes.find((write) => write.method === 'POST' && write.path.endsWith('/runs'))?.body;
  expect(createBody).toMatchObject({ question: 'Submitted question', provider_id: CONNECTION_ID, model: 'first-model', input_ids: [READY_FILE] });
  expect(prepareBodies[0]).toMatchObject({ workflow: 'crossref_csv', search_terms: [], csv_selection: { crossref: { query: 'submitted query' }, csv_file_id: READY_FILE, numeric_columns: ['control'] } });
});

test('failed preparation is retained for explicit recovery and never resent on reload', async ({ page }) => {
  await installResearchFixtureRoutes(page, { run: null, messages: [] });
  let prepares = 0;
  await page.route(`**/api/v1/runs/${NEW_RUN_ID}/prepare-plan`, async (route) => {
    prepares += 1;
    await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ code: 'request_failed' }) });
  });
  await page.goto(SESSION_URL);
  await page.getByLabel('Research question').fill('A recoverable plan');
  await page.getByRole('button', { name: 'Review plan' }).click();
  await expect(page.getByRole('alert')).toContainText('will not be resent automatically');
  expect(prepares).toBe(1);

  await page.reload();
  await expect(page.getByRole('heading', { name: 'Review the plan' })).toBeVisible();
  await page.locator('.chat-next-question > summary').click();
  await expect(page.getByLabel('Research question')).toHaveValue('A recoverable plan');
  expect(prepares).toBe(1);
  await page.getByRole('button', { name: 'Prepare plan' }).click();
  await expect.poll(() => prepares).toBe(2);
});

test('workflow and file disclosures retain the draft across reload, and selected files stay visible', async ({ page }) => {
  await installResearchFixtureRoutes(page, { run: null, messages: [] });
  await page.goto(SESSION_URL);
  await page.getByLabel('Research question').fill('Keep this draft');
  await page.locator('.chat-options > summary').click();
  await page.getByLabel('Research workflow').selectOption('crossref_csv');
  await page.getByLabel('Crossref query').fill('persistent query');
  await page.getByLabel('CSV input file').selectOption(READY_FILE);
  await page.getByLabel('Numeric columns').fill('control');
  await page.locator('.chat-file-picker > summary').click();
  await page.getByRole('button', { name: 'Add to question: example.csv' }).click();
  await expect(page.locator('.chat-selected-inputs')).toContainText('example.csv');

  await page.reload();
  await expect(page.getByLabel('Research question')).toHaveValue('Keep this draft');
  await page.locator('.chat-options > summary').click();
  await expect(page.getByLabel('Research workflow')).toHaveValue('crossref_csv');
  await expect(page.getByLabel('Crossref query')).toHaveValue('persistent query');
  await expect(page.getByLabel('Numeric columns')).toHaveValue('control');
  await expect(page.locator('.chat-selected-inputs')).toContainText('example.csv');
});

test('awaiting approval shows no zero-stage progress, names plan cancellation, and warns on zero limits', async ({ page }) => {
  const run = makeRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [] });
  await installResearchFixtureRoutes(page, { run, messages: [] });
  const plan = { ...makePlan(), plan: { ...makePlan().plan, token_limit: 0, elapsed_limit_ms: 0 } };
  await page.route(`**/api/v1/runs/${NEW_RUN_ID}/plan`, (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(plan) }));
  await page.goto(`${SESSION_URL}?run=${NEW_RUN_ID}`);
  await expect(page.getByRole('button', { name: 'Cancel plan' })).toBeVisible();
  await expect(page.getByText('0 stages completed')).toHaveCount(0);
  await expect(page.getByRole('status').filter({ hasText: 'limits are 0' })).toBeVisible();
});
