import { createHash } from 'node:crypto';
import { chmod, mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { expect, test } from '@playwright/test';

const bootstrapFile = process.env.SCIENTIFIC_W1_OWNER_BOOTSTRAP_FILE;
const proofFile = process.env.SCIENTIFIC_W1_PROOF;
const evidenceFile = process.env.SCIENTIFIC_W1_BROWSER_EVIDENCE;
const enabled = Boolean(bootstrapFile && proofFile && evidenceFile && process.env.SCIENTIFIC_W1_API_ORIGIN && process.env.SCIENTIFIC_W1_PYTHON);

test('real owner browser completes and presents the approved resource measurement', async ({ page, baseURL }) => {
  test.skip(!enabled, 'NOT RUN: dedicated W1 owner bootstrap, host, and evidence paths are required');
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
  await card.getByRole('button', { name: 'Expand visual: Resource measurements' }).click({ timeout: 30_000 });
  await expect(page.getByRole('region', { name: 'Resource measurement' })).toBeVisible();
  await expect(page.getByRole('row', { name: /Available CPU cores/ })).toBeVisible();

  const viewer = page.getByRole('dialog', { name: 'Resource measurements' });
  await viewer.getByRole('button', { name: 'Close', exact: true }).click({ timeout: 30_000 });
  await expect(viewer).toBeHidden({ timeout: 30_000 });
  await page.getByRole('button', { name: 'TH', exact: true }).click({ timeout: 30_000 });
  const thaiCard = page.getByRole('region', { name: 'ผลลัพธ์', exact: true }).getByRole('article', { name: 'Resource measurements', exact: true });
  await thaiCard.getByRole('button', { name: 'ขยายภาพ: Resource measurements', exact: true }).click({ timeout: 30_000 });
  await expect(page.getByRole('region', { name: 'ข้อมูลทรัพยากร' })).toBeVisible();
  await expect(page.getByRole('row', { name: /แกนประมวลผลที่ใช้ได้/ })).toBeVisible();
  await viewer.getByRole('button', { name: 'ปิด', exact: true }).click({ timeout: 30_000 });
  await expect(viewer).toBeHidden({ timeout: 30_000 });
  await page.getByRole('button', { name: 'EN', exact: true }).click({ timeout: 30_000 });
  await card.getByRole('button', { name: 'Expand visual: Resource measurements', exact: true }).click({ timeout: 30_000 });
  await expect(page.getByRole('region', { name: 'Resource measurement' })).toBeVisible();
  await expect(page.getByRole('row', { name: /Available CPU cores/ })).toBeVisible();
  await viewer.getByRole('button', { name: 'Close', exact: true }).click({ timeout: 30_000 });
  await expect(viewer).toBeHidden({ timeout: 30_000 });

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

const w2SessionFile = process.env.SCIENTIFIC_W2_OWNER_SESSION_FILE;
const w2ProofFile = process.env.SCIENTIFIC_W2_PROOF;
const w2EvidenceFile = process.env.SCIENTIFIC_W2_BROWSER_EVIDENCE;
const w2Origin = process.env.SCIENTIFIC_W2_API_ORIGIN;
const w2Python = process.env.SCIENTIFIC_W2_PYTHON;
const w2CsvFixture = process.env.SCIENTIFIC_W2_CSV_FIXTURE;
const w2Enabled = Boolean(w2SessionFile && w2ProofFile && w2EvidenceFile && w2Origin && w2Python && w2CsvFixture);

test('W2 real scientific workflow uploads CSV, retrieves Crossref, and publishes four compute outputs', async ({ page }) => {
  test.skip(!w2Enabled, 'NOT RUN: dedicated W2 authenticated owner session, host, and evidence paths are required');
  test.setTimeout(600_000);
  const proof = JSON.parse(await readFile(w2ProofFile!, 'utf8')) as {
    project_id: string; session_id: string; compute_image_digest: string;
    compute_recipe_manifest_sha256: string; source_hashes: { csv_fixture_sha256: string };
  };
  const csvBytes = await readFile(w2CsvFixture!);
  const csvSha256 = createHash('sha256').update(csvBytes).digest('hex');
  if (proof.source_hashes.csv_fixture_sha256 !== csvSha256) throw new Error('W2 CSV fixture differs its prepared hash');
  const origin = new URL(w2Origin!).origin;
  const gotoHost = (pathname: string) => page.goto(new URL(pathname, origin).toString());
  const cookies = JSON.parse(await readFile(w2SessionFile!, 'utf8'));
  if (!Array.isArray(cookies) || cookies.length !== 1 || cookies[0].name !== 'owner_session'
      || cookies[0].url !== origin || typeof cookies[0].value !== 'string' || !cookies[0].value
      || cookies[0].httpOnly !== true || cookies[0].sameSite !== 'Strict')
    throw new Error('W2 owner session does not match actual host');
  await page.context().addCookies(cookies);
  await gotoHost('/');
  if (new URL(page.url()).origin !== origin) throw new Error('W2 owner session left actual host');
  await page.getByRole('button', { name: 'EN' }).click();

  await gotoHost(`/projects/${encodeURIComponent(proof.project_id)}/library`);
  await expect(page.getByRole('heading', { name: 'Sources & outputs' })).toBeVisible();
  await page.locator('#library-upload').setInputFiles(w2CsvFixture!);
  await expect.poll(async () => page.evaluate(async (projectId) => {
    const response = await fetch(`/api/v1/projects/${encodeURIComponent(projectId)}/files`, { credentials: 'same-origin' });
    if (!response.ok) return null;
    const files = await response.json() as Array<{ id: string; filename: string; content_type: string; state: string }>;
    return files.find((file) => file.filename === 'w2_partial.csv') ?? null;
  }, proof.project_id), { timeout: 60_000 }).toBeTruthy();
  const file = await page.evaluate(async (projectId) => {
    const response = await fetch(`/api/v1/projects/${encodeURIComponent(projectId)}/files`, { credentials: 'same-origin' });
    if (!response.ok) throw new Error('W2 uploaded file list could not be read');
    const files = await response.json() as Array<{ id: string; filename: string; content_type: string; state: string }>;
    return files.find((item) => item.filename === 'w2_partial.csv') ?? null;
  }, proof.project_id) as { id: string; filename: string; content_type: string; state: string } | null;
  if (!file || file.content_type.split(';')[0].trim().toLowerCase() !== 'text/csv') {
    throw new Error('W2 upload did not create the expected text/csv input');
  }
  await expect.poll(async () => page.evaluate(async ({ projectId, fileId }) => {
    const response = await fetch(`/api/v1/projects/${encodeURIComponent(projectId)}/files`, { credentials: 'same-origin' });
    if (!response.ok) return false;
    const files = await response.json() as Array<{ id: string; state: string }>;
    return files.find((item) => item.id === fileId)?.state === 'ready';
  }, { projectId: proof.project_id, fileId: file.id }), { timeout: 60_000 }).toBe(true);

  await gotoHost(`/projects/${encodeURIComponent(proof.project_id)}/sessions/${encodeURIComponent(proof.session_id)}`);
  await expect(page.getByRole('heading', { name: 'Research chat' })).toBeVisible();
  await page.getByLabel('Research question').fill('Compare Crossref records about coastal nitrate monitoring with the selected CSV measurements.');
  await page.getByLabel('Research workflow').selectOption('crossref_csv');
  await page.getByLabel('Crossref query').fill('coastal nitrate monitoring');
  await page.getByLabel('CSV input file').selectOption(file.id);
  await page.getByLabel('Numeric columns').fill('temperature_c,nitrate_mg_l');
  await page.getByRole('button', { name: 'Add to question: w2_partial.csv' }).click();
  await page.getByRole('button', { name: 'Review plan' }).click();
  const planRegion = page.getByRole('region', { name: 'Plan review' });
  await expect(planRegion).toBeVisible({ timeout: 30_000 });
  await expect.poll(() => new URL(page.url()).searchParams.get('run'), { timeout: 30_000 })
    .toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  const runId = new URL(page.url()).searchParams.get('run');
  if (!runId) throw new Error('W2 UI did not create a run after plan review');
  await planRegion.getByRole('button', { name: 'Prepare plan', exact: true }).click({ timeout: 30_000 });

  await expect.poll(async () => page.evaluate(async (id) => {
    const [planResponse, readinessResponse] = await Promise.all([
      fetch(`/api/v1/runs/${encodeURIComponent(id)}/plan`, { credentials: 'same-origin' }),
      fetch(`/api/v1/runs/${encodeURIComponent(id)}/readiness`, { credentials: 'same-origin' }),
    ]);
    if (!planResponse.ok || !readinessResponse.ok) return false;
    const currentPlan = await planResponse.json() as {
      run_id: string; revision: number; plan_digest: string;
      plan: { scientific?: {
        capability_ids: string[];
        approved_crossref_queries: { crossref?: { query: string | null } };
        csv_describe_grants: { csv_describe?: {
          numeric_columns: string[]; input_ref: { key: string; size: number };
          input_sha256: string; recipe_manifest_sha256: string; image_digest: string;
        } };
        required_compute_profiles: Array<{ profile_id: string; image_digest: string }>;
      } | null };
    };
    const readiness = await readinessResponse.json() as {
      run_id: string; revision: number; plan_digest: string; state: string;
      requirements: Array<{ id: string; state: string }>;
    };
    const scientific = currentPlan.plan.scientific;
    return currentPlan.run_id === id && Boolean(scientific) &&
      scientific!.capability_ids.includes('paper-lookup') &&
      scientific!.capability_ids.includes('exploratory-data-analysis') &&
      scientific!.approved_crossref_queries.crossref?.query === 'coastal nitrate monitoring' &&
      JSON.stringify(scientific!.csv_describe_grants.csv_describe?.numeric_columns) === JSON.stringify(['temperature_c', 'nitrate_mg_l']) &&
      scientific!.csv_describe_grants.csv_describe?.input_sha256 === csvSha256 &&
      scientific!.csv_describe_grants.csv_describe?.input_ref.size === csvBytes.byteLength &&
      scientific!.csv_describe_grants.csv_describe?.recipe_manifest_sha256 === proof.compute_recipe_manifest_sha256 &&
      scientific!.csv_describe_grants.csv_describe?.image_digest === proof.compute_image_digest &&
      scientific!.required_compute_profiles.some((profile) => profile.profile_id === 'prof.csv-stdlib@py3.14.7' && profile.image_digest === proof.compute_image_digest) &&
      readiness.run_id === id &&
      readiness.revision === currentPlan.revision && readiness.plan_digest === currentPlan.plan_digest;
  }, runId), { timeout: 180_000 }).toBe(true);

    const setupQuery = new URLSearchParams({ session: proof.session_id, run: runId });
    await gotoHost(`/projects/${encodeURIComponent(proof.project_id)}/research-setup?${setupQuery}`);
    await expect(page.getByRole('heading', { name: 'Research Setup', exact: true })).toBeVisible();
    for (const [profileId, label] of [
      ['prof.worker-base@py3.14.7', 'Resource measurement environment'],
      ['prof.csv-stdlib@py3.14.7', 'CSV descriptive statistics'],
    ] as const) {
      const profileCard = page.locator('.setup-profile').filter({ has: page.getByRole('heading', { name: label, exact: true }) });
      await expect(profileCard).toBeVisible({ timeout: 30_000 });
      const prepareButton = profileCard.getByRole('button', { name: 'Prepare environment', exact: true });
      if (await prepareButton.isVisible().catch(() => false)) await prepareButton.click({ timeout: 30_000 });
      await expect.poll(async () => page.evaluate(async ({ projectId, profileId }) => {
        const response = await fetch(`/api/v1/projects/${encodeURIComponent(projectId)}/research-setup`, { credentials: 'same-origin' });
        if (!response.ok) return false;
        const setup = await response.json() as {
          project_id: string;
          profiles: Array<{ profile_id: string; version: string; manifest_sha256: string }>;
          preparations: Array<{ project_id: string; profile_id: string; version: string; manifest_sha256: string; state: string; stage: string; evidence_verified: boolean }>;
        };
        const profile = setup.profiles.find((item) => item.profile_id === profileId);
        return setup.project_id === projectId && !!profile && setup.preparations.some((job) =>
          job.project_id === projectId && job.profile_id === profile.profile_id && job.version === profile.version &&
          job.manifest_sha256 === profile.manifest_sha256 && job.state === 'ready' && job.stage === 'complete' && job.evidence_verified === true);
      }, { projectId: proof.project_id, profileId }), { timeout: 300_000, intervals: [1_000, 2_000, 5_000] }).toBe(true);
      await expect(profileCard.getByText('Environment ready', { exact: true })).toBeVisible({ timeout: 30_000 });
    }
    await page.getByRole('link', { name: 'Return to plan', exact: true }).click();
    await expect(planRegion).toBeVisible({ timeout: 30_000 });
  await planRegion.getByLabel('Token limit').fill('200000');
  await planRegion.getByLabel('Time limit (seconds)').fill('600');
  await planRegion.getByRole('button', { name: 'Save edits', exact: true }).click();
  await expect(planRegion.getByLabel('Token limit')).toHaveValue('200000');
  await expect(planRegion.getByLabel('Time limit (seconds)')).toHaveValue('600');

  await planRegion.getByRole('button', { name: 'Refresh readiness', exact: true }).click().catch(() => undefined);
  const refreshed = await page.evaluate(async (id) => {
    const [planResponse, readinessResponse] = await Promise.all([
      fetch(`/api/v1/runs/${encodeURIComponent(id)}/plan`, { credentials: 'same-origin' }),
      fetch(`/api/v1/runs/${encodeURIComponent(id)}/readiness`, { credentials: 'same-origin' }),
    ]);
    return { plan: await planResponse.json(), readiness: await readinessResponse.json() };
  }, runId);
  if (refreshed.plan.run_id !== runId || refreshed.readiness.run_id !== runId ||
      refreshed.plan.revision !== refreshed.readiness.revision ||
      refreshed.plan.plan_digest !== refreshed.readiness.plan_digest ||
      refreshed.plan.plan.token_limit !== 200_000 || refreshed.plan.plan.elapsed_limit_ms !== 600_000 ||
      refreshed.readiness.state !== 'ready') {
    throw new Error('W2 plan and readiness did not refresh to the same revision');
  }
  await expect(planRegion.getByRole('button', { name: 'Approve plan', exact: true })).toBeEnabled({ timeout: 30_000 });
  await planRegion.getByRole('button', { name: 'Approve plan', exact: true }).click();
  await expect.poll(async () => page.evaluate(async (id) => {
    const response = await fetch(`/api/v1/runs/${encodeURIComponent(id)}`, { credentials: 'same-origin' });
    if (!response.ok) return false;
    const run = await response.json() as { state: string; artifacts: Array<{ title: string; partial: boolean }> };
    return run.state === 'completed' && run.artifacts.length === 4 && run.artifacts.every((item) => !item.partial);
  }, runId), { timeout: 300_000 }).toBe(true);

  const expected = ['summary.json', 'summary.csv', 'chart.svg', 'report.md'];
  const outputs = page.getByRole('region', { name: 'Outputs' });
  for (const name of expected) {
    const card = outputs.getByRole('article', { name, exact: true });
    await expect(card).toBeVisible({ timeout: 30_000 });
    await expect(card.getByText('Partial output')).toHaveCount(0);
  }
  const chartCard = outputs.getByRole('article', { name: 'chart.svg', exact: true });
  await chartCard.getByRole('button', { name: 'Expand visual: chart.svg', exact: true }).click();
  const chartViewer = page.getByRole('dialog', { name: 'chart.svg' });
  await expect(chartViewer).toBeVisible();
  const chartImage = chartViewer.locator('img');
  await expect(chartImage).toBeVisible();
  await expect.poll(() => chartImage.evaluate((image) => {
    const element = image as HTMLImageElement;
    return element.complete && element.naturalWidth > 0 && element.naturalHeight > 0;
  })).toBe(true);
  await chartViewer.getByRole('button', { name: 'Close', exact: true }).click();
  await page.reload();
  if (new URL(page.url()).origin !== origin) throw new Error('W2 artifact refresh left the actual host');
  await expect(page.getByRole('region', { name: 'Outputs' })).toBeVisible({ timeout: 30_000 });

  const run = await page.evaluate(async (id) => {
    const response = await fetch(`/api/v1/runs/${encodeURIComponent(id)}`, { credentials: 'same-origin' });
    if (!response.ok) throw new Error('W2 run read failed after refresh');
    return await response.json() as { artifacts: Array<{
      artifact_id: string; title: string; partial: boolean; sha256: string; size: number;
    }> };
  }, runId);
  const artifacts = Object.fromEntries(run.artifacts.map((item) => [item.title, item]));
  if (Object.keys(artifacts).sort().join(',') !== [...expected].sort().join(',')) {
    throw new Error('W2 run does not expose the four expected output artifacts');
  }
  const outputEvidence: Record<string, { artifact_id: string; sha256: string; size: number }> = {};
  for (const name of expected) {
    const artifact = artifacts[name];
    if (artifact.partial) throw new Error(`W2 output is partial: ${name}`);
    const card = outputs.getByRole('article', { name, exact: true });
    const downloadPromise = page.waitForEvent('download');
    await card.getByRole('link', { name: 'Download output', exact: true }).click();
    const download = await downloadPromise;
    const stream = await download.createReadStream();
    if (!stream) throw new Error(`W2 download stream unavailable: ${name}`);
    const digest = createHash('sha256');
    let size = 0;
    for await (const chunk of stream) {
      size += chunk.length;
      digest.update(chunk);
    }
    const sha256 = digest.digest('hex');
    if (sha256 !== artifact.sha256 || size !== artifact.size) {
      throw new Error(`W2 downloaded bytes differ from artifact metadata: ${name}`);
    }
    outputEvidence[name] = { artifact_id: artifact.artifact_id, sha256, size };
  }
  await mkdir(path.dirname(w2EvidenceFile!), { recursive: true, mode: 0o700 });
  await writeFile(w2EvidenceFile!, JSON.stringify({
    status: 'PASS', project_id: proof.project_id, session_id: proof.session_id, run_id: runId,
    query: 'coastal nitrate monitoring', csv_columns: ['temperature_c', 'nitrate_mg_l'],
    outputs: outputEvidence, authenticated_owner_session_transferred: true, setup_refresh: true,
  }) + '\n', { mode: 0o600 });
  await chmod(w2EvidenceFile!, 0o600);
  const checker = path.resolve(process.cwd(), '../../backend/tests/live/w2_scientific_acceptance.py');
  const readback = spawnSync(w2Python!, [checker, '--verify-browser-run'], {
    encoding: 'utf8', timeout: 60_000, env: process.env,
  });
  if (readback.error || readback.status !== 0) throw new Error('W2 native PostgreSQL/S3/output readback failed');
});
