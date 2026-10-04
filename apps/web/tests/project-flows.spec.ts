import { expect, test } from '@playwright/test';
import { FINDING_ID, FILE_ID, OTHER_PROJECT_ID, PROJECT_ID, SESSION_ID, installProjectFixtureRoutes } from './fixtures/project';

test('projects create and open a project-scoped workspace; browser back and forward restore routes', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.goto('/projects');
  await expect(page.getByRole('heading', { name: 'Projects', level: 1 })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Diffusion study' })).toBeVisible();
  await page.getByRole('link', { name: 'Diffusion study' }).click();
  await expect(page).toHaveURL(new RegExp(`/projects/${PROJECT_ID}$`));
  await expect(page.getByRole('link', { name: 'Membrane transport' }).first()).toBeVisible();
  await page.locator(`a[href="/projects/${PROJECT_ID}/library"]`).click();
  await expect(page.getByText('diffusion-notes.pdf')).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`/projects/${PROJECT_ID}$`));
  await page.goForward();
  await expect(page.getByText('diffusion-notes.pdf')).toBeVisible();
});

test('creating a project trims its name and opens the returned project', async ({ page }) => {
  const fixture = await installProjectFixtureRoutes(page);
  await page.goto('/projects');
  await page.getByLabel('New project name').fill('  New science space  ');
  await page.getByRole('button', { name: 'Create project' }).click();
  await expect(page).toHaveURL(new RegExp(`/projects/${PROJECT_ID}$`));
  expect(fixture.writes.find((item) => item.method === 'POST' && item.url === '/projects')?.body).toEqual({ name: 'New science space' });
});

