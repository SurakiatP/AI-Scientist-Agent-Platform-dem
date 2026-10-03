import { expect, test } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';

test('system appearance follows OS and language persists across refresh', async ({ page }) => {
  await page.addInitScript(() => {
    if (localStorage.getItem('scientist-platform.language') === null) localStorage.setItem('scientist-platform.language', 'en');
    if (localStorage.getItem('scientist-platform.appearance') === null) localStorage.setItem('scientist-platform.appearance', 'system');
  });
  await page.goto('/settings/appearance');
  await page.getByRole('radio', { name: 'System', exact: true }).check();
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.getByRole('button', { name: 'TH', exact: true }).click();
  await expect.poll(() => page.evaluate(() => Object.keys(localStorage).sort())).toEqual([
    'scientist-platform.appearance', 'scientist-platform.language',
  ]);
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('lang', 'th');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(page.getByRole('heading', { name: 'ตั้งค่า', exact: true })).toBeVisible();
});

test('language and appearance are available from every route', async ({ page }) => {
  for (const route of ['/', '/projects', '/sources', '/history', '/settings/appearance']) {
    await page.goto(route);
    await expect(page.getByRole('button', { name: 'TH', exact: true })).toBeVisible();
  }
});

test('lab controls pause, reset, and announce a numerical readout', async ({ page }) => {
  await page.goto('/');
  const lab = page.getByRole('region', { name: 'Diffusion lab' });
  await expect(lab.getByText(/Illustrative model/)).toBeVisible();
  const originalDots = await lab.locator('.particle').evaluateAll((dots) => dots.map((dot) => [dot.getAttribute('cx'), dot.getAttribute('cy')]));
  await lab.getByRole('slider', { name: 'Diffusion' }).fill('0.8');
  await expect(lab.getByRole('status')).toHaveText('0.80');
  const changedDots = await lab.locator('.particle').evaluateAll((dots) => dots.map((dot) => [dot.getAttribute('cx'), dot.getAttribute('cy')]));
  expect(changedDots).not.toEqual(originalDots);
  await lab.getByRole('button', { name: 'Play' }).click();
  await expect(lab.getByRole('button', { name: 'Pause' })).toBeVisible();
  await lab.getByRole('button', { name: 'Reset' }).click();
  await expect(lab.getByRole('slider', { name: 'Diffusion' })).toHaveValue('0.45');
  await expect(lab.locator('.particle-scene')).toHaveClass(/is-paused/);
  const resetDots = await lab.locator('.particle').evaluateAll((dots) => dots.map((dot) => [dot.getAttribute('cx'), dot.getAttribute('cy')]));
  expect(resetDots).toEqual(originalDots);
  await lab.getByRole('button', { name: 'Reset' }).click();
  const secondResetDots = await lab.locator('.particle').evaluateAll((dots) => dots.map((dot) => [dot.getAttribute('cx'), dot.getAttribute('cy')]));
  expect(secondResetDots).toEqual(originalDots);
});

test('lab pauses when reduced motion is enabled while it is open', async ({ page }) => {
  await page.goto('/');
  const lab = page.getByRole('region', { name: 'Diffusion lab' });
  await lab.getByRole('button', { name: 'Play' }).click();
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await expect(lab.getByRole('button', { name: 'Play' })).toBeVisible();
  await expect(lab.locator('.particle-scene')).toHaveClass(/is-paused/);
});

