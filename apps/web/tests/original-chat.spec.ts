import { expect, test } from '@playwright/test';
import { CONNECTION_ID, NEW_RUN_ID, PROJECT_ID, SESSION_ID, SESSION_URL, installResearchFixtureRoutes, makeRun } from './fixtures/research';

test('empty conversation sample fills the question without submitting it', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: null, messages: [] });
  await page.goto(SESSION_URL);

  await page.getByRole('button', { name: 'Try a sample question' }).click();
  await expect(page.getByLabel('Research question')).toHaveValue(/temperature affects molecular diffusion/);
  expect(fixture.writes.filter((write) => write.method === 'POST' && write.path.endsWith('/runs'))).toHaveLength(0);
});

test('composer keeps failed files disabled and links to the project library', async ({ page }) => {
  await installResearchFixtureRoutes(page, { run: null, messages: [] });
  await page.goto(SESSION_URL);

  await page.locator('.chat-file-picker > summary').click();
  const addReady = page.getByRole('button', { name: 'Add to question: example.csv' });
  await expect(addReady).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Add to question: broken.csv' })).toBeDisabled();
  await addReady.click();
  await expect(page.getByRole('button', { name: 'Remove from question: example.csv' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Upload or manage files' })).toHaveAttribute('href', `/projects/${PROJECT_ID}/library#outputs-files`);
});

test('configured connection notice and plan review control remain available', async ({ page }) => {
  await installResearchFixtureRoutes(page, { run: null, messages: [] });
  await page.goto(SESSION_URL);

  await expect(page.getByLabel('Current model')).toHaveValue(CONNECTION_ID);
  await expect(page.getByText(/Configured connection\. Live provider access is checked when a run starts\./)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Send question' })).toBeEnabled();
});

test('changing model after an unknown outcome gets a new submission key', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: null, messages: [], failFirstSubmit: true });
  const secondConnection = '13131313-1313-4313-8313-131313131313';
  await page.route('**/api/v1/connections', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify([
      { id: CONNECTION_ID, label: 'Fixture', provider: 'fixture-provider', model: 'fixture-model', state: 'ready', has_secret: true },
      { id: secondConnection, label: 'Second fixture', provider: 'second-provider', model: 'second-model', state: 'ready', has_secret: true },
    ]),
  }));
  await page.goto(SESSION_URL);
  await page.getByLabel('Research question').fill('Compare models safely');

  await page.getByRole('button', { name: 'Send question' }).click();
  await expect(page.getByRole('alert')).toContainText('could not confirm whether this request arrived');
  await page.getByLabel('Current model').selectOption(secondConnection);
  await page.getByRole('button', { name: 'Send question' }).click();
  await expect.poll(() => fixture.writes.filter((write) => write.method === 'POST' && write.path.endsWith('/runs')).length).toBe(2);

  const submissions = fixture.writes.filter((write) => write.method === 'POST' && write.path.endsWith('/runs'));
  expect(submissions[0].body.provider_id).toBe(CONNECTION_ID);
  expect(submissions[1].body.provider_id).toBe(secondConnection);
  expect(submissions[1].body.submission_key).not.toBe(submissions[0].body.submission_key);
});

test('search-scope edits block approval until a fresh plan preparation completes', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: null, messages: [] });
  let releasePreparation!: () => void;
  let markPreparationStarted!: () => void;
  const preparationGate = new Promise<void>((resolve) => { releasePreparation = resolve; });
  const preparationStarted = new Promise<void>((resolve) => { markPreparationStarted = resolve; });
  const preparedRun = makeRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [] });
  let preparedSearchTerms: string[] = [];
  let prepareCalls = 0;
  await page.route(`**/api/v1/runs/${NEW_RUN_ID}/prepare-plan`, async (route) => {
    prepareCalls += 1;
    preparedSearchTerms = route.request().postDataJSON().search_terms;
    if (prepareCalls === 1) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(preparedRun) });
      return;
    }
    markPreparationStarted();
    await preparationGate;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(preparedRun) });
  });
  await page.goto(SESSION_URL);
  await page.locator('.chat-options > summary').click();
  await page.getByLabel('Research workflow').selectOption('literature');
  await page.getByLabel('Research question').fill('What changes diffusion?');
  await page.getByRole('button', { name: 'Send question' }).click();
  await expect(page.getByRole('heading', { name: 'Review the plan' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Approve plan' })).toBeEnabled();

  await page.getByRole('button', { name: 'Adjust search scope' }).click();
  await page.getByLabel('Literature search term').fill('temperature and diffusion');
  await page.getByRole('button', { name: 'Save search scope' }).click();
  await expect(page.getByText('Search scope or workflow changed. Prepare the plan again before approving.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Approve plan' })).toBeDisabled();

  await page.getByRole('button', { name: 'Adjust search scope' }).click();
  await page.getByRole('button', { name: 'Save search scope' }).click();
  await expect(page.getByText('Search scope or workflow changed. Prepare the plan again before approving.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Approve plan' })).toBeDisabled();

  await page.getByRole('button', { name: 'Prepare plan' }).click();
  await preparationStarted;
  await expect(page.getByRole('button', { name: 'Adjust search scope' })).toBeDisabled();
  releasePreparation();
  await expect(page.getByText('Plan prepared. Review the current requirements and limits before approving.')).toBeVisible();
  await expect(page.getByText('Search scope or workflow changed. Prepare the plan again before approving.')).toHaveCount(0);
  expect(preparedSearchTerms).toEqual(['temperature and diffusion']);
  expect(fixture.writes.some((write) => write.method === 'POST' && write.path.endsWith('/approve'))).toBe(false);
});

for (const resource of ['runs', 'messages']) {
  test(`failed ${resource} read does not claim an empty conversation`, async ({ page }) => {
    await installResearchFixtureRoutes(page, { run: null, messages: [] });
    const path = resource === 'runs' ? `/api/v1/projects/${PROJECT_ID}/runs` : `/api/v1/sessions/${SESSION_ID}/messages`;
    await page.route(`**${path}`, (route) => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'storage_unavailable' } }) }));
    await page.goto(SESSION_URL);
    await expect(page.getByRole('alert')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Try a sample question' })).toBeHidden();
  });
}
