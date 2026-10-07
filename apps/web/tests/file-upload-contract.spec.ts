import { expect, test } from '@playwright/test';
import { PROJECT_ID, installProjectFixtureRoutes } from './fixtures/project';

test('library sends filename and exact CSV bytes to the raw upload API', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  const csv = Buffer.from('x,y\n1,2\n3,\n5,6\n');
  await page.goto(`/projects/${PROJECT_ID}/library#outputs-files`);
  await expect(page.getByText('Accepted formats: CSV, JSON, Markdown, PDF, Plain text, XLSX · Maximum size: 25 MiB')).toBeVisible();
  await expect(page.locator('#library-upload')).toHaveAttribute('accept', '.csv,.json,.md,.pdf,.txt,.xlsx');
  await expect(page.locator('#library-upload')).toBeEnabled();
  const request = page.waitForRequest((r) => r.method() === 'POST' && new URL(r.url()).pathname.endsWith('/files'));
  await page.locator('#library-upload').setInputFiles({ name: 'partial.csv', mimeType: 'text/csv', buffer: csv });
  const upload = await request;
  expect(new URL(upload.url()).searchParams.get('filename')).toBe('partial.csv');
  expect(upload.headers()['content-type']).toBe('text/csv');
  expect(upload.postDataBuffer()).toEqual(csv);
});

test('library reports malformed upload capabilities without displaying NaN', async ({ page }) => {
  await installProjectFixtureRoutes(page);
  await page.route('**/api/v1/capabilities', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ file_types: ['.csv'], max_upload_bytes: 'invalid', protocols: {} }),
  }));
  await page.goto(`/projects/${PROJECT_ID}/library#outputs-files`);
  await expect(page.getByRole('alert')).toContainText('The server returned an invalid upload policy.');
  await expect(page.getByText(/NaN/)).toHaveCount(0);
});
