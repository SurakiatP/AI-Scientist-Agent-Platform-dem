import { expect, test, type Page, type Route } from '@playwright/test';

const PROJECT_ID = '11111111-1111-4111-8111-111111111111';
const SECOND_PROJECT_ID = '22222222-2222-4222-8222-222222222222';
const PROVIDER_ID = '33333333-3333-4333-8333-333333333333';
const PEER_ID = '44444444-4444-4444-8444-444444444444';
const TOKEN_ID = '55555555-5555-4555-8555-555555555555';
const SYNTHETIC_PROVIDER_SECRET = 'synthetic-provider-secret-settings-test';
const SYNTHETIC_PEER_SECRET = 'synthetic-peer-secret-settings-test';
const ONE_TIME_TOKEN = 'synthetic-once-only-scoped-token';

type Write = { method: string; path: string; body: unknown };
type SettingsOverrides = { connectionFailure?: boolean; connectionMutationFailure?: boolean; legacyManualToken?: boolean };

async function mockSettingsApi(page: Page, overrides: SettingsOverrides = {}) {
  const writes: Write[] = [];
  let connections: Array<Record<string, unknown>> = [];
  let delegations: Array<Record<string, unknown>> = [];
  let tokens: Array<Record<string, unknown>> = overrides.legacyManualToken ? [{
    id: TOKEN_ID,
    expires_at: null,
    revoked_at: null,
    created_at: '2026-10-05T12:00:00+00:00',
    grants: [{ project_id: PROJECT_ID, actions: ['project:read'] }],
  }] : [];
  let connectionFailure = overrides.connectionFailure ?? false;
  let connectionMutationFailure = overrides.connectionMutationFailure ?? false;

  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace('/api/v1', '');
    const method = request.method();
    let body: Record<string, unknown> = {};
    try { body = request.postDataJSON() as Record<string, unknown>; } catch { /* GET or empty body */ }
    if (method !== 'GET') writes.push({ method, path, body });
    const json = (data: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });

    if (method === 'GET' && path === '/owner/session') return json({ identity: 'fixture-owner', kind: 'owner', csrf_token: 'fixture-csrf-token' });
    if (method === 'GET' && path === '/capabilities') return json({
      file_types: ['application/pdf', 'text/csv'],
      max_upload_bytes: 25 * 1024 * 1024,
      protocols: { mcp: 'not_configured', a2a: 'not_configured' },
    });
    if (method === 'GET' && path === '/projects') return json([
      { id: PROJECT_ID, name: 'Cell signaling', revision: 1, instructions: '' },
      { id: SECOND_PROJECT_ID, name: 'Protein folding', revision: 1, instructions: '' },
    ]);
    if (method === 'GET' && path === '/connections') {
      if (connectionFailure) return json({ code: 'storage_unavailable', message: 'Secure storage is unavailable.', request_id: 'settings-test' }, 503);
      return json(connections);
    }
    if (method === 'POST' && path === '/connections') {
      if (connectionMutationFailure) return json({ code: 'storage_unavailable', message: 'Secure storage is unavailable.', request_id: 'settings-test' }, 503);
      const connection = { id: body.provider_id, label: body.label, provider: 'https://provider.example', model: body.model, state: 'ready', has_secret: true };
      connections = [connection, ...connections];
      return json(connection, 201);
    }
    if (method === 'DELETE' && /^\/connections\/[0-9a-f-]+$/.test(path)) {
      const id = path.split('/').at(-1);
      connections = connections.filter((connection) => connection.id !== id);
      return route.fulfill({ status: 204 });
    }
    if (method === 'GET' && path === '/peers') return json([{
      peer_id: PEER_ID,
      endpoint: 'https://peer.example',
      endpoint_fingerprint: 'a'.repeat(64),
      configured: true,
      credential_configured: false,
      network_check: 'not_run',
    }]);
    if (method === 'GET' && path === '/peer-delegations') return json(delegations);
    if (method === 'POST' && path === '/peer-delegations') {
      const delegation = {
        id: '66666666-6666-4666-8666-666666666666',
        project_id: body.project_id,
        peer_id: body.peer_id,
        endpoint: 'https://peer.example',
        endpoint_fingerprint: 'a'.repeat(64),
        configured: true,
        actions: ['peer'],
        credential_id: '77777777-7777-4777-8777-777777777777',
        credential_label: body.credential_label,
        credential_configured: true,
        network_check: 'not_run',
        revoked_at: null,
      };
      delegations = [delegation, ...delegations];
      return json(delegation, 201);
    }
    if (method === 'PUT' && /^\/peer-delegations\/[0-9a-f-]+\/credential$/.test(path)) return json({
      id: path.split('/')[2],
      credential_id: '88888888-8888-4888-8888-888888888888',
      credential_label: body.credential_label,
      credential_configured: true,
    });
    if (method === 'DELETE' && /^\/peer-delegations\/[0-9a-f-]+$/.test(path)) {
      delegations = delegations.map((item) => item.id === path.split('/')[2] ? { ...item, revoked_at: new Date().toISOString() } : item);
      return route.fulfill({ status: 204 });
    }
    if (method === 'GET' && path === '/access-tokens') return json(tokens);
    if (method === 'POST' && path === '/access-tokens') {
      tokens = [{
        id: TOKEN_ID,
        expires_at: '2026-11-04T12:00:00+00:00',
        revoked_at: null,
        created_at: '2026-10-05T12:00:00+00:00',
        grants: body.grants,
      }, ...tokens];
      return json({ id: TOKEN_ID, expires_at: '2026-11-04T12:00:00+00:00', token: ONE_TIME_TOKEN }, 201);
    }
    if (method === 'DELETE' && path === `/access-tokens/${TOKEN_ID}`) {
      tokens = tokens.map((item) => item.id === TOKEN_ID ? { ...item, revoked_at: '2026-10-05T12:01:00+00:00' } : item);
      return route.fulfill({ status: 204 });
    }
    return json({ code: 'not_found', message: 'Not found.', request_id: 'settings-test' }, 404);
  });

  return {
    writes,
    setConnectionFailure(value: boolean) { connectionFailure = value; },
    setConnectionMutationFailure(value: boolean) { connectionMutationFailure = value; },
  };
}

