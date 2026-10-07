import { expect, test, type Page } from '@playwright/test';
import { createHash } from 'node:crypto';
import { strictPlanView } from '../src/api';
import { installResearchFixtureRoutes, makeRun, makePlan, PROJECT_ID, SESSION_ID, SESSION_URL, NEW_RUN_ID, PLOT_ID, READY_FILE } from './fixtures/research';
import type { PreparationJobView, ResearchSetupView, RunReadinessView } from '../../../contracts/api-types';

test.setTimeout(20000);
const SETUP_URL = `/projects/${PROJECT_ID}/research-setup?session=${SESSION_ID}&run=${NEW_RUN_ID}`;
const PROFILE = { profile_id: 'reviewed-cpu', version: '1', manifest_sha256: 'b'.repeat(64), label: 'Scientific calculations', purpose: 'Analyze the selected measurements.', state: 'missing' as const, memory_limit_bytes: 1073741824, workspace_limit_bytes: 536870912 };
const JOB_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const job = (patch: Partial<PreparationJobView> = {}): PreparationJobView => ({ id: JOB_ID, project_id: PROJECT_ID, profile_id: PROFILE.profile_id, version: PROFILE.version, manifest_sha256: PROFILE.manifest_sha256, state: 'checking', stage: 'security', evidence_verified: false, ...patch });

for (const workflow of ['literature', 'resources'] as const) {
  test(`explicit ${workflow} preparation uses the created run revision without auto-approval`, async ({ page }) => {
    const fixture = await installResearchFixtureRoutes(page, { run: null });
    const preparations: unknown[] = [];
    await page.route(`**/api/v1/runs/${NEW_RUN_ID}/prepare-plan`, (route) => {
      preparations.push(route.request().postDataJSON());
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(makeRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [], revision: 1 })) });
    });
    const question = 'Measure the selected workspace '.repeat(7).trim();
    await page.goto(SESSION_URL);
    await page.getByLabel('Research question').fill(question);
    await page.getByText('Workflow options', { exact: true }).click();
    await page.getByLabel('Research workflow').selectOption(workflow);
    await page.getByRole('button', { name: 'Send question', exact: true }).click();
    await expect.poll(() => preparations.length).toBe(1);
    expect(preparations[0]).toEqual({ expected_revision: 1, workflow, search_terms: workflow === 'resources' ? [] : [question.slice(0, 150)] });
    expect(fixture.writes.filter((write) => write.path.endsWith('/approve'))).toHaveLength(0);
  });
}

test('owner budget edits preserve the plan and require refreshed review before approval', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [] }) });
  let plan = makePlan();
  const patches: any[] = [];
  await page.route(`**/api/v1/runs/${NEW_RUN_ID}/plan`, (route) => {
    if (route.request().method() === 'PATCH') {
      const body = route.request().postDataJSON(); patches.push(body);
      plan = { ...makePlan(2), plan: body.plan };
      fixture.setRun({ revision: 2 });
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(makeRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [], revision: 2 })) });
    }
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify(plan) });
  });
  await page.route(`**/api/v1/runs/${NEW_RUN_ID}/readiness`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ run_id: NEW_RUN_ID, revision: plan.revision, plan_digest: plan.plan_digest, state: 'ready', requirements: [] }) }));
  await page.goto(SESSION_URL);
  await page.getByText('Edit stages and limits', { exact: true }).click();
  await page.getByLabel('Token limit').fill('1000');
  await page.getByLabel('Time limit (seconds)').fill('120');
  await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Save edits', exact: true }).click();
  await expect(page.getByLabel('Token limit')).toHaveValue('1000');
  await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toBeEnabled();
  expect(patches).toEqual([{ expected_revision: 1, plan: { ...makePlan().plan, token_limit: 1000, elapsed_limit_ms: 120000 } }]);
  await page.getByRole('button', { name: 'Approve plan', exact: true }).click();
  await expect.poll(() => fixture.writes.filter((write) => write.path.endsWith('/approve')).length).toBe(1);
  expect(fixture.writes.find((write) => write.path.endsWith('/approve'))!.body).toEqual({ expected_revision: 2, plan_digest: plan.plan_digest });
});