test('removing a saved finding leaves its report and source intact', async ({ page }) => {
  const fixture = await installProjectFixtureRoutes(page);
  await page.goto(`/projects/${PROJECT_ID}`);
  await page.getByRole('button', { name: 'Remove finding: Example finding' }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('button', { name: 'Confirm removal' }).click();
  await expect(page.getByText('Example finding', { exact: true })).toHaveCount(0);
  await page.locator(`a[href="/projects/${PROJECT_ID}/library"]`).click();
  await expect(page.getByText('Example report', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('diffusion-notes.pdf')).toBeVisible();
  expect(fixture.deleted).toEqual([`/projects/${PROJECT_ID}/findings/${FINDING_ID}`]);
});

test('finding removal CSRF failure and manual recovery stay inside the open confirmation dialog', async ({ page }) => {
  const fixture = await installProjectFixtureRoutes(page, { rotateCsrfOnFirstFindingDelete: true });
  await page.goto(`/projects/${PROJECT_ID}`);
  await page.getByRole('button', { name: 'Remove finding: Example finding' }).click();
  const dialog = page.getByRole('dialog', { name: 'Remove saved finding?' });
  await dialog.getByRole('button', { name: 'Confirm removal' }).click();
  await expect(dialog.getByRole('alert')).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Refresh session' })).toBeVisible();
  expect(fixture.getFindingDeleteCount()).toBe(1);
  await dialog.getByRole('button', { name: 'Refresh session' }).click();
  await expect(dialog.getByRole('button', { name: 'Retry removal' })).toBeVisible();
  expect(fixture.getFindingDeleteCount()).toBe(1);
  await dialog.getByRole('button', { name: 'Retry removal' }).click();
  await expect(page.getByText('Example finding', { exact: true })).toHaveCount(0);
  expect(fixture.getFindingDeleteCount()).toBe(2);
});

test('removing a shared file keeps its finding, report, and citation provenance', async ({ page }) => {
  const fixture = await installProjectFixtureRoutes(page);
  await page.goto(`/projects/${PROJECT_ID}/library`);
  await page.getByRole('button', { name: 'Remove shared file' }).first().click();
  await expect(page.getByRole('dialog', { name: 'Remove shared file?' })).toBeVisible();
  await page.getByRole('button', { name: 'Confirm removal' }).click();
  await expect(page.getByText('diffusion-notes.pdf')).toHaveCount(0);
  expect(fixture.deleted).toContain(`/projects/${PROJECT_ID}/files/${FILE_ID}`);
  await expect(page.getByText('Example report').first()).toBeVisible();
  await page.goto(`/projects/${PROJECT_ID}`);
  await expect(page.getByText('Example finding')).toBeVisible();
});

test('shared file removal can be retried after explicit CSRF refresh without replaying the first delete', async ({ page }) => {
  const fixture = await installProjectFixtureRoutes(page, { rotateCsrfOnFirstFileDelete: true });
  await page.goto(`/projects/${PROJECT_ID}/library`);
  await page.getByRole('button', { name: 'Remove shared file' }).first().click();
  await page.getByRole('button', { name: 'Confirm removal' }).click();
  await expect(page.getByText('Your owner session may have changed.').first()).toBeVisible();
  expect(fixture.getFileDeleteCount()).toBe(1);
  await page.getByRole('dialog').getByRole('button', { name: 'Refresh session' }).click();
  await expect(page.getByText('diffusion-notes.pdf')).toBeVisible();
  expect(fixture.getFileDeleteCount()).toBe(1);
  await page.getByRole('button', { name: 'Retry removal' }).click();
  await expect(page.getByText('diffusion-notes.pdf')).toHaveCount(0);
  expect(fixture.getFileDeleteCount()).toBe(2);
  expect(fixture.getCsrfFetchCount()).toBe(2);
});

test('project switch does not show the previous project data', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.goto(`/projects/${PROJECT_ID}`);
  await expect(page.getByRole('link', { name: 'Membrane transport' }).first()).toBeVisible();
  await page.goto(`/projects/${OTHER_PROJECT_ID}`);
  await expect(page.getByRole('heading', { name: 'Cell migration' })).toBeVisible();
  await expect(page.getByText('Membrane transport')).toHaveCount(0);
});

test('invalid project links show a not-found state with a return path', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.goto('/projects/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa');
  await expect(page.getByRole('heading', { name: 'Project not found' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Back to projects' })).toHaveAttribute('href', '/projects');
});

test('global sources and history destinations require an explicit project context', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.goto('/sources');
  const sourceProject = page.getByRole('combobox', { name: 'Choose a project' });
  await sourceProject.selectOption(OTHER_PROJECT_ID);
  await expect(page.getByText('diffusion-notes.pdf')).toHaveCount(0);
  await expect(page.getByText('No files have been added to this project.')).toBeVisible();
  await page.goto('/history');
  await page.getByRole('combobox', { name: 'Choose a project' }).selectOption(OTHER_PROJECT_ID);
  await expect(page.getByText('No research runs have been started in this project.')).toBeVisible();
});

test('library exposes accepted formats, server size limit and distinct preparation status', async ({ page }) => {
  await installProjectFixtureRoutes(page, { preparationOutcome: 'ready' });
  await page.goto(`/projects/${PROJECT_ID}/library`);
  await expect(page.getByRole('listitem').filter({ hasText: 'pending.csv' })).toContainText('Preparing');
  await expect(page.getByText(/25 MiB/)).toBeVisible();
  await expect(page.getByText(/PDF|CSV|Markdown/)).toBeVisible();
  const input = page.getByLabel('Upload project files');
  await input.setInputFiles({ name: 'unsafe.exe', mimeType: 'application/x-msdownload', buffer: Buffer.from('x') });
  await expect(page.getByText(/file type is not supported/i)).toBeVisible();
  await expect(page.getByText('diffusion-notes.pdf')).toBeVisible();
  await page.getByRole('button', { name: 'Refresh file status' }).click();
  await expect(page.getByRole('listitem').filter({ hasText: 'pending.csv' })).toContainText('Ready');
});

test('preparation failure is shown as failed and does not claim progress', async ({ page }) => {
  await installProjectFixtureRoutes(page, { preparationOutcome: 'failed' });
  await page.goto(`/projects/${PROJECT_ID}/library`);
  await page.getByRole('button', { name: 'Refresh file status' }).click();
  const pending = page.getByRole('listitem').filter({ hasText: 'pending.csv' });
  await expect(pending).toContainText('Failed');
  await expect(pending).toContainText('File preparation failed');
  await expect(pending).not.toContainText('Preparing');
});

test('empty MIME XLSX uses its extension and unsupported and oversize files remain in the library', async ({ page }) => {
  const fixture = await installProjectFixtureRoutes(page);
  await page.goto(`/projects/${PROJECT_ID}/library`);
  await page.getByLabel('Upload project files').setInputFiles({ name: 'table.xlsx', mimeType: '', buffer: Buffer.from('xlsx') });
  await expect.poll(() => fixture.writes.some((item) => item.method === 'POST' && item.url === `/projects/${PROJECT_ID}/files`)).toBe(true);
  await page.getByLabel('Upload project files').setInputFiles({ name: 'oversize.pdf', mimeType: 'application/pdf', buffer: Buffer.alloc(25 * 1024 * 1024 + 1) });
  await expect(page.getByText(/File exceeds the 25 MiB limit/)).toBeVisible();
  await expect(page.getByText('diffusion-notes.pdf')).toBeVisible();
});

test('delayed preview and upload responses from a prior project are discarded', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.route(`**/api/v1/projects/${PROJECT_ID}/files/${FILE_ID}/content`, async (route) => {
    await page.waitForTimeout(500);
    try { await route.fulfill({ status: 200, contentType: 'application/pdf', body: '%PDF-fixture' }); } catch { /* request was aborted on project switch */ }
  });
  await page.goto('/sources');
  const projectPicker = page.getByRole('combobox', { name: 'Choose a project' });
  await projectPicker.selectOption(PROJECT_ID);
  await page.locator(`#file-${FILE_ID}`).getByRole('button', { name: 'Preview' }).click();
  await projectPicker.selectOption(OTHER_PROJECT_ID);
  await page.waitForTimeout(600);
  await expect(page.getByRole('dialog', { name: /Preview/ })).toHaveCount(0);
  await page.route(`**/api/v1/projects/${PROJECT_ID}/files`, async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    await page.waitForTimeout(500);
    try { await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({ id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', project_id: PROJECT_ID, filename: 'late.txt', size: 7, content_type: 'text/plain', state: 'ready' }) }); } catch { /* request was aborted on project switch */ }
  });
  await projectPicker.selectOption(PROJECT_ID);
  await page.getByLabel('Upload project files').setInputFiles({ name: 'late.txt', mimeType: 'text/plain', buffer: Buffer.from('content') });
  await expect(page.getByText('Sending file…')).toBeVisible();
  await projectPicker.selectOption(OTHER_PROJECT_ID);
  await expect(page.getByText('No files have been added to this project.')).toBeVisible();
  await page.waitForTimeout(600);
  await expect(page.getByText('late.txt')).toHaveCount(0);
});

test('preview Escape and close button close the native dialog and restore its opener focus', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.route(`**/api/v1/projects/${PROJECT_ID}/files/${FILE_ID}/content`, (route) => route.fulfill({ status: 200, contentType: 'application/pdf', body: '%PDF-preview' }));
  await page.goto(`/projects/${PROJECT_ID}/library`);
  const opener = page.locator(`#file-${FILE_ID}`).getByRole('button', { name: 'Preview' });
  await opener.click();
  const dialog = page.getByRole('dialog', { name: /Preview/ });
  await expect(dialog).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();
  await opener.click();
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: 'Close preview' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();
});

test('late instruction save cannot overwrite the next project view', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.route(`**/api/v1/projects/${PROJECT_ID}`, async (route) => {
    if (route.request().method() !== 'PATCH') return route.fallback();
    await page.waitForTimeout(500);
    try { await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...{ id: PROJECT_ID, name: 'Diffusion study', revision: 2, instructions: 'late change' } }) }); } catch { /* request was aborted on project switch */ }
  });
  await page.goto(`/projects/${PROJECT_ID}`);
  await page.getByLabel('Instructions shared with sessions in this project').fill('late change');
  await page.getByRole('button', { name: 'Save instructions' }).click();
  await page.goto(`/projects/${OTHER_PROJECT_ID}`);
  await expect(page.getByRole('heading', { name: 'Cell migration' })).toBeVisible();
  await page.waitForTimeout(600);
  await expect(page.getByLabel('Instructions shared with sessions in this project')).toHaveValue('');
});

