import { expect, test } from '@playwright/test';
import { installResearchFixtureRoutes, PROJECT_ID } from './fixtures/research';

test('library sends filename and exact CSV bytes to the raw upload API', async ({ page }) => {
  await installResearchFixtureRoutes(page, { run: null });
  const csv = Buffer.from('x,y\n1,2\n3,\n5,6\n');
  await page.goto(`/projects/${PROJECT_ID}/library`);
  const request = page.waitForRequest((r) => r.method() === 'POST' && new URL(r.url()).pathname.endsWith('/files'));
  await page.locator('#library-upload').setInputFiles({ name: 'partial.csv', mimeType: 'text/csv', buffer: csv });
  const upload = await request;
  expect(new URL(upload.url()).searchParams.get('filename')).toBe('partial.csv');
  expect(upload.headers()['content-type']).toBe('text/csv');
  expect(upload.postDataBuffer()).toEqual(csv);
});