test('scientific approval requires the current verified binding on the final readiness read', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [] }) });
  const plan = makePlan();
  plan.plan.scientific = { catalog_commit: '154988403bb5a18e9d3c0ce4e6d5e2e4b184a298', registry_sha256: 'a'.repeat(64), capability_ids: ['workspace-resources'], instruction_fingerprint: 'b'.repeat(64), profile_id: PROFILE.profile_id, image_digest: `sha256:${'c'.repeat(64)}`, input_snapshot_digest: plan.plan.input_snapshot_digest, max_result_bytes: 1024, timeout_ms: 1000, memory_limit_bytes: PROFILE.memory_limit_bytes, workspace_limit_bytes: PROFILE.workspace_limit_bytes };
  let binding: string | null = null;
  await page.route(`**/api/v1/runs/${NEW_RUN_ID}/plan`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(plan) }));
  await page.route(`**/api/v1/runs/${NEW_RUN_ID}/readiness`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ run_id: NEW_RUN_ID, revision: plan.revision, plan_digest: plan.plan_digest, binding_sha256: binding, state: 'ready', requirements: [] }) }));
  await page.goto(SESSION_URL);
  await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toBeDisabled();
  binding = 'f'.repeat(64);
  await page.getByRole('button', { name: 'Refresh readiness', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toBeEnabled();
  binding = null;
  await page.getByRole('button', { name: 'Approve plan', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toBeDisabled();
  expect(fixture.writes.filter((write) => write.path.endsWith('/approve'))).toHaveLength(0);
});

async function setupApi(page: Page, options: { lostFirst?: boolean; unverifiedReady?: boolean; ownerReadyWithoutProjectJob?: boolean } = {}) {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [] }) });
  let jobs: PreparationJobView[] = options.unverifiedReady ? [job({ state: 'ready', stage: 'complete' })] : [];
  let revision = 1;
  let readinessState: RunReadinessView['state'] = 'missing';
  let readinessFailure = false;
  let staleReadiness = false;
  const submissions: Array<Record<string, unknown>> = [];
  const reads: string[] = [];
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
    const method = route.request().method();
    const json = (data: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    if (method === 'GET' && path === '/capabilities') return json({ file_types: ['text/csv'], max_upload_bytes: 1000000, protocols: {} });
    if (method === 'GET') reads.push(path);
    if (method === 'GET' && path === `/projects/${PROJECT_ID}/research-setup`) {
      const view: ResearchSetupView = { project_id: PROJECT_ID, requirements: [{ id: 'environment', label: 'Calculation environment', purpose: 'Use the reviewed calculation environment.', state: readinessState, action: 'prepare_environment' }], profiles: [{ ...PROFILE, state: jobs.length ? 'preparing' : options.ownerReadyWithoutProjectJob ? 'ready' : 'missing' }], connections: [{ id: '12121212-1212-4212-8212-121212121212', label: 'Configured research connection', provider: 'https://provider.example', model: 'fixture-model', state: 'ready', has_secret: true }], preparations: jobs };
      return json(view);
    }
    if (method === 'POST' && path === `/projects/${PROJECT_ID}/preparations`) {
      submissions.push(route.request().postDataJSON());
      if (options.lostFirst && submissions.length === 1) return route.abort('failed');
      jobs = [options.ownerReadyWithoutProjectJob ? job({ state: 'ready', stage: 'complete', evidence_verified: true }) : job()];
      return json(jobs[0], 202);
    }
    if (method === 'GET' && path === `/projects/${PROJECT_ID}/preparations/${JOB_ID}`) return json(jobs[0]);
    if (method === 'GET' && path === `/runs/${NEW_RUN_ID}/readiness`) {
      if (readinessFailure) return json({ code: 'storage_unavailable', request_id: 'setup-test' }, 503);
      return json({ run_id: NEW_RUN_ID, revision: staleReadiness ? revision - 1 : revision, plan_digest: makePlan(staleReadiness ? revision - 1 : revision).plan_digest, state: readinessState, binding_sha256: 'c'.repeat(64), requirements: [] });
    }
    if (method === 'GET' && path === `/runs/${NEW_RUN_ID}/plan`) return json(makePlan(revision));
    return route.fallback();
  });
  return { fixture, submissions, reads, setJob: (next: PreparationJobView) => { jobs = [next]; }, setReadiness: (state: RunReadinessView['state']) => { readinessState = state; }, changeRevision: (next: number) => { revision = next; fixture.setRun({ revision: next }); }, failReadiness: () => { readinessFailure = true; }, staleReadiness: (value: boolean) => { staleReadiness = value; } };
}

test('shared setup is reachable from Settings and reading never starts preparation', async ({ page }) => {
  const api = await setupApi(page);
  await page.goto('/settings');
  await page.getByRole('link', { name: 'Research Setup' }).first().click();
  await expect(page.getByRole('heading', { name: 'Research Setup', exact: true })).toBeVisible();
  await expect(page.getByText(PROFILE.label, { exact: true })).toBeVisible();
  await expect(page.getByText('Configured; verification not run', { exact: false }).first()).toBeVisible();
  await page.reload();
  await expect(page.getByText(PROFILE.purpose)).toBeVisible();
  expect(api.submissions).toHaveLength(0);
});

