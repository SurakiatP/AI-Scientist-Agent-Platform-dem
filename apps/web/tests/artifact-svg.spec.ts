import { expect, test } from '@playwright/test';
import { createHash } from 'node:crypto';
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
  await expect(image).toHaveCSS('background-color', 'rgb(255, 255, 255)');
  await expect(image).toHaveCSS('padding-top', '12px');
  await expect.poll(() => image.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(64);

  await card.getByRole('button', { name: `Expand visual: ${resource.title}` }).click();
  const dialog = page.getByRole('dialog');
  const expanded = dialog.locator('img.artifact-image');
  await expect(expanded).toBeVisible();
  await expect(expanded).toHaveCSS('background-color', 'rgb(255, 255, 255)');
  await expect.poll(() => expanded.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(64);

  await page.waitForTimeout(100);
  expect(await page.evaluate(() => (window as typeof window & { __w2SvgScriptRan?: boolean }).__w2SvgScriptRan)).toBe(false);
  expect(externalRequests).toEqual([]);
});

test('previews octet-stream SVG file artifacts inertly without changing download bytes', async ({ page }) => {
  const digest = createHash('sha256').update(svg).digest('hex');
  const chart = { ...plot, kind: 'file' as const, title: 'chart.svg', sha256: digest, content_type: 'image/svg+xml' };
  const externalRequests: string[] = [];
  page.on('request', (request) => {
    if (new URL(request.url()).hostname === 'svg-attack.test') externalRequests.push(request.url());
  });
  await page.addInitScript(() => {
    (window as typeof window & { __w2SvgScriptRan?: boolean }).__w2SvgScriptRan = false;
  });
  await page.route('http://svg-attack.test/**', (route) => route.abort());
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ artifacts: [chart] }) });
  await page.route(`**${contentPath}`, (route) => route.fulfill({ status: 200, contentType: 'application/octet-stream', body: svg }));

  await page.goto(SESSION_URL);
  const card = page.getByRole('article', { name: 'chart.svg' });
  const image = card.locator('img.artifact-image');
  await expect(image).toBeVisible();
  await expect.poll(() => image.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(64);
  await expect(card.getByText('Preview unavailable for this file type.')).toHaveCount(0);
  for (const selector of ['svg', 'iframe', 'object', 'embed']) await expect(card.locator(selector)).toHaveCount(0);
  const preview = await page.evaluate(async (src) => {
    const response = await fetch(src);
    return { contentType: response.headers.get('content-type'), bytes: Array.from(new Uint8Array(await response.arrayBuffer())) };
  }, await image.getAttribute('src'));
  expect(preview.contentType).toContain('image/svg+xml');
  expect(createHash('sha256').update(Buffer.from(preview.bytes)).digest('hex')).toBe(digest);

  const download = card.getByRole('link', { name: 'Download output' });
  await expect(download).toHaveAttribute('href', contentPath);
  await expect(download).toHaveAttribute('download', 'chart.svg');
  const downloaded = await page.evaluate(async (href) => {
    const response = await fetch(href, { credentials: 'same-origin' });
    return { contentType: response.headers.get('content-type'), bytes: Array.from(new Uint8Array(await response.arrayBuffer())) };
  }, contentPath);
  expect(downloaded.contentType).toContain('application/octet-stream');
  expect(createHash('sha256').update(Buffer.from(downloaded.bytes)).digest('hex')).toBe(digest);

  await page.getByRole('button', { name: 'Expand visual: chart.svg' }).click();
  const expanded = page.getByRole('dialog').locator('img.artifact-image');
  await expect(expanded).toBeVisible();
  await expect.poll(() => expanded.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(64);
  for (const selector of ['svg', 'iframe', 'object', 'embed']) await expect(page.getByRole('dialog').locator(selector)).toHaveCount(0);
  await page.waitForTimeout(100);
  expect(await page.evaluate(() => (window as typeof window & { __w2SvgScriptRan?: boolean }).__w2SvgScriptRan)).toBe(false);
  expect(externalRequests).toEqual([]);

  fixture.setRun(makeRun({ artifacts: [{ ...chart, title: 'chart.html' }] }));
  await page.reload();
  const unknown = page.getByRole('article', { name: 'chart.html' });
  await expect(unknown.getByText('Preview unavailable for this file type.')).toBeVisible();
  await expect(unknown.locator('img, svg, iframe, object, embed')).toHaveCount(0);
});