test('late session creation cannot navigate away from the next project', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.route(`**/api/v1/projects/${PROJECT_ID}/sessions`, async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    await page.waitForTimeout(500);
    try { await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({ id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', project_id: PROJECT_ID, title: 'Late session' }) }); } catch { /* request was aborted on project switch */ }
  });
  await page.goto(`/projects/${PROJECT_ID}`);
  await page.getByLabel('New session').fill('Late session');
  await page.getByRole('button', { name: 'Create session' }).click();
  await page.goto(`/projects/${OTHER_PROJECT_ID}`);
  await expect(page.getByRole('heading', { name: 'Cell migration' })).toBeVisible();
  await page.waitForTimeout(600);
  await expect(page).toHaveURL(new RegExp(`/projects/${OTHER_PROJECT_ID}$`));
});

test('API, CSRF acquisition, and blob requests reject redirects', async ({ page }) => {
  await page.goto('/projects');
  const externalRequests: string[] = [];
  let sessionRequests = 0;
  await page.route('https://redirect-target.test/**', async (route) => { externalRequests.push(route.request().url()); await route.fulfill({ status: 200, body: '{}' }); });
  await page.route('**/api/v1/owner/session', (route) => {
    sessionRequests += 1;
    return sessionRequests === 1 ? route.fulfill({ status: 302, headers: { location: 'https://redirect-target.test/session' } }) : route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ csrf_token: 'fixture-csrf' }) });
  });
  await page.route('**/api/v1/projects/redirect', (route) => route.fulfill({ status: 302, headers: { location: 'https://redirect-target.test/projects' } }));
  await page.route('**/api/v1/projects', (route) => route.fulfill({ status: 302, headers: { location: 'https://redirect-target.test/projects' } }));
  await page.route('**/api/v1/projects/redirect/files/redirect/content', (route) => route.fulfill({ status: 302, headers: { location: 'https://redirect-target.test/file' } }));
  const outcomes = await page.evaluate(async () => {
    const { request, requestBlob, refreshOwnerSession } = await import('/src/api.ts');
    const attempts = [
      () => request('/api/v1/projects/redirect'),
      () => refreshOwnerSession(),
      () => request('/api/v1/projects', { method: 'POST', body: '{}' }),
      () => requestBlob('/api/v1/projects/redirect/files/redirect/content'),
    ];
    const outcomes: string[] = [];
    for (const attempt of attempts) { try { await attempt(); outcomes.push('resolved'); } catch (error) { outcomes.push(error instanceof TypeError ? 'redirect-rejected' : 'other-error'); } }
    return outcomes;
  });
  expect(outcomes).toEqual(['redirect-rejected', 'redirect-rejected', 'redirect-rejected', 'redirect-rejected']);
  expect(externalRequests).toEqual([]);
});

