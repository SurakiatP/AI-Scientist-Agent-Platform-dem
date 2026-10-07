import { expect, test } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const evidence = fileURLToPath(new URL('../.local/sidebar-provider-polish/', import.meta.url));

test('desktop sidebar keeps its route semantics and fits English and Thai labels', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.addInitScript(() => {
    localStorage.setItem('scientist-platform.language', 'en');
    localStorage.setItem('scientist-platform.appearance', 'dark');
  });
  await page.goto('/projects/00000000-0000-4000-8000-000000000001/library');

  const rail = page.locator('.workspace-sidebar');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(rail).toHaveCSS('width', '190px');
  await expect(rail.getByRole('link', { name: 'Sources & outputs', exact: true })).toHaveAttribute('aria-current', 'page');
  await expect(rail.locator('.nav-icon')).toHaveCount(4);
  await expect(rail.locator('.nav-icon[aria-hidden="true"]')).toHaveCount(4);
  await expect(rail.locator('.sidebar-brand-mark svg')).toBeVisible();

  const hoverLink = rail.getByRole('link', { name: 'Projects', exact: true });
  await hoverLink.hover();
  await expect(hoverLink).not.toHaveCSS('box-shadow', 'none');
  await hoverLink.evaluate((node) => (node as HTMLElement).focus());
  await expect(hoverLink).toHaveCSS('outline-style', 'solid');

  await rail.getByRole('button', { name: 'TH', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('lang', 'th');
  const labels = rail.locator('.workspace-link-label');
  for (const label of await labels.all()) {
    const fits = await label.evaluate((node) => node.scrollWidth <= node.clientWidth + 1 && node.getBoundingClientRect().height < 25);
    expect(fits, `Thai label fits in the 190px rail: ${await label.textContent()}`).toBe(true);
  }

  mkdirSync(evidence, { recursive: true });
  await page.screenshot({ path: resolve(evidence, 'sidebar-desktop-th.png') });
});

test('sidebar respects reduced motion and mobile navigation stays usable at 320px', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.addInitScript(() => localStorage.setItem('scientist-platform.language', 'en'));
  await page.goto('/projects');
  const motionDuration = await page.locator('.workspace-sidebar .workspace-link').first().evaluate((node) => getComputedStyle(node).transitionDuration);
  expect(Number.parseFloat(motionDuration)).toBeLessThanOrEqual(0.001);

  await page.setViewportSize({ width: 320, height: 740 });
  await expect(page.locator('.workspace-sidebar')).toBeHidden();
  const menu = page.getByRole('button', { name: 'Open navigation' });
  await menu.click();
  const dialog = page.getByRole('dialog', { name: 'Navigation' });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('link', { name: 'Projects', exact: true })).toHaveAttribute('aria-current', 'page');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= 320 && document.body.scrollWidth <= 320)).toBe(true);
  mkdirSync(evidence, { recursive: true });
  await page.screenshot({ path: resolve(evidence, 'sidebar-mobile-320.png'), fullPage: true });
  await page.keyboard.press('Escape');
  await expect(dialog).not.toBeVisible();
  await expect(menu).toBeFocused();

});