test('Crossref CSV preparation requires explicit query and ready CSV, then rechecks the V2 plan before approval', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: null });
  const prepareBodies: Array<Record<string, unknown>> = [];
  const plan = validCsvPlanV2();
  const readyRun = makeRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [] });
  let revision = 1;
  await page.route(`**/api/v1/projects/${PROJECT_ID}/runs`, (route) => route.fulfill({ contentType: 'application/json', body: '[]' }));
  await page.route(`**/api/v1/sessions/${SESSION_ID}/runs`, async (route) => {
    const body = route.request().postDataJSON();
    expect(body.input_ids).toEqual([READY_FILE]);
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify(readyRun), status: 201 });
  });
  await page.route(`**/api/v1/runs/${NEW_RUN_ID}/prepare-plan`, async (route) => {
    prepareBodies.push(route.request().postDataJSON());
    revision = 2;
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ...readyRun, revision }) });
  });
  await page.route(`**/api/v1/projects/${PROJECT_ID}/files/${READY_FILE}/content`, (route) => route.fulfill({ contentType: 'text/csv', body: 'x,y\n1,2\n3,\n5,6\n' }));
  await page.route(`**/api/v1/runs/${NEW_RUN_ID}/plan`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ...plan, revision, plan_digest: `${'d'.repeat(63)}${revision}` }) }));
  await page.route(`**/api/v1/runs/${NEW_RUN_ID}/readiness`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ run_id: NEW_RUN_ID, revision, plan_digest: `${'d'.repeat(63)}${revision}`, binding_sha256: '9'.repeat(64), state: 'ready', requirements: [] }) }));
  await page.route(`**/api/v1/runs/${NEW_RUN_ID}`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ...readyRun, revision }) }));

  await page.goto(SESSION_URL);
  await page.getByLabel('Research question').fill('Describe these measurements and find related studies.');
  await page.getByText('Workflow options', { exact: true }).click();
  await page.getByLabel('Research workflow').selectOption('crossref_csv');
  await page.getByLabel('Crossref query').fill('microplastic exposure');
  await page.getByLabel('CSV input file').selectOption(READY_FILE);
  await page.getByLabel('Numeric columns').fill('x,y');
  await page.getByRole('button', { name: 'TH', exact: true }).click();
  await expect(page.getByLabel('คำค้น Crossref')).toHaveValue('microplastic exposure');
  await expect(page.getByLabel('คอลัมน์ตัวเลข')).toHaveValue('x,y');
  await page.getByRole('button', { name: 'EN', exact: true }).click();
  await page.getByRole('button', { name: 'Send question' }).click();
  await expect(page.getByRole('region', { name: 'Plan review' })).toBeVisible();
  await expect.poll(() => prepareBodies.length).toBe(1);
  await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toBeEnabled();
  expect(prepareBodies).toEqual([{
    expected_revision: 1,
    workflow: 'crossref_csv',
    search_terms: [],
    csv_selection: {
      crossref: { source_id: 'crossref', version: 1, access_mode: 'public_read', query: 'microplastic exposure', doi: null, limit: 10 },
      csv_file_id: READY_FILE,
      numeric_columns: ['x', 'y'],
    },
  }]);
  await page.getByRole('button', { name: 'Approve plan', exact: true }).click();
  await expect.poll(() => fixture.writes.filter((write) => write.path.endsWith('/approve')).length).toBe(1);
  expect(fixture.writes.find((write) => write.path.endsWith('/approve'))!.body.expected_revision).toBe(2);
});

function validCsvPlanV2() {
  const plan = makePlan();
  const input = 'x,y\n1,2\n3,\n5,6\n';
  const inputHash = createHash('sha256').update(input).digest('hex');
  return {
    ...plan,
    plan: {
      ...plan.plan,
      allowed_ops: ['llm', 'search', 'compute'],
      data_recipients: ['fixture-provider', 'https://api.crossref.org'],
      scientific: {
        binding_version: 2,
        catalog_commit: '154988403bb5a18e9d3c0ce4e6d5e2e4b184a298',
        registry_sha256: 'a'.repeat(64),
        capability_ids: ['paper-lookup', 'exploratory-data-analysis'],
        instruction_fingerprint: 'b'.repeat(64),
        agent_runtime_pins: { runtime_commit: 'bd0affe5e5f723579df8902852f5d0c47795f355', image_digest: `sha256:${'c'.repeat(64)}`, skills_digest: 'd'.repeat(64), environment_digest: 'e'.repeat(64) },
        input_snapshot_digest: plan.plan.input_snapshot_digest,
        approved_crossref_queries: { crossref: { source_id: 'crossref', version: 1, access_mode: 'public_read', query: 'microplastic exposure', doi: null, limit: 10 } },
        required_compute_profiles: [{ profile_id: 'prof.csv-stdlib@py3.14.7', version: '1', image_digest: `sha256:${'f'.repeat(64)}` }],
        csv_describe_grants: { csv_describe: {
          recipe_id: 'csv.describe.v1', recipe_version: '1', recipe_manifest_sha256: '1'.repeat(64),
          profile_id: 'prof.csv-stdlib@py3.14.7', profile_version: '1', image_digest: `sha256:${'f'.repeat(64)}`,
          input_ref: { project_id: PROJECT_ID, key: `inputs/${inputHash}`, sha256: inputHash, size: Buffer.byteLength(input), content_type: 'application/octet-stream' },
          input_sha256: inputHash, numeric_columns: ['x', 'y'], max_input_bytes: 1048576,
          max_output_bytes: 262144, timeout_ms: 30000, memory_limit_bytes: 1073741824, workspace_limit_bytes: 67108864,
        } },
      },
    },
  };
}

