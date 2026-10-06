import { expect, test, type Page } from '@playwright/test';
import { installResearchFixtureRoutes, makeRun, plot, SESSION_URL } from './fixtures/research';

const envelope = {
  instruction_fingerprint: 'b'.repeat(64),
  measurement: { cpu_count: 2, cpu_quota_cores: 1.5, gpu_validation: false, memory_current_bytes: 268435456, memory_limit_bytes: 536870912 },
  profile_id: 'prof.worker-base@py3.14.7', recipe_id: 'get-available-resources', schema_version: 1,
};
const resource = { ...plot, title: 'Worker resources' };
const contentPath = `/api/v1/artifacts/${resource.artifact_id}/content`;

async function showJson(page: Page, body: string) {
  await installResearchFixtureRoutes(page, { run: makeRun({ artifacts: [resource] }) });
  await page.route(`**${contentPath}`, (route) => route.fulfill({ status: 200, contentType: 'application/json', body }));
  await page.goto(SESSION_URL);
  return page.getByRole('article', { name: resource.title });
}

test('resource measurements hide runtime identities in EN/TH and expanded view while retaining provenance and original download', async ({ page }) => {
  // Python's canonical float spelling is valid even when JS would serialize it as 2.
  const body = JSON.stringify(envelope).replace('"cpu_quota_cores":1.5', '"cpu_quota_cores":2.0');
  const card = await showJson(page, body);
  await expect(card.getByRole('row', { name: 'Available CPU cores 2', exact: true })).toBeVisible();
  await expect(card.getByRole('row', { name: 'CPU quota 2 cores', exact: true })).toBeVisible();
  await expect(card.getByRole('row', { name: 'Memory limit 512 MiB (536,870,912 bytes)', exact: true })).toBeVisible();
  await expect(card.getByRole('row', { name: 'Memory in use 256 MiB (268,435,456 bytes)', exact: true })).toBeVisible();
  await expect(card.getByRole('row', { name: 'GPU availability Not assessed', exact: true })).toBeVisible();
  await expect(card.locator('.artifact-provenance')).toContainText('sha256 aaaaaaaaaaaa');
  for (const internal of ['recipe_id', 'profile_id', 'instruction_fingerprint', envelope.profile_id, envelope.recipe_id, envelope.instruction_fingerprint]) {
    await expect(card).not.toContainText(internal);
  }
  await expect(card.getByRole('link', { name: 'Download output' })).toHaveAttribute('href', contentPath);
  await expect(card.getByRole('link', { name: 'Download output' })).toHaveAttribute('download', resource.title);
  // Browser downloads bypass page.route in this harness. Check the original target response.
  expect(await card.getByRole('link', { name: 'Download output' }).evaluate(async (link: HTMLAnchorElement) => (await fetch(link.href)).text())).toBe(body);
  await card.getByRole('button', { name: 'Expand visual: Worker resources' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('row', { name: 'Memory limit 512 MiB (536,870,912 bytes)', exact: true })).toBeVisible();
  await expect(dialog).not.toContainText('instruction_fingerprint');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'TH', exact: true }).click();
  await expect(card.getByRole('row', { name: 'แกนประมวลผลที่ใช้ได้ 2', exact: true })).toBeVisible();
  await expect(card.getByRole('row', { name: 'หน่วยความจำสูงสุด 512 MiB (536,870,912 ไบต์)', exact: true })).toBeVisible();
  await expect(card.getByRole('row', { name: 'หน่วยความจำที่ใช้อยู่ 256 MiB (268,435,456 ไบต์)', exact: true })).toBeVisible();
  await card.getByRole('button', { name: 'ขยายภาพ: Worker resources' }).click();
  await expect(dialog.getByRole('row', { name: 'โควตาการประมวลผล 2 แกน', exact: true })).toBeVisible();
  await expect(dialog.getByRole('row', { name: 'การใช้ GPU ยังไม่ได้ประเมิน', exact: true })).toBeVisible();
  await expect(dialog).not.toContainText(envelope.profile_id);
});

test('resource measurements preserve fractional CPU quota and report missing limits without fabricating zero', async ({ page }) => {
  const body = JSON.stringify({ ...envelope, measurement: { ...envelope.measurement, cpu_quota_cores: 0.125, memory_current_bytes: null, memory_limit_bytes: null } });
  const card = await showJson(page, body);
  await expect(card.getByRole('row', { name: 'CPU quota 0.125 cores', exact: true })).toBeVisible();
  await expect(card.getByRole('row', { name: 'Memory limit No finite limit reported', exact: true })).toBeVisible();
  await expect(card.getByRole('row', { name: 'Memory in use Not reported', exact: true })).toBeVisible();
});

for (const [name, body] of [
  ['unrelated scientific JSON', JSON.stringify({ profile_id: 'another-profile', recipe_id: 'another-recipe', instruction_fingerprint: 'research-value', measurement: { result: 7.25 } })],
  ['extra envelope field', JSON.stringify({ ...envelope, conclusion: 'retain this text' })],
  ['unknown profile', JSON.stringify({ ...envelope, profile_id: 'prof.future' })],
  ['invalid fingerprint', JSON.stringify({ ...envelope, instruction_fingerprint: 'B'.repeat(64) })],
  ['impossible memory usage', JSON.stringify({ ...envelope, measurement: { ...envelope.measurement, memory_current_bytes: 536870913 } })],
  ['fabricated GPU assessment', JSON.stringify({ ...envelope, measurement: { ...envelope.measurement, gpu_validation: true } })],
  ['duplicate keys', JSON.stringify(envelope).replace('"cpu_count":2', '"cpu_count":99,"cpu_count":2')],
  ['noncanonical bytes', JSON.stringify(envelope, null, 2)],
  ['trailing newline', JSON.stringify(envelope) + '\n'],
  ['noncanonical float spelling', JSON.stringify(envelope).replace('"cpu_quota_cores":1.5', '"cpu_quota_cores":1.50')],
] as const) {
  test(`${name} keeps the full JSON preview`, async ({ page }) => {
    const card = await showJson(page, body);
    await expect(card.locator('pre')).toHaveText(JSON.stringify(JSON.parse(body), null, 2));
    await expect(card.getByRole('table')).toHaveCount(0);
  });
}