test('dark lab controls meet 4.5:1 text contrast', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('scientist-platform.appearance', 'dark'));
  await page.goto('/');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  const lab = page.getByRole('region', { name: 'Diffusion lab' });
  for (const name of ['Play', 'Reset']) {
    const ratio = await lab.getByRole('button', { name }).evaluate((button) => {
      const channels = (color: string) => color.match(/[\d.]+/g)!.slice(0, 3).map(Number);
      const luminance = (color: number[]) => color.map((value) => {
        const channel = value / 255;
        return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
      }).reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
      const style = getComputedStyle(button);
      const values = [luminance(channels(style.color)), luminance(channels(style.backgroundColor))].sort((a, b) => b - a);
      return {
        ratio: (values[0] + 0.05) / (values[1] + 0.05),
        color: style.color,
        background: style.backgroundColor,
        surface: getComputedStyle(document.documentElement).getPropertyValue('--surface').trim(),
        appearance: style.appearance,
        forcedColors: matchMedia('(forced-colors: active)').matches,
      };
    });
    console.log(`${name}: text ${ratio.color}, background ${ratio.background}, contrast ${ratio.ratio.toFixed(2)}:1`);
    expect(ratio.ratio, `${name} contrast ratio: ${JSON.stringify(ratio)}`).toBeGreaterThanOrEqual(4.5);
  }
});

test('mobile navigation drawer traps interaction, closes on Escape, and restores focus', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto('/');
  const opener = page.getByRole('button', { name: 'Open navigation' });
  const openerBox = await opener.boundingBox();
  const languageBox = await page.getByRole('button', { name: 'TH', exact: true }).boundingBox();
  expect(openerBox?.width).toBeGreaterThanOrEqual(44);
  expect(openerBox?.height).toBeGreaterThanOrEqual(44);
  expect(languageBox?.width).toBeGreaterThanOrEqual(44);
  expect(languageBox?.height).toBeGreaterThanOrEqual(44);
  await opener.click();
  const dialog = page.getByRole('dialog', { name: 'Navigation' });
  await expect(dialog).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
});

test('browser Back and Forward restore the previous route and focus its heading', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('link', { name: 'Get started' }).first().click();
  await expect(page).toHaveURL('/projects');
  await expect(page.getByRole('heading', { name: 'Projects', exact: true })).toBeFocused();
  await page.goBack();
  await expect(page).toHaveURL('/');
  await expect(page.getByRole('heading', { name: /Turn a question/ })).toBeFocused();
  await page.goForward();
  await expect(page).toHaveURL('/projects');
});

test('browser Back while the mobile drawer is open closes the modal', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto('/');
  await page.getByRole('link', { name: 'Get started' }).first().click();
  await expect(page).toHaveURL('/projects');
  await page.getByRole('button', { name: 'Open navigation' }).click();
  const dialog = page.getByRole('dialog', { name: 'Navigation' });
  await expect(dialog).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL('/');
  await expect(dialog).toBeHidden();
  await expect.poll(() => page.locator('main').evaluate((element) => (element as HTMLElement).inert)).toBe(false);
  await expect(page.getByRole('heading', { name: /Turn a question/ })).toBeFocused();
});

test('capture landing review images in light, dark, and mobile layouts', async ({ page }) => {
  const output = resolve(process.cwd(), '../../.local/frontend-review');
  mkdirSync(output, { recursive: true });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.addInitScript(() => {
    if (localStorage.getItem('scientist-platform.language') === null) localStorage.setItem('scientist-platform.language', 'en');
    if (localStorage.getItem('scientist-platform.appearance') === null) localStorage.setItem('scientist-platform.appearance', 'light');
  });
  await page.goto('/');
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: resolve(output, 'landing-light-1280.png'), fullPage: true });
  await page.evaluate(() => localStorage.setItem('scientist-platform.appearance', 'dark'));
  await page.reload();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: resolve(output, 'landing-dark-1280.png'), fullPage: true });
  await page.setViewportSize({ width: 375, height: 812 });
  await page.evaluate(() => localStorage.setItem('scientist-platform.appearance', 'light'));
  await page.reload();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: resolve(output, 'landing-mobile-375.png'), fullPage: true });
});

test('routes have no page-level horizontal overflow at supported widths', async ({ page }) => {
  const routes = ['/', '/projects', '/sources', '/history', '/settings/appearance'];
  for (const width of [320, 375, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    for (const route of routes) {
      await page.goto(route);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
      expect(overflow, `horizontal overflow at ${width}px on ${route}`).toBe(false);
    }
  }
});
