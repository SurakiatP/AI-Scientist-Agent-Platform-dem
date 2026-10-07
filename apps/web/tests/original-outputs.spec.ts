import { expect, test } from '@playwright/test';
import { CITATION_ID, FILE_ID, OTHER_PROJECT_ID, PROJECT_ID, REPORT_ID, installProjectFixtureRoutes, runs } from './fixtures/project';

test('history restores the timeline from recorded events and keeps partial work explicit', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  const savedRun = { ...runs[0], latest_cursor: 1 };
  await page.route(`**/api/v1/runs/${savedRun.run_id}`, (route) => route.fulfill({ json: savedRun }));
  await page.route(`**/api/v1/runs/${savedRun.run_id}/event-page?**`, (route) => route.fulfill({ json: {
    latest_cursor: 1,
    events: [{ schema_version: 1, run_id: savedRun.run_id, sequence: 1, revision: savedRun.revision, occurred_at: '2026-10-07T00:00:00Z', kind: 'stage.completed', payload: { stage: 'Analysis', outcome: 'partial' } }],
  } }));
  await page.goto('/history');
  await page.getByRole('combobox', { name: 'Choose a project' }).selectOption(PROJECT_ID);
  const timeline = page.getByRole('region', { name: 'Run timeline' });
  await expect(timeline.getByText('Analysis · Partial')).toBeVisible();
  await expect(timeline.getByText('Analysis · Completed')).toBeHidden();
  await expect(page.getByRole('article', { name: 'Run details' }).getByRole('link', { name: 'View outputs', exact: true })).toHaveAttribute('href', `/projects/${PROJECT_ID}/library`);
  await page.getByRole('combobox', { name: 'Choose a project' }).selectOption(OTHER_PROJECT_ID);
  await expect(timeline).toHaveCount(0);
});

for (const routeName of ['sources', 'history']) {
  test(`${routeName} does not report an empty workspace before the project request succeeds`, async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('scientist-platform.language', 'en'));
    let finish!: () => void;
    const held = new Promise<void>((resolve) => { finish = resolve; });
    await page.route('**/api/v1/projects', async (route) => {
      await held;
      await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'storage_unavailable' } }) });
    });
    await page.goto(`/${routeName}`);
    await expect(page.getByRole('status')).toContainText('Loading');
    await expect(page.getByText('No projects are available yet.')).toBeHidden();
    await expect(page.getByText('Create a project to begin your first research run.')).toBeHidden();
    finish();
    await expect(page.getByRole('alert')).toBeVisible();
    await expect(page.getByRole('status')).toBeHidden();
    await expect(page.getByText('No projects are available yet.')).toBeHidden();
    await expect(page.getByText('Create a project to begin your first research run.')).toBeHidden();
  });
}

test('original output navigation opens real report, sources and files, including deep links', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.route(`**/api/v1/artifacts/${REPORT_ID}/content`, (route) => route.fulfill({ contentType: 'text/markdown', body: '# Evidence report\nActual fixture content.' }));
  await page.goto(`/projects/${PROJECT_ID}/library#artifact-${REPORT_ID}`);
  await expect(page.getByRole('heading', { name: 'Evidence report' })).toBeVisible();
  const outputNav = page.getByRole('navigation', { name: 'Project outputs' });
  await outputNav.getByRole('button', { name: /^Sources/ }).click();
  await expect(page.locator(`#citation-${CITATION_ID}`)).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Evidence report' })).toBeHidden();
  await outputNav.getByRole('button', { name: /^Project files/ }).click();
  await expect(page.locator(`#file-${FILE_ID}`)).toBeVisible();
  await page.reload();
  await expect(page.locator(`#file-${FILE_ID}`)).toBeVisible();
  await page.goBack();
  await expect(page.locator(`#citation-${CITATION_ID}`)).toBeVisible();
  await page.goto(`/projects/${PROJECT_ID}/library#file-${FILE_ID}`);
  await expect(page.locator(`#file-${FILE_ID}`)).toBeVisible();
});

test('library expands its real output in the existing safe viewer and restores opener focus', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.route(`**/api/v1/artifacts/${REPORT_ID}/content`, (route) => route.fulfill({ contentType: 'text/markdown', body: '# Expanded evidence' }));
  await page.goto(`/projects/${PROJECT_ID}/library`);
  const opener = page.getByRole('button', { name: 'Expand visual: Example report' });
  await opener.click();
  const viewer = page.getByRole('dialog', { name: 'Example report' });
  await expect(viewer.getByRole('heading', { name: 'Expanded evidence' })).toBeVisible();
  await expect(viewer.getByRole('link', { name: 'Download output' })).toHaveAttribute('href', `/api/v1/artifacts/${REPORT_ID}/content`);
  await page.keyboard.press('Escape');
  await expect(viewer).toBeHidden();
  await expect(opener).toBeFocused();
});

test('history selection changes authoritative details and clears them on project switch', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.route(`**/api/v1/projects/${PROJECT_ID}/runs`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify([runs[0], { ...runs[0], run_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', state: 'canceled', usage_tokens: 37, reserved_tokens: 3623, artifacts: [] }]) }));
  await page.goto('/history');
  await page.getByRole('combobox', { name: 'Choose a project' }).selectOption(PROJECT_ID);
  const detail = page.getByRole('article', { name: 'Run details' });
  await page.getByRole('navigation', { name: 'Research runs' }).getByRole('button', { name: /bbbbbbbb/ }).click();
  await expect(detail.locator('dd').nth(0)).toHaveText('37');
  await expect(detail.locator('dd').nth(1)).toHaveText('3,623');
  await expect(detail.getByRole('link', { name: 'Prepare a new run from this work' })).toHaveAttribute('href', /retry=bbbbbbbb/);
  await page.getByRole('combobox', { name: 'Choose a project' }).selectOption(OTHER_PROJECT_ID);
  await expect(detail).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'No research runs yet' })).toBeVisible();
});

test('original appearance preview cards change real preferences across refresh', async ({ page }) => {
  await page.goto('/settings/appearance');
  await expect(page.locator('.mode-sample')).toHaveCount(3);
  await expect(page.locator('.mode-card').first()).toHaveCSS('display', 'block');
  await expect(page.locator('.original-appearance')).toHaveCSS('display', 'block');
  for (const sample of await page.locator('.mode-sample').all()) {
    const box = await sample.boundingBox();
    expect(box?.height).toBeGreaterThanOrEqual(60);
    expect(box?.width).toBeGreaterThan(60);
  }
  await page.getByRole('radio', { name: 'Dark', exact: true }).check();
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(page.getByRole('radio', { name: 'Dark', exact: true })).toBeChecked();
});
