import type { Page } from '@playwright/test';
import type { CitationView, FileView, FindingView, ProjectView, RunView, SessionView } from '../../../../contracts/api-types';

export const PROJECT_ID = '11111111-1111-4111-8111-111111111111';
export const OTHER_PROJECT_ID = '33333333-3333-4333-8333-333333333333';
export const SESSION_ID = '22222222-2222-4222-8222-222222222222';
export const OTHER_SESSION_ID = '44444444-4444-4444-8444-444444444444';
export const FILE_ID = '55555555-5555-4555-8555-555555555555';
export const REPORT_ID = '66666666-6666-4666-8666-666666666666';
export const FINDING_ID = '77777777-7777-4777-8777-777777777777';
export const CITATION_ID = '88888888-8888-4888-8888-888888888888';

export const projects: ProjectView[] = [
  { id: PROJECT_ID, name: 'Diffusion study', revision: 1, instructions: 'Compare primary sources.' },
  { id: OTHER_PROJECT_ID, name: 'Cell migration', revision: 1, instructions: '' },
];
export const sessions: SessionView[] = [
  { id: SESSION_ID, project_id: PROJECT_ID, title: 'Membrane transport' },
  { id: OTHER_SESSION_ID, project_id: PROJECT_ID, title: 'Temperature effects' },
];
export const files: FileView[] = [
  { id: FILE_ID, project_id: PROJECT_ID, filename: 'diffusion-notes.pdf', size: 2048, content_type: 'application/pdf', state: 'ready' },
  { id: '99999999-9999-4999-8999-999999999999', project_id: PROJECT_ID, filename: 'pending.csv', size: 128, content_type: 'text/csv', state: 'preparing' },
];
export const citations: CitationView[] = [{
  id: CITATION_ID, title: 'Example report', authors: ['A. Researcher'], year: 2024,
  identifier: 'doi:10.0000/example', access: 'abstract', verification: 'unverified',
}];
export const findings: FindingView[] = [{
  id: FINDING_ID, project_id: PROJECT_ID, session_id: SESSION_ID, artifact_id: REPORT_ID,
  text: 'Example finding', citation_ids: [CITATION_ID],
}];
export const runs: RunView[] = [{
  run_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', project_id: PROJECT_ID, session_id: SESSION_ID,
  revision: 3, state: 'failed', error_code: 'storage_unavailable', latest_cursor: 2,
  usage_tokens: 20, reserved_tokens: 0, planning_tokens: 4, token_limit: 100,
  artifacts: [{ artifact_id: REPORT_ID, project_id: PROJECT_ID, run_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', title: 'Example report', kind: 'report', sha256: 'a'.repeat(64), size: 900, content_type: 'text/markdown', partial: true }],
}];

export async function installProjectFixtureRoutes(page: Page, options: { preparationOutcome?: 'ready' | 'failed'; rotateCsrfOnFirstFileDelete?: boolean; rotateCsrfOnFirstFindingDelete?: boolean } = {}) {
  const writes: Array<{ method: string; url: string; body?: unknown }> = [];
  const deleted: string[] = [];
  let fileFetchCount = 0;
  let csrfFetchCount = 0;
  let fileDeleteCount = 0;
  let findingDeleteCount = 0;
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace('/api/v1', '');
    const method = request.method();
    if (method !== 'GET') {
      let body: unknown;
      try { body = request.postDataJSON(); } catch { body = undefined; }
      writes.push({ method, url: path, body });
    }
    const json = (data: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    if (method === 'GET' && path === '/owner/session') { csrfFetchCount += 1; return json({ identity: 'fixture-owner', kind: 'owner', csrf_token: `fixture-csrf-token-${csrfFetchCount}` }); }
    if (method === 'GET' && path === '/capabilities') return json({ file_types: ['.csv', '.json', '.md', '.pdf', '.txt', '.xlsx'], max_upload_bytes: 25 * 1024 * 1024, protocols: { mcp: 'not_configured', a2a: 'not_configured' } });
    if (method === 'GET' && path === '/projects') return json(projects);
    if (method === 'POST' && path === '/projects') return json(projects[0], 201);
    if (method === 'GET' && /^\/projects\/[0-9a-f-]+$/.test(path)) {
      const id = path.split('/')[2];
      const project = projects.find((item) => item.id === id);
      return project ? json(project) : json({ code: 'not_found', message: 'Not found', request_id: 'fixture' }, 404);
    }
    if (method === 'PATCH' && path === `/projects/${PROJECT_ID}`) return json({ ...projects[0], ...(request.postDataJSON() as object), revision: 2 });
    if (method === 'GET' && path === `/projects/${PROJECT_ID}/sessions`) return json(sessions);
    if (method === 'GET' && path === `/projects/${OTHER_PROJECT_ID}/sessions`) return json([]);
    if (method === 'POST' && path === `/projects/${PROJECT_ID}/sessions`) return json({ id: OTHER_SESSION_ID, project_id: PROJECT_ID, title: 'New session' }, 201);
    if (method === 'GET' && path === `/projects/${PROJECT_ID}/files`) {
      fileFetchCount += 1;
      const prepared = fileFetchCount > 3 && options.preparationOutcome !== undefined;
      return json(files.map((file) => file.id === files[1].id && prepared ? { ...file, state: options.preparationOutcome!, error_code: options.preparationOutcome === 'failed' ? 'preparation_failed' : null } : file));
    }
    if (method === 'GET' && path === `/projects/${OTHER_PROJECT_ID}/files`) return json([]);
    if (method === 'POST' && path === `/projects/${PROJECT_ID}/files`) return json(files[0], 201);
    if (method === 'DELETE' && path === `/projects/${PROJECT_ID}/files/${FILE_ID}`) {
      fileDeleteCount += 1;
      if (options.rotateCsrfOnFirstFileDelete && fileDeleteCount === 1) return json({ code: 'forbidden', message: 'Session changed.', request_id: 'fixture-csrf' }, 403);
      deleted.push(path); return json({});
    }
    if (method === 'GET' && path === `/projects/${PROJECT_ID}/findings`) return json(findings);
    if (method === 'GET' && path === `/projects/${OTHER_PROJECT_ID}/findings`) return json([]);
    if (method === 'DELETE' && path === `/projects/${PROJECT_ID}/findings/${FINDING_ID}`) {
      findingDeleteCount += 1;
      if (options.rotateCsrfOnFirstFindingDelete && findingDeleteCount === 1) return json({ code: 'forbidden', message: 'Session changed.', request_id: 'fixture-csrf' }, 403);
      deleted.push(path); return json({});
    }
    if (method === 'GET' && path === `/projects/${PROJECT_ID}/sources`) return json(citations);
    if (method === 'GET' && path === `/projects/${OTHER_PROJECT_ID}/sources`) return json([]);
    if (method === 'GET' && path === `/projects/${PROJECT_ID}/runs`) return json(runs);
    if (method === 'GET' && path === `/projects/${OTHER_PROJECT_ID}/runs`) return json([]);
    if (method === 'GET' && path === `/projects/${PROJECT_ID}/sessions/${SESSION_ID}/messages`) return json({ session_id: SESSION_ID, messages: [] });
    return json({ code: 'not_found', message: 'Not found', request_id: 'fixture' }, 404);
  });
  return { writes, deleted, getCsrfFetchCount: () => csrfFetchCount, getFileDeleteCount: () => fileDeleteCount, getFindingDeleteCount: () => findingDeleteCount };
}