test('Thai upload error text is localized and removal dialog Escape restores focus', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.goto(`/projects/${PROJECT_ID}/library`);
  await page.getByRole('button', { name: 'TH' }).click();
  await page.getByLabel('อัปโหลดไฟล์โครงการ').setInputFiles({ name: 'unsafe.exe', mimeType: 'application/x-msdownload', buffer: Buffer.from('x') });
  await expect(page.getByText('ไม่รองรับไฟล์ชนิดนี้')).toBeVisible();
  const remove = page.getByRole('button', { name: 'นำไฟล์ที่แชร์ออก' }).first();
  await remove.focus(); await page.keyboard.press('Enter');
  await expect(page.getByRole('dialog', { name: 'นำไฟล์ที่แชร์ออกหรือไม่' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(remove).toBeFocused();
});

test('Thai API failures, citation access, artifact kinds, and run errors use localized labels', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.route(`**/api/v1/projects/${PROJECT_ID}/files/${FILE_ID}/content`, (route) => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ code: 'storage_unavailable', message: 'Secure storage is unavailable.', request_id: 'localized-error' }) }));
  await page.goto(`/projects/${PROJECT_ID}/library`);
  await page.getByRole('button', { name: 'TH' }).click();
  await expect(page.getByText('บทคัดย่อ')).toBeVisible();
  await expect(page.getByText('รายงาน').first()).toBeVisible();
  await page.locator(`#file-${FILE_ID}`).getByRole('button', { name: 'ดูตัวอย่าง' }).click();
  await expect(page.getByText('พื้นที่จัดเก็บที่ปลอดภัยไม่พร้อมใช้งาน')).toBeVisible();
  await expect(page.getByText('Secure storage is unavailable.')).toHaveCount(0);
  await page.goto(`/projects/${PROJECT_ID}/runs`);
  await expect(page.getByText('พื้นที่จัดเก็บที่ปลอดภัยไม่พร้อมใช้งาน')).toBeVisible();
  await expect(page.getByText('รายงาน').first()).toBeVisible();
  await page.goto(`/projects/${PROJECT_ID}`);
  await expect(page.getByText('บทคัดย่อ')).toBeVisible();
});

test('Thai network failures use a localized generic message instead of English caller fallbacks', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.route(`**/api/v1/projects/${PROJECT_ID}/files/${FILE_ID}/content`, (route) => route.abort('failed'));
  await page.goto(`/projects/${PROJECT_ID}/library`);
  await page.getByRole('button', { name: 'TH' }).click();
  await page.locator(`#file-${FILE_ID}`).getByRole('button', { name: 'ดูตัวอย่าง' }).click();
  await expect(page.getByText('เชื่อมต่อบริการไม่สำเร็จ โปรดลองอีกครั้ง')).toBeVisible();
  await expect(page.getByText('Preview unavailable.')).toHaveCount(0);
  await expect(page.getByText('Failed to fetch')).toHaveCount(0);
});

test('history links back to the original session and labels partial output', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.goto(`/projects/${PROJECT_ID}/runs`);
  await expect(page.getByText('Example report')).toBeVisible();
  await expect(page.getByText(/partial/i)).toBeVisible();
  await page.getByRole('link', { name: /Membrane transport/ }).click();
  await expect(page).toHaveURL(new RegExp(`/projects/${PROJECT_ID}/sessions/${SESSION_ID}$`));
});
