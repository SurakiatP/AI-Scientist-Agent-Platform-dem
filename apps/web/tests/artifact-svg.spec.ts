import { expect, test } from '@playwright/test';
import { installResearchFixtureRoutes, makeRun, plot, SESSION_URL } from './fixtures/research';

const resource = { ...plot, title: 'Scientific chart' };
const contentPath = `/api/v1/artifacts/${resource.artifact_id}/content`;
const svg = `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="64" height="48" viewBox="0 0 64 48">
  <script>parent.__w2SvgScriptRan = true; fetch('http://svg-attack.test/script-executed').catch(() => {});</script>
  <image href="http://svg-attack.test/external-image.png" width="8" height="8"/>
  <rect width="64" height="48" fill="#2864dc"/>
</svg>`;

test('renders and expands SVG artifacts as inert image previews', async ({ page }) => {
  const externalRequests: string[] = [];
  page.on('request', (request) => {
    if (new URL(request.url()).hostname === 'svg-attack.test') externalRequests.push(request.url());
  });
  await page.addInitScript(() => {
    (window as typeof window & { __w2SvgScriptRan?: boolean }).__w2SvgScriptRan = false;
  });
  await page.route('http://svg-attack.test/**', (route) => route.abort());
  await installResearchFixtureRoutes(page, { run: makeRun({ artifacts: [resource] }) });
  await page.route(`**${contentPath}`, (route) => route.fulfill({ status: 200, contentType: 'image/svg+xml', body: svg }));

  await page.goto(SESSION_URL);
  const card = page.getByRole('article', { name: resource.title });
  const image = card.locator('img.artifact-image');
  await expect(image).toBeVisible();
  await expect.poll(() => image.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(64);

  await card.getByRole('button', { name: `Expand visual: ${resource.title}` }).click();
  const dialog = page.getByRole('dialog');
  const expanded = dialog.locator('img.artifact-image');
  await expect(expanded).toBeVisible();
  await expect.poll(() => expanded.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(64);

  await page.waitForTimeout(100);
  expect(await page.evaluate(() => (window as typeof window & { __w2SvgScriptRan?: boolean }).__w2SvgScriptRan)).toBe(false);
  expect(externalRequests).toEqual([]);
});
