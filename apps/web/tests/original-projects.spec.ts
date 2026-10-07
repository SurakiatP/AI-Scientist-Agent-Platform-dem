import { expect, test } from '@playwright/test';
import { OTHER_SESSION_ID, PROJECT_ID, SESSION_ID, installProjectFixtureRoutes } from './fixtures/project';

test('empty projects show model setup and first-project onboarding without seeded examples', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.route('**/api/v1/projects', async (route) => {
    if (route.request().method() === 'GET') return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    return route.fallback();
  });
  await page.goto('/projects');

  await expect(page.getByRole('heading', { name: 'Research starts here' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Welcome to your research space' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Configure model' })).toHaveAttribute('href', '/settings');
  await expect(page.getByRole('button', { name: 'Create first project' })).toBeVisible();
  await expect(page.getByText('Diffusion study')).toHaveCount(0);
  await expect(page.getByText('Materials study')).toHaveCount(0);
});

test('populated project cards use API sessions, files, outputs, and real instructions', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.goto('/projects');

  const card = page.locator('article.project-card').filter({ has: page.getByRole('heading', { name: 'Diffusion study' }) });
  await expect(card).toContainText('Compare primary sources.');
  await expect(card).toContainText('2 sessions');
  await expect(card).toContainText('3 files & outputs');
  await expect(page.getByRole('link', { name: 'Open chat →' }).first()).toHaveAttribute('href', `/projects/${PROJECT_ID}/sessions/${SESSION_ID}`);
  await expect(page.getByRole('link', { name: 'View outputs' }).first()).toHaveAttribute('href', `/projects/${PROJECT_ID}/library`);
  await expect(page.getByRole('link', { name: 'Membrane transport' })).toHaveAttribute('href', `/projects/${PROJECT_ID}/sessions/${SESSION_ID}`);
});

test('native project dialog submits only supported project fields and opens the returned project', async ({ page }) => {
  const fixture = await installProjectFixtureRoutes(page);
  await page.goto('/projects');
  await page.getByRole('button', { name: '＋ Create project' }).click();
  const dialog = page.getByRole('dialog', { name: 'Make a new research space' });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel('Project name').fill('  New science space  ');
  await dialog.getByRole('button', { name: 'Create and open project' }).click();
  await expect(page).toHaveURL(new RegExp(`/projects/${PROJECT_ID}$`));
  expect(fixture.writes.find((item) => item.method === 'POST' && item.url === '/projects')?.body).toEqual({ name: 'New science space' });
});

test('a project without sessions creates a real session before opening chat', async ({ page }) => {
  const fixture = await installProjectFixtureRoutes(page);
  await page.route(`**/api/v1/projects/${PROJECT_ID}/sessions`, async (route) => {
    if (route.request().method() === 'GET') return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    return route.fallback();
  });
  await page.goto('/projects');
  await page.getByRole('button', { name: 'Start chat →' }).first().click();
  await expect(page).toHaveURL(new RegExp(`/projects/${PROJECT_ID}/sessions/${OTHER_SESSION_ID}$`));
  expect(fixture.writes.find((item) => item.method === 'POST' && item.url === `/projects/${PROJECT_ID}/sessions`)?.body).toEqual({ title: 'New research session' });
});

test('project detail keeps session creation, project instructions, and evidence controls connected', async ({ page }) => {
  const fixture = await installProjectFixtureRoutes(page);
  await page.goto(`/projects/${PROJECT_ID}`);
  await expect(page.getByRole('heading', { name: 'Diffusion study' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Continue chat →' })).toHaveAttribute('href', `/projects/${PROJECT_ID}/sessions/${SESSION_ID}`);
  await page.getByLabel('New session').fill('A new question');
  await page.getByRole('button', { name: 'Create session' }).click();
  await expect(page).toHaveURL(new RegExp(`/projects/${PROJECT_ID}/sessions/${OTHER_SESSION_ID}$`));
  expect(fixture.writes.find((item) => item.method === 'POST' && item.url === `/projects/${PROJECT_ID}/sessions`)?.body).toEqual({ title: 'A new question' });

  await page.goto(`/projects/${PROJECT_ID}`);
  await page.getByLabel('Instructions shared with sessions in this project').fill('Use peer-reviewed sources.');
  await page.getByRole('button', { name: 'Save instructions' }).click();
  await expect.poll(() => fixture.writes.some((item) => item.method === 'PATCH' && item.url === `/projects/${PROJECT_ID}`)).toBe(true);
  expect(fixture.writes.find((item) => item.method === 'PATCH' && item.url === `/projects/${PROJECT_ID}`)?.body).toEqual({ instructions: 'Use peer-reviewed sources.', revision: 1 });
  await expect(page.getByRole('link', { name: 'Example report' }).first()).toBeVisible();
  await expect(page.getByRole('button', { name: 'Remove finding: Example finding' })).toBeVisible();
});
