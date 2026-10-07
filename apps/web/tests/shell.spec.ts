import { expect, test } from '@playwright/test';

test('unsaved appearance follows the system like the original design', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.goto('/settings/appearance');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(page.getByRole('radio', { name: 'System', exact: true })).toBeChecked();
  await page.emulateMedia({ colorScheme: 'light' });
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
});

test('language and appearance persist across refresh', async ({ page }) => {
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

test('language controls remain available across product routes', async ({ page }) => {
  for (const route of ['/', '/projects', '/sources', '/history', '/settings/appearance']) {
    await page.goto(route);
    await expect(page.getByRole('button', { name: 'TH', exact: true }).first()).toBeVisible();
  }
});

test('temperature lab controls change the model, pause, and reset', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('scientist-platform.language', 'en'));
  await page.goto('/');
  const lab = page.getByRole('region', { name: 'Particle diffusion' });
  await expect(lab.getByText(/Illustrative model only/)).toBeVisible();
  const coordinates = () => lab.locator('.lab-particle').evaluateAll((dots) => dots.map((dot) => [dot.getAttribute('cx'), dot.getAttribute('cy')]));
  const original = await coordinates();
  await lab.getByRole('slider', { name: 'Temperature' }).fill('500');
  await expect(lab.locator('#lab-temperature-value')).toHaveText('500 K');
  expect(await coordinates()).not.toEqual(original);
  await expect(lab.getByText('Relative motion')).toBeVisible();
  await expect(lab.getByText('Spread width')).toBeVisible();
  await expect(lab.getByRole('img', { name: 'Histogram of particle distribution' })).toBeVisible();
  await lab.getByRole('button', { name: '500 K' }).click();
  await lab.getByRole('button', { name: 'Play' }).click();
  await expect(lab.locator('.lab-scene')).not.toHaveClass(/is-paused/);
  await lab.getByRole('button', { name: 'Pause' }).click();
  await lab.getByRole('button', { name: 'Reset' }).click();
  await expect(lab.getByRole('slider', { name: 'Temperature' })).toHaveValue('300');
  await expect(lab.locator('.lab-scene')).toHaveClass(/is-paused/);
  expect(await coordinates()).toEqual(original);
});

test('reduced motion keeps the illustrative lab paused', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('scientist-platform.language', 'en'));
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  const lab = page.getByRole('region', { name: 'Particle diffusion' });
  await expect(lab.locator('.lab-scene')).toHaveClass(/is-paused/);
});

test('lab pauses when reduced motion is enabled while it is open', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('scientist-platform.language', 'en'));
  await page.goto('/');
  const lab = page.getByRole('region', { name: 'Particle diffusion' });
  await lab.getByRole('button', { name: 'Play' }).click();
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await expect(lab.getByRole('button', { name: 'Play' })).toBeVisible();
  await expect(lab.locator('.lab-scene')).toHaveClass(/is-paused/);
});

test('dark lab controls meet 4.5:1 text contrast', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('scientist-platform.language', 'en');
    localStorage.setItem('scientist-platform.appearance', 'dark');
  });
  await page.goto('/');
  const lab = page.getByRole('region', { name: 'Particle diffusion' });
  for (const name of ['Play', 'Reset']) {
    const ratio = await lab.getByRole('button', { name }).evaluate((button) => {
      const channels = (color: string) => color.match(/[\d.]+/g)!.slice(0, 3).map(Number);
      const luminance = (color: number[]) => color.map((value) => {
        const channel = value / 255;
        return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
      }).reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
      const style = getComputedStyle(button);
      const values = [luminance(channels(style.color)), luminance(channels(style.backgroundColor))].sort((a, b) => b - a);
      return (values[0] + 0.05) / (values[1] + 0.05);
    });
    expect(ratio, `${name} text contrast`).toBeGreaterThanOrEqual(4.5);
  }
});

test('mobile navigation closes with Escape and restores focus', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto('/projects');
  const opener = page.getByRole('button', { name: 'Open navigation' });
  const openerBox = await opener.boundingBox();
  const languageBox = await page.getByRole('button', { name: 'TH', exact: true }).first().boundingBox();
  expect(openerBox?.width).toBeGreaterThanOrEqual(44);
  expect(openerBox?.height).toBeGreaterThanOrEqual(44);
  expect(languageBox?.width).toBeGreaterThanOrEqual(44);
  expect(languageBox?.height).toBeGreaterThanOrEqual(44);
  await opener.click();
  const dialog = page.getByRole('dialog', { name: /Navigation|เมนูนำทาง/ });
  await expect(dialog).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
});

test('browser Back and Forward restore the route and heading focus', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('scientist-platform.language', 'en'));
  await page.goto('/');
  await page.getByRole('link', { name: 'Start a research project' }).click();
  await expect(page).toHaveURL('/projects');
  await expect(page.getByRole('heading', { name: 'Research starts here' })).toBeFocused();
  await page.goBack();
  await expect(page).toHaveURL('/');
  await expect(page.getByRole('heading', { name: /Every scientific question/ })).toBeFocused();
  await page.goForward();
  await expect(page).toHaveURL('/projects');
  await expect(page.getByRole('heading', { name: 'Research starts here' })).toBeFocused();
});

test('browser Back while the mobile drawer is open closes the modal', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('scientist-platform.language', 'en'));
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto('/');
  await page.getByRole('link', { name: 'Start a research project' }).click();
  await expect(page).toHaveURL('/projects');
  await page.getByRole('button', { name: 'Open navigation' }).click();
  const dialog = page.getByRole('dialog', { name: 'Navigation' });
  await expect(dialog).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL('/');
  await expect(dialog).toBeHidden();
  await expect.poll(() => page.locator('main').evaluate((element) => (element as HTMLElement).inert)).toBe(false);
  await expect(page.getByRole('heading', { name: /Every scientific question/ })).toBeFocused();
});

test('product routes fit supported mobile and desktop widths', async ({ page }) => {
  for (const width of [320, 375, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    for (const route of ['/', '/projects', '/sources', '/history', '/settings/appearance']) {
      await page.goto(route);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `overflow at ${width}px on ${route}`).toBe(true);
    }
  }
});
