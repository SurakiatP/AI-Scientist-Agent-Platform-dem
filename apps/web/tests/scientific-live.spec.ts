import { createHash } from 'node:crypto';
import { chmod, mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { expect, test } from '@playwright/test';

const bootstrapFile = process.env.SCIENTIFIC_W1_OWNER_BOOTSTRAP_FILE;
const proofFile = process.env.SCIENTIFIC_W1_PROOF;
const evidenceFile = process.env.SCIENTIFIC_W1_BROWSER_EVIDENCE;
const enabled = Boolean(bootstrapFile && proofFile && evidenceFile && process.env.SCIENTIFIC_W1_API_ORIGIN && process.env.SCIENTIFIC_W1_PYTHON);

test.skip(!enabled, 'NOT RUN: dedicated W1 owner bootstrap, host, and evidence paths are required');

test('real owner browser completes and presents the approved resource measurement', async ({ page, baseURL }) => {
  test.setTimeout(600_000);
  const proof = JSON.parse(await readFile(proofFile!, 'utf8')) as {
    project_id: string; session_id: string; run_id: string;
  };
  const bootstrapUrl = (await readFile(bootstrapFile!, 'utf8')).trim();
  if (!baseURL) throw new Error('live owner bootstrap is unavailable');
  const origin = new URL(baseURL).origin;
  const bootstrap = new URL(bootstrapUrl);
  if (bootstrap.origin !== origin || !bootstrap.hash.startsWith('#bootstrap=')) {
    throw new Error('fresh owner bootstrap does not match the configured same-origin host');
  }

  // Navigate to the unmodified, one-use URL emitted by the host process.
  try {
    await page.goto(bootstrapUrl);
    await expect.poll(() => page.evaluate(() => location.hash.length === 0), { timeout: 20_000 }).toBe(true);
  } catch {
    await page.goto(origin).catch(() => undefined);
    throw new Error('owner bootstrap did not complete and clear its fragment');
  }
  await page.getByRole('button', { name: 'EN' }).click();
  await expect(page.getByRole('link', { name: 'Projects' })).toBeVisible();

  const setup = `/projects/${encodeURIComponent(proof.project_id)}/research-setup?${new URLSearchParams({
    session: proof.session_id, run: proof.run_id,
  })}`;
  await page.goto(setup);
  await expect(page.getByRole('heading', { name: 'Research setup' })).toBeVisible();
  await page.getByRole('button', { name: 'Refresh setup' }).click();
  await expect(page.getByText('Environment ready')).toBeVisible({ timeout: 20_000 });
  await page.getByRole('link', { name: 'Return to plan' }).click({ timeout: 30_000 });
  await expect(page).toHaveURL(new RegExp(`/sessions/${proof.session_id}\\?run=${proof.run_id}`));

  await page.getByLabel('Research question').fill('Measure the approved synthetic worker resource limits in the browser acceptance.');
  await page.getByLabel('Research workflow').selectOption('resources');
  await page.getByRole('button', { name: 'Review plan' }).click();
  const plan = page.getByRole('region', { name: 'Plan review' });
  await expect(plan).toBeVisible({ timeout: 30_000 });
  const currentUrl = new URL(page.url());
  const runId = currentUrl.searchParams.get('run');
  if (!runId || runId === proof.run_id) throw new Error('browser did not create a fresh run');
  const waitForResourceReadiness = async (budgets?: { token_limit: number; elapsed_limit_ms: number }) => {
    await expect.poll(async () => page.evaluate(async ({ id, expectedBudgets }) => {
      const [planResponse, readinessResponse] = await Promise.all([
        fetch(`/api/v1/runs/${encodeURIComponent(id)}/plan`, { credentials: 'same-origin' }),
        fetch(`/api/v1/runs/${encodeURIComponent(id)}/readiness`, { credentials: 'same-origin' }),
      ]);
      if (!planResponse.ok || !readinessResponse.ok) return false;
      const currentPlan = await planResponse.json() as {
        run_id: string; revision: number; plan_digest: string;
        plan: { token_limit: number; elapsed_limit_ms: number; scientific?: { capability_ids: string[] } | null };
      };
      const readiness = await readinessResponse.json() as {
        run_id: string; revision: number; plan_digest: string; state: string; binding_sha256: string | null;
        requirements: Array<{ state: string }>;
      };
      return currentPlan.run_id === id &&
        currentPlan.plan.scientific?.capability_ids.includes('get-available-resources') === true &&
        (expectedBudgets === null || (currentPlan.plan.token_limit === expectedBudgets.token_limit &&
          currentPlan.plan.elapsed_limit_ms === expectedBudgets.elapsed_limit_ms)) &&
        readiness.run_id === id && readiness.state === 'ready' && readiness.binding_sha256 !== null &&
        Array.isArray(readiness.requirements) && readiness.requirements.every((item) => item.state === 'ready') &&
        readiness.revision === currentPlan.revision && readiness.plan_digest === currentPlan.plan_digest;
    }, { id: runId, expectedBudgets: budgets ?? null }), { timeout: 30_000 }).toBe(true);
  };
  await plan.getByRole('button', { name: 'Prepare plan', exact: true }).click({ timeout: 30_000 });
  await waitForResourceReadiness();
  await expect(plan.getByText('Current plan requirements are ready.')).toBeVisible({ timeout: 30_000 });
  await plan.getByRole('link', { name: 'Research Setup' }).click();
  await expect(page.getByText('Environment ready')).toBeVisible();
  await page.getByRole('button', { name: 'Refresh setup' }).click();
  await page.getByRole('link', { name: 'Return to plan' }).click({ timeout: 30_000 });
  await expect(page.getByRole('region', { name: 'Plan review' })).toBeVisible();

  await page.getByLabel('Token limit').fill('20000');
  await page.getByLabel('Time limit (seconds)').fill('600');
  await page.getByRole('button', { name: 'Save edits', exact: true }).click();
  await expect(plan.getByLabel('Token limit')).toHaveValue('20000');
  await expect(plan.getByLabel('Time limit (seconds)')).toHaveValue('600');
  await expect(plan.getByText('20000 tokens · 600 s')).toBeVisible();
  await waitForResourceReadiness({ token_limit: 20_000, elapsed_limit_ms: 600_000 });
  await expect(plan.getByText('Current plan requirements are ready.')).toBeVisible();
  await expect(plan.getByRole('button', { name: 'Approve plan', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Approve plan' }).click();

  await expect.poll(async () => page.evaluate(async (id: string) => {
    const response = await fetch(`/api/v1/runs/${encodeURIComponent(id)}`, { credentials: 'same-origin' });
    if (!response.ok) return false;
    const current = await response.json() as { state: string; artifacts: Array<{ title: string; partial: boolean }> };
    return current.state === 'completed' && current.artifacts.some((item) =>
      item.title === 'Resource measurements' && !item.partial);
  }, runId), { timeout: 300_000 }).toBe(true);

  const outputs = page.getByRole('region', { name: 'Outputs' });
  const card = outputs.getByRole('article', { name: 'Resource measurements' });
  await expect(card).toBeVisible({ timeout: 300_000 });
  await expect(card.getByText('Partial output')).toHaveCount(0);
  await card.getByRole('button', { name: 'Expand visual: Resource measurements' }).click();
  await expect(page.getByRole('region', { name: 'Resource measurement' })).toBeVisible();
  await expect(page.getByRole('row', { name: /Available CPU cores/ })).toBeVisible();
  await page.getByRole('button', { name: 'TH' }).click();
  await expect(page.getByRole('region', { name: 'ข้อมูลทรัพยากร' })).toBeVisible();
  await expect(page.getByRole('row', { name: /แกนประมวลผลที่ใช้ได้/ })).toBeVisible();
  await page.getByRole('button', { name: 'EN' }).click();
  await expect(page.getByRole('region', { name: 'Resource measurement' })).toBeVisible();

  const run = await page.evaluate(async (id: string) => {
    const response = await fetch(`/api/v1/runs/${encodeURIComponent(id)}`, { credentials: 'same-origin' });
    if (!response.ok) throw new Error('run read failed');
    return await response.json() as { artifacts: Array<{ title: string; partial: boolean; sha256: string; size: number; artifact_id: string }> };
  }, runId);
  const artifact = run.artifacts.find((item: { title: string; partial: boolean }) => item.title === 'Resource measurements' && !item.partial);
  if (!artifact) throw new Error('completed run has no final resource artifact');
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('link', { name: 'Download output' }).click();
  const download = await downloadPromise;
  const stream = await download.createReadStream();
  if (!stream) throw new Error('artifact download stream is unavailable');
  const hash = createHash('sha256');
  let size = 0;
  for await (const chunk of stream) {
    size += chunk.length;
    hash.update(chunk);
  }
  const sha256 = hash.digest('hex');
  expect(sha256).toBe(artifact.sha256);
  expect(size).toBe(artifact.size);

  await mkdir(path.dirname(evidenceFile!), { recursive: true, mode: 0o700 });
  await writeFile(evidenceFile!, JSON.stringify({ status: 'PASS', project_id: proof.project_id,
    session_id: proof.session_id, run_id: runId, artifact_id: artifact.artifact_id,
    artifact_sha256: sha256, artifact_size: size, thai_and_english_measurements: true,
    setup_refresh: true, owner_bootstrap_fragment_cleared: true }) + '\n', { mode: 0o600 });
  await chmod(evidenceFile!, 0o600);
  const checker = path.resolve(process.cwd(), '../../backend/tests/live/scientific_host_http_acceptance.py');
  const readback = spawnSync(process.env.SCIENTIFIC_W1_PYTHON!, [checker, '--verify-browser-run', runId], {
    encoding: 'utf8', timeout: 60_000, env: process.env,
  });
  if (readback.error || readback.status !== 0) throw new Error('browser artifact DB/S3 readback failed');
});