test('V2 scientific plans reject unknown fields, unsafe limits, incomplete capabilities, and unknown versions', () => {
  const run = makeRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [] });
  const cases: Array<[string, (binding: any) => void]> = [
    ['endpoint', (binding) => { binding.approved_crossref_queries.crossref.endpoint = 'https://evil.example'; }],
    ['credential', (binding) => { binding.approved_crossref_queries.crossref.api_key = 'synthetic-only'; }],
    ['timeout', (binding) => { binding.csv_describe_grants.csv_describe.timeout_ms = 60000; }],
    ['output limit', (binding) => { binding.csv_describe_grants.csv_describe.max_output_bytes = 1048576; }],
    ['input limit', (binding) => { binding.csv_describe_grants.csv_describe.max_input_bytes = 1048577; }],
    ['input object size', (binding) => { binding.csv_describe_grants.csv_describe.input_ref.size = 1048577; }],
    ['C1 query control', (binding) => { binding.approved_crossref_queries.crossref.query = 'microplastic\u0085 exposure'; }],
    ['C1 DOI control', (binding) => { binding.approved_crossref_queries.crossref.query = null; binding.approved_crossref_queries.crossref.doi = '10.1234/example\u0085record'; }],
    ['C1 CSV header control', (binding) => { binding.csv_describe_grants.csv_describe.numeric_columns = ['x\u0085y']; }],
    ['required capability', (binding) => { binding.capability_ids = ['paper-lookup']; }],
    ['binding version', (binding) => Object.assign(binding, { binding_version: 3, profile_id: 'reviewed-cpu', profile_version: '1', tool_version: '1', image_digest: `sha256:${'c'.repeat(64)}`, parameters: {}, max_result_bytes: 1024, timeout_ms: 1000, memory_limit_bytes: 1073741824, workspace_limit_bytes: 67108864 })],
  ];
  for (const [label, mutate] of cases) {
    const plan = validCsvPlanV2();
    mutate(plan.plan.scientific as any);
    expect(strictPlanView(plan, run), label).toBe(false);
  }
});

test('CSV plan review shows its saved authority and requires reprepare after draft changes', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [] }) });
  const plan = validCsvPlanV2();
  await page.addInitScript(({ key, value }) => sessionStorage.setItem(key, JSON.stringify(value)), {
    key: `research-draft:${SESSION_ID}`,
    value: { question: 'Describe the measurements.', selected: [], workflow: 'crossref_csv', crossrefMode: 'query', crossrefTerm: 'microplastic exposure', csvFileId: READY_FILE, csvColumnsText: 'x,y' },
  });
  const input = 'x,y\n1,2\n3,\n5,6\n';
  const inputHash = createHash('sha256').update(input).digest('hex');
  await page.route(`**/api/v1/projects/${PROJECT_ID}/files/${READY_FILE}/content`, (route) => route.fulfill({ contentType: 'text/csv', body: input }));
  await page.route(`**/api/v1/runs/${NEW_RUN_ID}/plan`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(plan) }));
  await page.route(`**/api/v1/runs/${NEW_RUN_ID}/readiness`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ run_id: NEW_RUN_ID, revision: plan.revision, plan_digest: plan.plan_digest, binding_sha256: '9'.repeat(64), state: 'ready', requirements: [] }) }));
  await page.goto(SESSION_URL);
  await expect(page.getByText('microplastic exposure', { exact: true })).toBeVisible();
  await expect(page.getByText(new RegExp(`inputs/${inputHash}`))).toBeVisible();
  await expect(page.getByText(/x, y/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toBeEnabled();
  await page.getByText('Next question', { exact: true }).click();
  await page.getByText('Workflow options', { exact: true }).click();
  await page.getByLabel('Crossref query').fill('different query');
  await page.getByLabel('Numeric columns').fill('x,z');
  await expect(page.getByText('The current draft differs from the saved scientific inputs. Prepare the plan again before approval.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toBeDisabled();
  expect(fixture.writes.filter((write) => write.path.endsWith('/approve'))).toHaveLength(0);
});

test('malformed V2 scientific binding blocks plan approval', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [] }) });
  const invalidPlan = { ...makePlan(), plan: { ...makePlan().plan, scientific: {
    binding_version: 2,
    catalog_commit: '154988403bb5a18e9d3c0ce4e6d5e2e4b184a298',
    registry_sha256: 'a'.repeat(64), capability_ids: ['paper-lookup'], instruction_fingerprint: 'b'.repeat(64),
    agent_runtime_pins: { image_digest: 'invalid', skills_digest: 'd'.repeat(64), environment_digest: 'e'.repeat(64) },
    input_snapshot_digest: 'e'.repeat(64),
  } } };
  await page.route(`**/api/v1/runs/${NEW_RUN_ID}/plan`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(invalidPlan) }));
  await page.route(`**/api/v1/runs/${NEW_RUN_ID}/readiness`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ run_id: NEW_RUN_ID, revision: 1, plan_digest: makePlan().plan_digest, binding_sha256: '9'.repeat(64), state: 'ready', requirements: [] }) }));
  await page.goto(SESSION_URL);
  await expect(page.getByRole('alert')).toContainText('invalid response');
  await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toHaveCount(0);
  expect(fixture.writes.filter((write) => write.path.endsWith('/approve'))).toHaveLength(0);
});

