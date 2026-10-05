import { expect, test, type Page } from '@playwright/test';
import { installResearchFixtureRoutes, makePlan, makeRun, NEW_RUN_ID, SESSION_URL } from './fixtures/research';

const peerId = 'abababab-abab-4bab-8bab-abababababab';
const fingerprint = 'a'.repeat(64);
const release = {
  release_id: 'babababa-baba-4aba-8aba-babababababa', peer_id: peerId,
  endpoint_fingerprint: fingerprint, purpose: 'Compare a synthetic research summary',
  input_snapshot_digest: 'e'.repeat(64), data_refs: [{ kind: 'file' as const,
    record_id: 'cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd', version_digest: 'c'.repeat(64) }],
  approved_parameters: { message: { messageId: 'synthetic-message', parts: [{ text: '<script>synthetic only</script>' }] } },
  parameters_sha256: 'd'.repeat(64), message_id: 'synthetic-message', method: 'SendMessage' as const,
  allow_get_task: true, request_bytes_limit: 4096, timeout_ms: 5000,
  reserved_tokens: 100, reconciliation_limit: 2,
};

async function setup(page: Page, options: { fingerprint?: string; malformed?: boolean; hold?: boolean; purpose?: string } = {}) {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [] }) });
  const plan = makePlan();
  plan.plan.peer_releases = [{ ...release, purpose: options.purpose ?? release.purpose, ...(options.malformed ? { approved_parameters: null as never } : {}) }];
  await page.route('**/api/v1/runs/*/plan', (route) => route.fulfill({ json: plan }));
  let unblock: (() => void) | undefined;
  const gate = options.hold ? new Promise<void>((resolve) => { unblock = resolve; }) : Promise.resolve();
  await page.route('**/api/v1/peers', async (route) => {
    await gate;
    await route.fulfill({ json: [{ peer_id: peerId, endpoint: 'https://peer.example.test', endpoint_fingerprint: options.fingerprint ?? fingerprint, configured: true }] });
  });
  await page.goto(SESSION_URL);
  return { fixture, unblock };
}

test('peer release presents recipient, exact payload, immutable data and bounded reads before approval', async ({ page }) => {
  await setup(page);
  const review = page.getByRole('region', { name: 'Peer data release review' });
  await expect(review.getByText('https://peer.example.test', { exact: true })).toBeVisible();
  await expect(review.getByText(release.purpose)).toBeVisible();
  await expect(review.getByText(release.data_refs[0].version_digest, { exact: true })).toBeVisible();
  await expect(review.getByText('4096 bytes · 5000 ms · 100 tokens · 2 status reads', { exact: true })).toBeVisible();
  await review.getByText('Exact approved payload', { exact: true }).click();
  await expect(review.locator('pre')).toContainText('<script>synthetic only</script>');
  await expect(review.locator('script')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toBeEnabled();
  const expand = review.getByRole('button', { name: 'Expand payload' });
  await expand.click();
  await expect(page.getByRole('dialog').locator('pre')).toContainText('<script>synthetic only</script>');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(expand).toBeFocused();
  await page.getByRole('button', { name: 'TH', exact: true }).click();
  await expect(page.getByRole('region', { name: 'ตรวจทานการส่งข้อมูลให้ผู้ร่วมวิจัย' })).toBeVisible();
  await expect(page.getByText('ข้อมูลที่อนุมัติให้ส่งอย่างครบถ้วน', { exact: true })).toBeVisible();
});

test('approval waits for trusted peer metadata', async ({ page }) => {
  const { unblock } = await setup(page, { hold: true });
  await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toBeDisabled();
  unblock!();
  await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toBeEnabled();
});

test('changed recipient fingerprint blocks approval', async ({ page }) => {
  await setup(page, { fingerprint: 'b'.repeat(64) });
  await expect(page.getByRole('alert').filter({ hasText: 'Peer details could not be verified' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toBeDisabled();
});

test('unreviewable payload blocks approval rather than hiding the release', async ({ page }) => {
  await setup(page, { malformed: true });
  await expect(page.getByRole('alert').filter({ hasText: 'Peer details could not be verified' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toBeDisabled();
});


test('peer metadata failure blocks release approval and reports the problem', async ({ page }) => {
  await setup(page, { hold: false });
  await page.route('**/api/v1/peers', (route) => route.fulfill({ status: 503, json: { code: 'unavailable', message: 'Unavailable', request_id: 'synthetic' } }));
  await page.reload();
  await expect(page.getByRole('alert').filter({ hasText: 'Peer details could not be verified' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toBeDisabled();
});


test('long release purpose and expanded payload stay readable on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await setup(page, { purpose: 'x'.repeat(200) });
  await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toBeEnabled();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await page.getByRole('button', { name: 'Expand payload' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  expect(await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
});
