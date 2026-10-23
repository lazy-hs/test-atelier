import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  // E2E cases share the configured API/database and intentionally exercise
  // global model settings, so parallel workers would race on shared state.
  workers: 1,
  use: { baseURL: process.env.AI_STUDIO_E2E_URL || 'http://127.0.0.1:5173', browserName: 'chromium', channel: 'chrome', headless: true },
  reporter: 'list',
  timeout: 120_000,
})