test('ready owner profile can bind its verified preparation to a project', async ({ page }) => {
  const api = await setupApi(page, { ownerReadyWithoutProjectJob: true });
  await page.goto(SETUP_URL);
  await expect(page.getByText('Preparation evidence is not verified.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Prepare environment', exact: true }).click();
  await expect(page.getByText('Environment ready', { exact: true })).toBeVisible();
  expect(api.submissions).toHaveLength(1);
  expect(api.submissions[0]).toEqual({ profile_id: PROFILE.profile_id, version: PROFILE.version, manifest_sha256: PROFILE.manifest_sha256, request_id: expect.stringMatching(/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/) });
});

test('unverified preparation cannot appear ready', async ({ page }) => {
  const api = await setupApi(page, { unverifiedReady: true });
  await page.goto(SETUP_URL);
  await expect(page.getByText('Preparation evidence is not verified.', { exact: true })).toBeVisible();
  await expect(page.getByText('Environment ready', { exact: true })).toHaveCount(0);
  expect(api.submissions).toHaveLength(0);
});

test('invalid profile state fails closed before preparation', async ({ page }) => {
  const api = await setupApi(page);
  await page.route(`**/api/v1/projects/${PROJECT_ID}/research-setup`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ project_id: PROJECT_ID, requirements: [], profiles: [{ ...PROFILE, state: 'unrecognized' }], connections: [], preparations: [] }) }));
  await page.goto(SETUP_URL);
  await expect(page.getByRole('alert')).toContainText('invalid response');
  await expect(page.getByRole('button', { name: 'Prepare environment' })).toHaveCount(0);
  expect(api.submissions).toHaveLength(0);
});

test('lost preparation response reuses one UUID after refresh without automatic resend', async ({ page }) => {
  const api = await setupApi(page, { lostFirst: true });
  await page.goto(SETUP_URL);
  await page.getByRole('button', { name: 'Prepare environment' }).click();
  await expect(page.getByRole('alert')).toContainText('could not confirm');
  expect(api.submissions).toHaveLength(1);
  await page.reload();
  await expect(page.getByRole('button', { name: 'Check preparation request' })).toBeVisible();
  expect(api.submissions).toHaveLength(1);
  await page.getByRole('button', { name: 'Check preparation request' }).dblclick();
  await expect(page.getByText('Security checks', { exact: true })).toBeVisible();
  expect(api.submissions).toHaveLength(2);
  expect(api.submissions[0]).toEqual(api.submissions[1]);
  expect(api.submissions[0]).toEqual({ profile_id: PROFILE.profile_id, version: PROFILE.version, manifest_sha256: PROFILE.manifest_sha256, request_id: expect.stringMatching(/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/) });
});

test('GET polling only shows ready after verified complete evidence and survives refresh', async ({ page }) => {
  const api = await setupApi(page);
  await page.goto(SETUP_URL);
  await page.getByRole('button', { name: 'Prepare environment' }).click();
  await expect(page.getByText('Security checks', { exact: true })).toBeVisible();
  api.setJob(job({ state: 'ready', stage: 'complete', evidence_verified: true }));
  await expect(page.getByText('Environment ready', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText('Environment ready', { exact: true })).toBeVisible();
  expect(api.submissions).toHaveLength(1);
  expect(api.reads).toContain(`/projects/${PROJECT_ID}/preparations/${JOB_ID}`);
  expect(api.reads).not.toContain(`/preparations/${JOB_ID}`);
});

test('a fast checking reply cannot cancel the slower verified sibling preparation', async ({ page }) => {
  await setupApi(page);
  const slowerId = 'abababab-abab-4bab-8bab-abababababab';
  const slower = job({ id: slowerId });
  let verifiedReplies = 0;
  await page.route(`**/api/v1/projects/${PROJECT_ID}/research-setup`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ project_id: PROJECT_ID, requirements: [], profiles: [{ ...PROFILE, state: 'preparing' }], connections: [], preparations: [slower, job()] }) }));
  await page.route(`**/api/v1/projects/${PROJECT_ID}/preparations/${JOB_ID}`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(job()) }));
  await page.route(`**/api/v1/projects/${PROJECT_ID}/preparations/${slowerId}`, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 500));
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ...slower, state: 'ready', stage: 'complete', evidence_verified: true }) }).then(() => { verifiedReplies += 1; }).catch(() => undefined);
  });
  await page.goto(SETUP_URL);
  await expect(page.getByText('Security checks', { exact: true })).toBeVisible();
  await expect(page.getByText('Environment ready', { exact: true })).toBeVisible();
  expect(verifiedReplies).toBe(1);
});

