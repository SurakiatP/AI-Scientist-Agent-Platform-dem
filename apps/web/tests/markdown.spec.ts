import { expect, test } from '@playwright/test';
import { SESSION_URL, installResearchFixtureRoutes } from './fixtures/research';

const open = async (page: import('@playwright/test').Page, reportMarkdown: string) => {
  await installResearchFixtureRoutes(page, { reportMarkdown });
  await page.goto(SESSION_URL);
  await expect(page.locator('.report-text')).toBeVisible();
  return page.locator('.report-text');
};

test('inline and block math render KaTeX nodes', async ({ page }) => {
  const report = await open(page, 'Inline $e^{i\\pi}+1=0$ here.\n\n$$\n\\int_0^1 x\\,dx\n$$\n');
  await expect(report.locator('.katex')).toHaveCount(2);
  await expect(report.locator('.katex-display')).toHaveCount(1);
  await expect(report.locator('annotation')).toHaveCount(2);
});

test('raw HTML appears as text, never as elements', async ({ page }) => {
  const report = await open(page, 'Before <script>window.__pwn = 1</script> and <img src=x onerror="window.__pwn = 2"> after\n');
  await expect(report.locator('script, img')).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __pwn?: number }).__pwn)).toBeUndefined();
  await expect(report).toContainText('Before');
  await expect(report).toContainText('after');
});

test('javascript: and data: links are neutralised, safe links keep rel', async ({ page }) => {
  const report = await open(page, '[js](javascript:alert(1)) [data](data:text/html,x) [ok](https://example.org/a) [rel](/relative) [mail](mailto:a@b.co) [ftp](ftp://x.org)\n');
  const hrefs = await report.locator('a').evaluateAll((as) => as.map((a) => a.getAttribute('href') ?? ''));
  expect(hrefs.filter((h) => /^(javascript|data|ftp):/i.test(h))).toEqual([]);
  const ok = report.getByRole('link', { name: 'ok' });
  await expect(ok).toHaveAttribute('href', 'https://example.org/a');
  await expect(ok).toHaveAttribute('rel', 'noopener noreferrer');
  await expect(report.getByRole('link', { name: 'rel' })).toHaveAttribute('href', '/relative');
  await expect(report.getByRole('link', { name: 'mail' })).toHaveAttribute('href', 'mailto:a@b.co');
  await expect(report).toContainText('js');
});

test('KaTeX \\href is inert with trust disabled', async ({ page }) => {
  // A safe URL: only KaTeX trust (not the link filter) keeps this from becoming a live link.
  const report = await open(page, '$\\href{https://example.org}{click}$\n');
  await expect(report.locator('a')).toHaveCount(0);
  await expect(report.locator('.katex')).toContainText('\\href');
});

test('remote images are not loaded', async ({ page }) => {
  const report = await open(page, '![alt text](https://example.org/x.png)\n');
  await expect(report.locator('img')).toHaveCount(0);
  await expect(report).toContainText('alt text');
});

test('pipe tables stay inside a scroll box (no GFM plugin is installed) and fenced code copies', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  const report = await open(page, '| A | B |\n|---|---|\n| 1 | 2 |\n\n```python\nprint(1)\n```\n');
  await expect(report.locator('.table-scroll')).toContainText('| A | B |');
  await report.getByRole('button', { name: 'Copy code' }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('print(1)');
});
