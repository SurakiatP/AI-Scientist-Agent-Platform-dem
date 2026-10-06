import { chmod, mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { expect, test } from '@playwright/test';

const sessionFile = process.env.SCIENTIFIC_W2_STOP_OWNER_SESSION_FILE;
const proofFile = process.env.SCIENTIFIC_W2_STOP_PROOF;
const evidenceFile = process.env.SCIENTIFIC_W2_STOP_BROWSER_EVIDENCE;
const originValue = process.env.SCIENTIFIC_W2_STOP_API_ORIGIN;
const readyFile = process.env.SCIENTIFIC_W2_STOP_STALL_READY;
const enabled = Boolean(sessionFile && proofFile && evidenceFile && originValue && readyFile);

test('W2 stop preserves unknown compute outcome', async ({ page }) => {
  test.skip(!enabled, 'NOT RUN: dedicated W2 no-Crossref stop host and proof paths required');
  test.setTimeout(600_000);

  const proof = JSON.parse(await readFile(proofFile!, 'utf8')) as {
    project_id: string;
    session_id: string;
    run_id: string;
    workflow: string;
    crossref_requests_expected: number;
  };
  if (proof.workflow !== 'resources' || proof.crossref_requests_expected !== 0) {
    throw new Error('W2 stop proof is not the isolated no-Crossref workflow');
  }

  const origin = new URL(originValue!).origin;
  const cookies = JSON.parse(await readFile(sessionFile!, 'utf8'));
  if (!Array.isArray(cookies) || cookies.length !== 1 || cookies[0].name !== 'owner_session'
      || cookies[0].url !== origin || typeof cookies[0].value !== 'string' || !cookies[0].value
      || cookies[0].httpOnly !== true || cookies[0].sameSite !== 'Strict')
    throw new Error('W2 stop owner session does not match actual host');
  await page.context().addCookies(cookies);
  await page.goto(new URL(
    `/projects/${encodeURIComponent(proof.project_id)}/sessions/${encodeURIComponent(proof.session_id)}?run=${encodeURIComponent(proof.run_id)}`,
    origin,
  ).toString());
  await expect(page.getByRole('heading', { name: 'Research chat' })).toBeVisible({ timeout: 30_000 });
  const plan = page.getByRole('region', { name: 'Plan review' });
  await expect(plan).toBeVisible({ timeout: 60_000 });
  await expect(plan.getByRole('button', { name: 'Approve plan', exact: true })).toBeEnabled({ timeout: 60_000 });

  let stopRequests = 0;
  let decisionWrites = 0;
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (request.method() === 'POST' && url.pathname === `/api/v1/runs/${proof.run_id}/stop`) stopRequests += 1;
    if (request.method() === 'POST' && url.pathname === `/api/v1/runs/${proof.run_id}/decisions`) decisionWrites += 1;
  });
  await plan.getByRole('button', { name: 'Approve plan', exact: true }).click();

  await expect.poll(async () => page.evaluate(async (runId) => {
    const response = await fetch(`/api/v1/runs/${encodeURIComponent(runId)}`, { credentials: 'same-origin' });
    if (!response.ok) return false;
    const run = await response.json() as { state: string };
    return run.state === 'running' || run.state === 'stopping' || run.state === 'waiting_input';
  }, proof.run_id), { timeout: 60_000 }).toBe(true);

  await expect.poll(async () => {
    try {
      await readFile(readyFile!, 'utf8');
      return true;
    } catch {
      return false;
    }
  }, { timeout: 180_000, intervals: [250, 500, 1_000] }).toBe(true);

  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible({ timeout: 60_000 });
  await page.getByRole('button', { name: 'Stop', exact: true }).click();

  await expect.poll(async () => page.evaluate(async (runId) => {
    const [runResponse, pendingResponse] = await Promise.all([
      fetch(`/api/v1/runs/${encodeURIComponent(runId)}`, { credentials: 'same-origin' }),
      fetch(`/api/v1/runs/${encodeURIComponent(runId)}/pending-decisions`, { credentials: 'same-origin' }),
    ]);
    if (!runResponse.ok || !pendingResponse.ok) return null;
    const run = await runResponse.json() as { state: string; waiting_reason: string | null };
    const pending = await pendingResponse.json() as Array<{ reason: string; operation_reserved_tokens: number | null }>;
    if (!Array.isArray(pending)) return null;
    const stopped = run.state === 'canceled' ||
      (run.state === 'waiting_input' && run.waiting_reason === 'unknown_outcome');
    return stopped && pending.length === 1 && pending[0].reason === 'unknown_outcome' &&
      typeof pending[0].operation_reserved_tokens === 'number' && pending[0].operation_reserved_tokens > 0;
  }, proof.run_id), { timeout: 120_000, intervals: [500, 1_000, 2_000] }).toBe(true);
  expect(stopRequests).toBe(1);

  const snapshot = await page.evaluate(async (runId) => {
    const [runResponse, pendingResponse] = await Promise.all([
      fetch(`/api/v1/runs/${encodeURIComponent(runId)}`, { credentials: 'same-origin' }),
      fetch(`/api/v1/runs/${encodeURIComponent(runId)}/pending-decisions`, { credentials: 'same-origin' }),
    ]);
    if (!runResponse.ok || !pendingResponse.ok) throw new Error('W2 stop state readback failed');
    const run = await runResponse.json() as { state: string; waiting_reason: string | null };
    const pending = await pendingResponse.json() as Array<{ reason: string; operation_reserved_tokens: number | null }>;
    if (!Array.isArray(pending) || pending.length !== 1 || pending[0].reason !== 'unknown_outcome' ||
        typeof pending[0].operation_reserved_tokens !== 'number') {
      throw new Error('W2 pending unknown-decision response shape changed');
    }
    return { run_state: run.state, waiting_reason: run.waiting_reason, pending_count: pending.length,
      pending_reason: pending[0].reason, operation_reserved_tokens: pending[0].operation_reserved_tokens };
  }, proof.run_id);
  if (snapshot.pending_count !== 1 || snapshot.pending_reason !== 'unknown_outcome' ||
      snapshot.operation_reserved_tokens <= 0 ||
      (snapshot.run_state === 'waiting_input' && snapshot.waiting_reason !== 'unknown_outcome')) {
    throw new Error('W2 stop did not preserve an unresolved owner decision');
  }
  if (stopRequests !== 1 || decisionWrites !== 0) {
    throw new Error('W2 browser sent duplicate stop or resolved the unknown decision');
  }
  if (new URL(page.url()).origin !== origin) throw new Error('W2 stop browser left the real host origin');

  await mkdir(path.dirname(evidenceFile!), { recursive: true, mode: 0o700 });
  await writeFile(evidenceFile!, JSON.stringify({
    status: 'PASS', run_id: proof.run_id, project_id: proof.project_id, session_id: proof.session_id,
    run_state: snapshot.run_state, waiting_reason: snapshot.waiting_reason,
    pending_decisions: snapshot.pending_count, pending_reason: snapshot.pending_reason,
    operation_reserved_tokens: snapshot.operation_reserved_tokens, stop_requests: stopRequests,
    decision_writes: decisionWrites, crossref_requests: 0,
    authenticated_owner_session_transferred: true, actual_host_origin: origin,
  }) + '\n', { mode: 0o600 });
  await chmod(evidenceFile!, 0o600);
});