for (const malformed of ['not-a-digest', 'missing digest', 'stale revision'] as const) {
  test(`${malformed} plan/readiness cannot authorize approval`, async ({ page }) => {
    const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ run_id: NEW_RUN_ID, revision: malformed === 'stale revision' ? 3 : 1, state: 'awaiting_approval', artifacts: [] }) });
    const plan: Record<string, unknown> = { ...makePlan() };
    const readiness: Record<string, unknown> = { run_id: NEW_RUN_ID, revision: 1, plan_digest: makePlan().plan_digest, state: 'ready', requirements: [] };
    if (malformed === 'missing digest') { delete plan.plan_digest; delete readiness.plan_digest; }
    if (malformed === 'not-a-digest') { plan.plan_digest = 'not-a-digest'; readiness.plan_digest = 'not-a-digest'; }
    await page.route(`**/api/v1/runs/${NEW_RUN_ID}/plan`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(plan) }));
    await page.route(`**/api/v1/runs/${NEW_RUN_ID}/readiness`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(readiness) }));
    await page.goto(SESSION_URL);
    await expect(page.getByRole('alert')).toContainText('invalid response');
    await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toHaveCount(0);
    expect(fixture.writes.filter((write) => write.path.endsWith('/approve'))).toHaveLength(0);
  });
}

for (const malformed of ['not-a-digest', 'missing digest', 'stale revision'] as const) {
  test(`final ${malformed} read cannot send approval after a valid initial review`, async ({ page }) => {
    const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ run_id: NEW_RUN_ID, revision: 3, state: 'awaiting_approval', artifacts: [] }) });
    let malformedReply = false;
    await page.route(`**/api/v1/runs/${NEW_RUN_ID}/plan`, (route) => {
      const plan: Record<string, unknown> = { ...makePlan(malformedReply && malformed === 'stale revision' ? 1 : 3) };
      if (malformedReply && malformed === 'not-a-digest') plan.plan_digest = 'not-a-digest';
      if (malformedReply && malformed === 'missing digest') delete plan.plan_digest;
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(plan) });
    });
    await page.route(`**/api/v1/runs/${NEW_RUN_ID}/readiness`, (route) => {
      const revision = malformedReply && malformed === 'stale revision' ? 1 : 3;
      const readiness: Record<string, unknown> = { run_id: NEW_RUN_ID, revision, plan_digest: makePlan(revision).plan_digest, state: 'ready', requirements: [] };
      if (malformedReply && malformed === 'not-a-digest') readiness.plan_digest = 'not-a-digest';
      if (malformedReply && malformed === 'missing digest') delete readiness.plan_digest;
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(readiness) });
    });
    await page.goto(SESSION_URL);
    await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toBeEnabled();
    malformedReply = true;
    await page.getByRole('button', { name: 'Approve plan', exact: true }).click();
    await expect(page.getByRole('alert')).toContainText('invalid response');
    expect(fixture.writes.filter((write) => write.path.endsWith('/approve'))).toHaveLength(0);
  });
}

test('missing, non-finite and negative plan limits fail closed', async ({ page }) => {
  const fixture = await installResearchFixtureRoutes(page, { run: makeRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [] }) });
  for (const limits of [{ token_limit: undefined }, { token_limit: NaN }, { token_limit: -1 }, { elapsed_limit_ms: -1 }, { elapsed_limit_ms: 1.5 }]) {
    await page.route(`**/api/v1/runs/${NEW_RUN_ID}/plan`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ...makePlan(), plan: { ...makePlan().plan, ...limits } }) }));
    await page.goto(SESSION_URL);
    await expect(page.getByRole('alert')).toContainText('invalid response');
    await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toHaveCount(0);
  }
  expect(fixture.writes.filter((write) => write.path.endsWith('/approve'))).toHaveLength(0);
});

test('setup return selects the requested run instead of a newer run in the same session', async ({ page }) => {
  await setupApi(page);
  const other = '77777777-7777-4777-8777-777777777777';
  await page.route(`**/api/v1/projects/${PROJECT_ID}/runs`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify([makeRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [] }), makeRun({ run_id: other, state: 'awaiting_approval', artifacts: [] })]) }));
  await page.goto(SETUP_URL);
  await page.getByRole('link', { name: 'Return to plan', exact: true }).click();
  await expect(page).toHaveURL(`${SESSION_URL}?run=${NEW_RUN_ID}`);
  await expect(page.getByRole('link', { name: 'Research Setup', exact: true })).toHaveAttribute('href', SETUP_URL);
});

for (const scope of ['foreign project', 'foreign session', 'absent', 'invalid'] as const) {
  test(`requested ${scope} run is unavailable without falling back to another run`, async ({ page }) => {
    await installResearchFixtureRoutes(page);
    const other = '77777777-7777-4777-8777-777777777777';
    const requested = scope === 'invalid' ? 'not-a-run' : NEW_RUN_ID;
    const candidate = makeRun({ run_id: NEW_RUN_ID, state: 'awaiting_approval', artifacts: [], ...(scope === 'foreign project' ? { project_id: other } : scope === 'foreign session' ? { session_id: other } : {}) });
    await page.route(`**/api/v1/projects/${PROJECT_ID}/runs`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify([makeRun({ run_id: other, state: 'awaiting_approval', artifacts: [] }), ...(scope === 'absent' ? [] : [candidate])]) }));
    await page.goto(`${SESSION_URL}?run=${requested}`);
    await expect(page.getByRole('alert')).toContainText('requested run is unavailable');
    await expect(page.getByRole('button', { name: 'Approve plan', exact: true })).toHaveCount(0);
  });
}

