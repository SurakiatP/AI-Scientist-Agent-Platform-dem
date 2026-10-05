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

test('fenced code copies', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  const report = await open(page, '```python\nprint(1)\n```\n');
  await report.getByRole('button', { name: 'Copy code' }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('print(1)');
});

test('GFM pipe tables render a real focusable table in a scroll box', async ({ page }) => {
  const report = await open(page, '| A | B |\n|---|---|\n| 1 | 2 |\n');
  await expect(report.locator('table')).toHaveCount(1);
  await expect(report.locator('th')).toHaveText(['A', 'B']);
  await expect(report.locator('td')).toHaveText(['1', '2']);
  const box = report.locator('.table-scroll');
  await expect(box).toHaveAttribute('tabindex', '0');
  await expect(box).toHaveAttribute('role', 'region');
  await expect(box).toHaveAttribute('aria-label', 'Table');
});

test('table region label follows Thai language', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('scientist-platform.language', 'th'));
  const report = await open(page, '| A |\n|---|\n| 1 |\n');
  await expect(report.getByRole('region', { name: 'ตาราง' })).toBeVisible();
});

test('wide tables scroll inside the box, not the page', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const cols = 30;
  const row = (c: string) => `| ${Array.from({ length: cols }, (_, i) => `${c}${i}longcellvalue`).join(' | ')} |`;
  const report = await open(page, `${row('h')}\n|${'---|'.repeat(cols)}\n${row('d')}\n`);
  const box = report.locator('.table-scroll');
  const m = await box.evaluate((e) => ({ sw: e.scrollWidth, cw: e.clientWidth, ox: getComputedStyle(e).overflowX }));
  expect(m.sw).toBeGreaterThan(m.cw);
  expect(m.ox).toBe('auto');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('table cells get borders and padding', async ({ page }) => {
  const report = await open(page, '| A |\n|---|\n| 1 |\n');
  const s = await report.locator('td').evaluate((e) => { const c = getComputedStyle(e); return { b: c.borderTopWidth, p: c.paddingTop }; });
  expect(s.b).not.toBe('0px');
  expect(s.p).not.toBe('0px');
});

test('GFM autolink literals become safe links', async ({ page }) => {
  const report = await open(page, 'See www.example.org and https://example.org/a and a@b.co now.\n');
  const links = report.locator('a');
  await expect(links).toHaveCount(3);
  const hrefs = await links.evaluateAll((as) => as.map((a) => a.getAttribute('href')));
  expect(hrefs).toEqual(['http://www.example.org', 'https://example.org/a', 'mailto:a@b.co']);
  for (const rel of await links.evaluateAll((as) => as.map((a) => a.getAttribute('rel')))) expect(rel).toBe('noopener noreferrer');
});

test('GFM does not reintroduce raw HTML or unsafe links in tables', async ({ page }) => {
  const report = await open(page, '| A | B |\n|---|---|\n| <script>window.__pwn = 1</script> | <img src=x onerror="window.__pwn = 2"> |\n| <iframe src="https://example.org"></iframe> | [js](javascript:alert(1)) [d](data:text/html,x) |\n\n<title>x</title> <xmp>y</xmp>\n');
  await expect(report.locator('script, img, iframe, title, xmp')).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __pwn?: number }).__pwn)).toBeUndefined();
  await expect(report.locator('td').first()).toContainText('<script>');
  const hrefs = await report.locator('a').evaluateAll((as) => as.map((a) => a.getAttribute('href') ?? ''));
  expect(hrefs.filter((h) => /^(javascript|data):/i.test(h))).toEqual([]);
});

test('GFM footnotes link in-page with ids, hidden localized label and unique prefixes', async ({ page }) => {
  const report = await open(page, 'Claim[^1].\n\n[^1]: Source note.\n');
  const ref = report.locator('sup a');
  await expect(ref).toHaveAttribute('id', /fnref-1$/);
  const href = await ref.getAttribute('href');
  expect(href).toMatch(/^#.+fn-1$/);
  await expect(report.locator(href!.replace(/^#/, '[id="') + '"]')).toContainText('Source note.');
  for (const a of await report.locator('a').all()) expect(await a.getAttribute('target')).toBeNull();
  await expect(report.locator('[data-footnote-backref]')).toHaveAttribute('aria-label', 'Back to reference 1');
  await expect(report.locator('h2.visually-hidden')).toHaveText('Footnotes');
  expect(href).not.toBe('#user-content-fn-1');
});