test('settings saves masked credentials, keeps peer grants scoped, and never offers a fake check', async ({ page }) => {
  const api = await mockSettingsApi(page);
  await page.addInitScript(() => {
    localStorage.setItem('scientist-platform.language', 'en');
    localStorage.setItem('scientist-platform.appearance', 'light');
  });
  await page.goto('/settings');

  await expect(page.getByRole('heading', { name: 'Configure your research workspace', level: 1 })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Connections' })).toBeVisible();
  await page.getByLabel('Configured provider ID').fill(PROVIDER_ID);
  await page.getByLabel('Connection name').fill('Literature model');
  await page.getByLabel('Model').fill('research-model-v1');
  await page.getByLabel('Provider credential').fill(SYNTHETIC_PROVIDER_SECRET);
  await page.getByRole('button', { name: 'Save connection' }).click();
  await expect(page.getByLabel('Provider credential')).toHaveValue('');
  await expect(page.getByText('Configured; verification not run')).toBeVisible();
  await expect(page.getByText(SYNTHETIC_PROVIDER_SECRET)).toHaveCount(0);

  await expect(page.getByText('Network check: NOT RUN')).toBeVisible();
  await expect(page.getByRole('button', { name: /check peer/i })).toHaveCount(0);
  await expect(page.getByText('A project delegation allows this peer to receive only the approved per-run release. Every release needs an approved purpose and data scope.')).toBeVisible();
  await page.getByLabel('Project for peer delegation').selectOption(PROJECT_ID);
  await page.getByLabel('Configured peer').selectOption(PEER_ID);
  await page.getByLabel('Peer credential name').fill('Review gateway');
  await page.getByLabel('Peer credential', { exact: true }).fill(SYNTHETIC_PEER_SECRET);
  await page.getByRole('button', { name: 'Grant peer access' }).click();
  await expect(page.getByLabel('Peer credential', { exact: true })).toHaveValue('');
  await expect(page.getByText('Peer release only')).toBeVisible();
  await expect(page.getByText(SYNTHETIC_PEER_SECRET)).toHaveCount(0);

  expect(api.writes.find((write) => write.method === 'POST' && write.path === '/connections')?.body).toEqual({
    provider_id: PROVIDER_ID,
    label: 'Literature model',
    model: 'research-model-v1',
    secret: SYNTHETIC_PROVIDER_SECRET,
  });
  expect(api.writes.find((write) => write.method === 'POST' && write.path === '/peer-delegations')?.body).toEqual({
    project_id: PROJECT_ID,
    peer_id: PEER_ID,
    credential_label: 'Review gateway',
    credential: SYNTHETIC_PEER_SECRET,
  });
  await page.getByLabel('New credential name').fill('Rotated gateway key');
  await page.getByLabel('New peer credential').fill('synthetic-rotated-peer-secret');
  await page.getByRole('button', { name: 'Update credential' }).click();
  await expect(page.getByLabel('New peer credential')).toHaveValue('');
  expect(api.writes.find((write) => write.method === 'PUT' && write.path.endsWith('/credential'))?.body).toEqual({
    credential_label: 'Rotated gateway key',
    credential: 'synthetic-rotated-peer-secret',
  });
  await page.getByRole('button', { name: 'Revoke peer access' }).click();
  await expect(page.getByText('Revoked')).toBeVisible();
  expect(api.writes.some((write) => write.method === 'DELETE' && write.path.startsWith('/peer-delegations/'))).toBe(true);
  const persistedSecrets = await page.evaluate(() => [localStorage, sessionStorage].flatMap((storage) =>
    Array.from({ length: storage.length }, (_, index) => `${storage.key(index)}=${storage.getItem(storage.key(index)!)}`),
  ).join(' '));
  expect(persistedSecrets).not.toContain(SYNTHETIC_PROVIDER_SECRET);
  expect(persistedSecrets).not.toContain(SYNTHETIC_PEER_SECRET);
});

test('scoped tokens display once, list only metadata, copy, and revoke', async ({ page }) => {
  const api = await mockSettingsApi(page);
  await page.addInitScript(() => localStorage.setItem('scientist-platform.language', 'en'));
  await page.goto('/settings');

  await page.getByLabel('Token project').selectOption(PROJECT_ID);
  await page.getByLabel('Read project data').check();
  await page.getByLabel('Read research results').check();
  await page.getByRole('button', { name: 'Create scoped token' }).click();
  await expect(page.getByRole('alert').filter({ hasText: ONE_TIME_TOKEN })).toBeVisible();
  await expect(page.getByRole('alert').getByText(/November 4, 2026/)).toBeVisible();
  await expect(page.getByText(ONE_TIME_TOKEN)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Copy token' })).toBeVisible();
  await expect(page.getByText(ONE_TIME_TOKEN, { exact: true })).toHaveCount(1);
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.getByRole('button', { name: 'Copy token' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Token copied.' })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(ONE_TIME_TOKEN);
  await page.getByRole('button', { name: 'TH', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'คัดลอกโทเค็นแล้ว' })).toBeVisible();
  await page.getByRole('button', { name: 'EN', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Token copied.' })).toBeVisible();

  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText: async () => { throw new Error('Clipboard blocked'); } },
  }));
  await page.getByRole('button', { name: 'Copy token' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Copy failed. Select and copy the token manually.' })).toBeVisible();
  await page.getByRole('button', { name: 'TH', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'คัดลอกไม่สำเร็จ โปรดเลือกและคัดลอกโทเค็นด้วยตนเอง' })).toBeVisible();
  await page.getByRole('button', { name: 'EN', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Copy failed. Select and copy the token manually.' })).toBeVisible();

  const create = api.writes.find((write) => write.method === 'POST' && write.path === '/access-tokens');
  expect(create?.body).toEqual({ grants: [{ project_id: PROJECT_ID, actions: ['project:read', 'result:read'] }] });
  expect(await page.evaluate(() => [localStorage, sessionStorage].flatMap((storage) =>
    Array.from({ length: storage.length }, (_, index) => `${storage.key(index)}=${storage.getItem(storage.key(index)!)}`),
  ).join(' '))).not.toContain(ONE_TIME_TOKEN);

  await page.reload();
  await expect(page.getByText(ONE_TIME_TOKEN)).toHaveCount(0);
  await expect(page.getByText(/Expires November 4, 2026/)).toBeVisible();
  await page.getByRole('button', { name: 'Revoke token' }).click();
  await expect(page.getByText('Revoked')).toBeVisible();
  expect(api.writes.some((write) => write.method === 'DELETE' && write.path === `/access-tokens/${TOKEN_ID}`)).toBe(true);
});

test('a failed settings collection is shown as unavailable instead of an empty list', async ({ page }) => {
  const api = await mockSettingsApi(page);
  api.setConnectionFailure(true);
  await page.addInitScript(() => localStorage.setItem('scientist-platform.language', 'en'));
  await page.goto('/settings');
  await expect(page.getByRole('heading', { name: 'Connections' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Connections' }).getByRole('alert')).toBeVisible();
  await expect(page.getByRole('region', { name: 'Connections' }).getByRole('alert')).toContainText('Secure storage is unavailable.');
  await page.getByRole('button', { name: 'TH', exact: true }).click();
  await expect(page.getByRole('region', { name: 'การเชื่อมต่อ' }).getByRole('alert')).toContainText('พื้นที่จัดเก็บที่ปลอดภัยไม่พร้อมใช้งาน');
  await page.getByRole('button', { name: 'EN', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Connections' }).getByRole('alert')).toContainText('Secure storage is unavailable.');
  await expect(page.getByText('No connections configured.')).toHaveCount(0);
});

test('connection mutation errors follow the current language in both directions', async ({ page }) => {
  const api = await mockSettingsApi(page, { connectionMutationFailure: true });
  await page.addInitScript(() => localStorage.setItem('scientist-platform.language', 'en'));
  await page.goto('/settings');
  await page.getByLabel('Configured provider ID').fill(PROVIDER_ID);
  await page.getByLabel('Connection name').fill('Unavailable storage test');
  await page.getByLabel('Model').fill('fixture-model');
  await page.getByLabel('Provider credential').fill(SYNTHETIC_PROVIDER_SECRET);
  await page.getByRole('button', { name: 'Save connection' }).click();
  const error = page.getByRole('region', { name: 'Connections' }).getByRole('alert');
  await expect(error).toContainText('Secure storage is unavailable.');
  await page.getByRole('button', { name: 'TH', exact: true }).click();
  await expect(page.getByRole('region', { name: 'การเชื่อมต่อ' }).getByRole('alert')).toContainText('พื้นที่จัดเก็บที่ปลอดภัยไม่พร้อมใช้งาน');
  await page.getByRole('button', { name: 'EN', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Connections' }).getByRole('alert')).toContainText('Secure storage is unavailable.');
  expect(api.writes.some((write) => write.method === 'POST' && write.path === '/connections')).toBe(true);
});

test('manual legacy tokens without expiry do not render an epoch date', async ({ page }) => {
  await mockSettingsApi(page, { legacyManualToken: true });
  await page.addInitScript(() => localStorage.setItem('scientist-platform.language', 'en'));
  await page.goto('/settings');
  await expect(page.getByText('No expiration date')).toBeVisible();
  await expect(page.getByText(/1970/)).toHaveCount(0);
  await page.getByRole('button', { name: 'TH', exact: true }).click();
  await expect(page.getByText('ไม่มีวันหมดอายุ')).toBeVisible();
  await page.getByRole('button', { name: 'EN', exact: true }).click();
  await expect(page.getByText('No expiration date')).toBeVisible();
});

test('settings text follows the shell language preference', async ({ page }) => {
  await mockSettingsApi(page);
  await page.addInitScript(() => localStorage.setItem('scientist-platform.language', 'en'));
  await page.goto('/settings');
  await page.getByRole('button', { name: 'TH' }).click();
  await expect(page.getByRole('heading', { name: 'ตั้งค่า', level: 1 })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'การเชื่อมต่อ', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'EN' }).click();
  await expect(page.getByRole('heading', { name: 'Configure your research workspace', level: 1 })).toBeVisible();
});