test('return restores the same work and rejects stale readiness before current plan approval', async ({ page }) => {
  const api = await setupApi(page);
  await page.goto(SESSION_URL);
  await expect(page.getByRole('button', { name: 'Approve plan' })).toBeDisabled();
  await page.getByRole('link', { name: 'Research Setup', exact: true }).click();
  await expect(page).toHaveURL(SETUP_URL);
  api.changeRevision(2);
  api.setReadiness('ready');
  api.staleReadiness(true);
  await page.getByRole('link', { name: 'Return to plan' }).click();
  await expect(page).toHaveURL(`${SESSION_URL}?run=${NEW_RUN_ID}`);
  await expect(page.getByRole('button', { name: 'Approve plan' })).toBeDisabled();
  api.staleReadiness(false);
  await page.getByRole('button', { name: 'Refresh readiness' }).click();
  await expect(page.getByRole('button', { name: 'Approve plan' })).toBeEnabled();
  await page.getByRole('button', { name: 'Approve plan' }).click();
  await expect.poll(() => api.fixture.writes.find((write) => write.path.endsWith('/approve'))?.body).toEqual({ expected_revision: 2, plan_digest: makePlan(2).plan_digest });
});

test('readiness service failure blocks approval with a retry action', async ({ page }) => {
  const api = await setupApi(page);
  api.setReadiness('ready');
  api.failReadiness();
  await page.goto(SESSION_URL);
  await expect(page.getByText('Secure storage is unavailable.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Approve plan' })).toBeDisabled();
  expect(api.fixture.writes.filter((write) => write.path.endsWith('/approve'))).toHaveLength(0);
});

test('fetched table values replace the illustrative plot and remain in expanded view', async ({ page }) => {
  await installResearchFixtureRoutes(page);
  await page.route(`**/api/v1/artifacts/${PLOT_ID}/content`, (route) => route.fulfill({ contentType: 'text/csv', body: 'sample,measured concentration\ncontrol,8.25\ntreated,3.75\n' }));
  await page.goto(SESSION_URL);
  await expect(page.getByRole('cell', { name: '8.25', exact: true })).toBeVisible();
  await expect(page.getByLabel('Diffusion', { exact: true })).toHaveCount(0);
  const opener = page.getByRole('button', { name: 'Expand visual: Concentration profile' });
  await opener.click();
  await expect(page.getByRole('dialog').getByRole('cell', { name: '3.75' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(opener).toBeFocused();
});

test('fetched image uses actual bytes without executing unsupported HTML', async ({ page }) => {
  await installResearchFixtureRoutes(page);
  await page.route(`**/api/v1/artifacts/${PLOT_ID}/content`, (route) => route.fulfill({ contentType: 'image/png', body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/gYQAAAAASUVORK5CYII=', 'base64') }));
  await page.goto(SESSION_URL);
  const image = page.getByRole('img', { name: 'Concentration profile', exact: true });
  await expect(image).toHaveAttribute('src', /^blob:/);
  await expect.poll(() => image.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(1);
  await page.route(`**/api/v1/artifacts/${PLOT_ID}/content`, (route) => route.fulfill({ contentType: 'text/html', body: '<script>window.__artifactExecuted=true</script><h1>untrusted html</h1>' }));
  await page.reload();
  await expect(page.getByText('Preview unavailable for this file type.')).toBeVisible();
  expect(await page.evaluate(() => '__artifactExecuted' in window)).toBe(false);
});

test('late setup replies cannot cross project navigation and no GET starts a job', async ({ page }) => {
  const api = await setupApi(page);
  let release!: () => void;
  const waiting = new Promise<void>((resolve) => { release = resolve; });
  const other = '77777777-7777-4777-8777-777777777777';
  let entered = false;
  await page.route(`**/api/v1/projects/${PROJECT_ID}/research-setup`, async (route) => {
    entered = true;
    await waiting;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ project_id: PROJECT_ID, requirements: [], profiles: [{ ...PROFILE, label: 'Obsolete setup' }], connections: [], preparations: [] }) }).catch(() => undefined);
  });
  await page.route(`**/api/v1/projects/${other}/research-setup`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ project_id: other, requirements: [], profiles: [{ ...PROFILE, label: 'Current project setup' }], connections: [], preparations: [] }) }));
  await page.goto(SETUP_URL);
  await expect.poll(() => entered).toBe(true);
  await page.evaluate((target) => { history.pushState({}, '', target); dispatchEvent(new PopStateEvent('popstate')); }, `/projects/${other}/research-setup`);
  await expect(page.getByText('Current project setup', { exact: true })).toBeVisible();
  release();
  await expect(page.getByText('Obsolete setup', { exact: true })).toHaveCount(0);
  expect(api.submissions).toHaveLength(0);
});

test('foreign preparation status fails closed during polling', async ({ page }) => {
  const api = await setupApi(page);
  await page.goto(SETUP_URL);
  await page.getByRole('button', { name: 'Prepare environment' }).click();
  await expect(page.getByText('Security checks', { exact: true })).toBeVisible();
  api.setJob(job({ project_id: '77777777-7777-4777-8777-777777777777', state: 'ready', stage: 'complete', evidence_verified: true }));
  await expect(page.getByRole('alert')).toContainText('invalid response');
  await expect(page.getByText('Environment ready', { exact: true })).toHaveCount(0);
  expect(api.submissions).toHaveLength(1);
});

