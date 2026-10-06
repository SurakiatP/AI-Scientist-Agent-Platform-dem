import { expect, test } from '@playwright/test';

test('bootstrap fragment is removed before the request and its CSRF token authorizes mutations', async ({ page }) => {
  let release!: () => void;
  const blocked = new Promise<void>((resolve) => { release = resolve; });
  const events: string[] = [];
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/v1/bootstrap') {
      events.push(`bootstrap:${new URL(page.url()).hash}`);
      await blocked;
      await route.fulfill({ json: { csrf_token: 'csrf-from-bootstrap' } });
      return;
    }
    if (path === '/api/v1/owner/session') events.push('owner-session');
    if (path === '/api/v1/test-mutation') {
      events.push(`mutation:${request.headers()['x-csrf-token'] ?? ''}`);
      await route.fulfill({ json: { ok: true } });
      return;
    }
    await route.fulfill({ status: 404, json: { code: 'not_found' } });
  });

  await page.goto('/#bootstrap=one-time-secret');
  await expect(page).toHaveURL('/');
  expect(events).toEqual(['bootstrap:']);
  await expect(page.locator('#root')).toBeEmpty();
  release();
  await expect(page.getByRole('heading', { name: /Turn a question/ })).toBeVisible();
  await page.evaluate(async () => {
    const api = await import('/src/api.ts');
    await api.request('/api/v1/test-mutation', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  });
  expect(events).toEqual(['bootstrap:', 'mutation:csrf-from-bootstrap']);
});

test('failed bootstrap shows a safe message without mounting routes or fetching an owner session', async ({ page }) => {
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/bootstrap') {
      await route.fulfill({ status: 401, json: { code: 'forbidden', message: 'one-time-secret must not appear' } });
    } else {
      await route.fulfill({ status: 401, json: { code: 'unexpected' } });
    }
  });
  await page.goto('/#bootstrap=one-time-secret');
  await expect(page.getByRole('alert')).toContainText('Startup could not be completed.');
  await expect(page.locator('body')).not.toContainText('one-time-secret');
  await expect(page.locator('#root')).not.toContainText(/Turn a question/);
  await expect(page).toHaveURL('/');
});

test('normal startup keeps session lookup lazy until the first mutation', async ({ page }) => {
  const events: string[] = [];
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    events.push(`${request.method()}:${path}`);
    if (path === '/api/v1/owner/session') await route.fulfill({ json: { csrf_token: 'csrf-from-session' } });
    else await route.fulfill({ json: { ok: true } });
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: /Turn a question/ })).toBeVisible();
  expect(events).toEqual([]);
  await page.evaluate(async () => {
    const api = await import('/src/api.ts');
    await api.request('/api/v1/test-mutation', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  });
  expect(events).toEqual(['GET:/api/v1/owner/session', 'POST:/api/v1/test-mutation']);
});
