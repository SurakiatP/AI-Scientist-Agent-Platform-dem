import { defineConfig, devices } from '@playwright/test';

const apiOrigin = process.env.SCIENTIFIC_W1_API_ORIGIN;
if (apiOrigin) {
  const api = new URL(apiOrigin);
  if (
    api.protocol !== 'http:' ||
    !['127.0.0.1', 'localhost', '[::1]'].includes(api.hostname) ||
    !api.port ||
    api.username ||
    api.password ||
    api.pathname !== '/' ||
    api.search ||
    api.hash
  ) {
    throw new Error('SCIENTIFIC_W1_API_ORIGIN must be a loopback HTTP origin');
  }
}

export default defineConfig({
  testDir: './tests',
  testMatch: '**/scientific-live.spec.ts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 600_000,
  reporter: 'list',
  use: {
    baseURL: apiOrigin,
    ...devices['Desktop Chrome'],
    trace: 'off',
    screenshot: 'off',
    video: 'off',
  },
});