test('a plan changing during approval is refreshed for review without sending approval', async ({ page }) => {
  const api = await setupApi(page);
  api.setReadiness('ready');
  await page.goto(SESSION_URL);
  await expect(page.getByRole('button', { name: 'Approve plan' })).toBeEnabled();
  api.changeRevision(2);
  await page.getByRole('button', { name: 'Approve plan' }).click();
  await expect(page.getByText('The plan changed. Review the refreshed plan before approving.')).toBeVisible();
  expect(api.fixture.writes.filter((write) => write.path.endsWith('/approve'))).toHaveLength(0);
});

test('large bodies and malformed JSON fail without a fabricated preview', async ({ page }) => {
  await installResearchFixtureRoutes(page);
  await page.route(`**/api/v1/artifacts/${PLOT_ID}/content`, (route) => route.fulfill({ contentType: 'application/json', body: '{broken content' }));
  await page.goto(SESSION_URL);
  await expect(page.getByRole('article', { name: 'Concentration profile' }).getByRole('alert')).toBeVisible();
  await expect(page.getByRole('article', { name: 'Concentration profile' }).getByRole('img')).toHaveCount(0);
  await page.route(`**/api/v1/artifacts/${PLOT_ID}/content`, (route) => route.fulfill({ contentType: 'text/plain', body: 'x'.repeat(8 * 1024 * 1024 + 1) }));
  await page.reload();
  await expect(page.getByRole('article', { name: 'Concentration profile' }).getByRole('alert')).toContainText('exceeds the size limit');
  await expect(page.getByRole('article', { name: 'Concentration profile' }).getByRole('link', { name: 'Download output' })).toBeVisible();
});

test('setup keeps return context through credential forms, language and small screens', async ({ page }) => {
  const api = await setupApi(page);
  await page.setViewportSize({ width: 320, height: 700 });
  await page.goto(SETUP_URL);
  await page.getByRole('link', { name: 'Configure connections and access' }).click();
  await expect(page.getByLabel('API key')).toHaveAttribute('type', 'password');
  await page.getByRole('link', { name: 'Return to Research Setup' }).click();
  await expect(page).toHaveURL(SETUP_URL);
  await page.getByRole('button', { name: 'TH', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'ความพร้อมของงาน', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'กลับไปตรวจทานแผน' })).toHaveAttribute('href', `${SESSION_URL}?run=${NEW_RUN_ID}`);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole('link', { name: 'กลับไปตรวจทานแผน' }).click();
  await page.getByText('แก้ไขขั้นตอนและขีดจำกัด', { exact: true }).click();
  await expect(page.getByLabel('ขีดจำกัดโทเคน')).toBeVisible();
  await page.getByText('คำถามถัดไป', { exact: true }).click();
  await page.getByText('ตัวเลือกเวิร์กโฟลว์', { exact: true }).click();
  await page.getByLabel('รูปแบบงานวิจัย').selectOption('resources');
  await expect(page.getByLabel('รูปแบบงานวิจัย')).toHaveValue('resources');
  await expect(page.getByText('ขอบเขตหรือรูปแบบงานวิจัยเปลี่ยนแล้ว โปรดเตรียมแผนอีกครั้งก่อนอนุมัติ')).toBeVisible();
  const prepareButton = page.getByRole('button', { name: 'เตรียมแผน', exact: true });
  await expect(prepareButton).toBeEnabled();
  await expect(page.getByRole('button', { name: 'อนุมัติแผน', exact: true })).toBeDisabled();
  expect(api.fixture.writes.filter((write) => write.path.endsWith('/prepare-plan'))).toHaveLength(0);
  await prepareButton.click();
  await expect.poll(() => api.fixture.writes.filter((write) => write.path.endsWith('/prepare-plan')).length).toBe(1);
  expect(api.fixture.writes.find((write) => write.path.endsWith('/prepare-plan'))!.body).toEqual({ expected_revision: 1, workflow: 'resources', search_terms: [] });
  expect(api.fixture.writes.filter((write) => write.path.endsWith('/approve'))).toHaveLength(0);
  expect(api.submissions).toHaveLength(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('browser Back closes an expanded output and Forward restores the fetched preview', async ({ page }) => {
  await installResearchFixtureRoutes(page);
  await page.goto(SESSION_URL);
  await page.getByLabel('Research question').fill('Keep this draft while reviewing the output');
  await page.getByRole('button', { name: 'Expand visual: Concentration profile' }).click();
  await expect(page.getByRole('dialog').getByRole('cell', { name: '8.25' })).toBeVisible();
  await page.goBack();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByLabel('Research question')).toHaveValue('Keep this draft while reviewing the output');
  await page.goForward();
  await expect(page.getByRole('dialog').getByRole('cell', { name: '8.25' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: 'Expand visual: Concentration profile' })).toBeFocused();
});
