import { expect, test } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

test('project-scoped outputs and history select the corresponding original sidebar item', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('scientist-platform.language', 'en'));
  const project = '00000000-0000-4000-8000-000000000001';
  for (const [suffix, name] of [['library', 'Sources & outputs'], ['runs', 'Run history']] as const) {
    await page.goto(`/projects/${project}/${suffix}`);
    const rail = page.locator('.workspace-sidebar');
    await expect(rail.getByRole('link', { name, exact: true })).toHaveAttribute('aria-current', 'page');
    await expect(rail.getByRole('link', { name: 'Projects', exact: true })).not.toHaveAttribute('aria-current', 'page');
  }
});

test('landing follows the original science product shell', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.addInitScript(() => localStorage.setItem('scientist-platform.language', 'en'));
  await page.goto('/');
  await expect(page.getByRole('link', { name: 'Work' })).toHaveAttribute('href', '#research');
  await expect(page.getByRole('link', { name: 'Connections', exact: true })).toHaveAttribute('href', '#connections');
  await expect(page.getByRole('heading', { name: /Every scientific question/ })).toBeVisible();
  await expect(page.locator('.hero-art, .workflow-grid')).toHaveCount(0);
  await expect(page.locator('.notebook-sidebar')).toBeVisible();
  await expect(page.getByRole('region', { name: 'Particle diffusion' })).toBeVisible();
  await expect(page.locator('.science-section')).toHaveCount(4);
  await expect(page.locator('.science-art')).toHaveCount(3);
  await expect(page.locator('.science-band > span')).toHaveCount(5);
  await expect(page.locator('.notebook-session')).toHaveCount(3);
  await expect(page.locator('.notebook-bottom')).toContainText('Question → outputs');
  await expect(page.locator('.connections-illustration')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Get started' }).last()).toHaveAttribute('href', '/projects');
});

test('internal product pages use the dark workspace rail and preserve chat rail ownership', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  for (const route of ['/projects', '/sources', '/history', '/settings']) {
    await page.goto(route);
    const rail = page.locator('.workspace-sidebar');
    await expect(rail).toBeVisible();
    await expect(rail).toHaveCSS('width', '190px');
    await expect(rail.getByRole('link', { name: /Projects|โครงการ/ })).toBeVisible();
    await expect(rail.getByRole('link', { name: /Sources|แหล่ง/ })).toBeVisible();
    await expect(rail.getByRole('link', { name: /History|history|ประวัติ/ })).toBeVisible();
    await expect(rail.getByRole('link', { name: /Settings|ตั้งค่า/ })).toBeVisible();
    await expect(rail.getByText(/Local workspace|พื้นที่ทำงานในเครื่อง/)).toBeVisible();
    await expect(rail.getByRole('button', { name: 'EN', exact: true })).toBeVisible();
  }
  await page.goto('/projects/00000000-0000-4000-8000-000000000001/sessions/00000000-0000-4000-8000-000000000002');
  await expect(page.locator('.chat-frame')).toBeVisible();
  await expect(page.locator('.workspace-sidebar')).toHaveCount(0);
});

test('capture restored shell at desktop, mobile, and internal route', async ({ page }) => {
  const output = fileURLToPath(new URL('../../../.local/original-ui-restoration/shell/', import.meta.url));
  mkdirSync(output, { recursive: true });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.addInitScript(() => localStorage.setItem('scientist-platform.language', 'en'));
  await page.goto('/');
  await page.screenshot({ path: resolve(output, 'landing-en-1280.png'), fullPage: true });
  await page.setViewportSize({ width: 375, height: 812 });
  await page.screenshot({ path: resolve(output, 'landing-en-375.png'), fullPage: true });
  await page.goto('/projects');
  await page.screenshot({ path: resolve(output, 'projects-en-375.png'), fullPage: true });
});
