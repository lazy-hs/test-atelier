import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  use: { baseURL: process.env.AI_STUDIO_E2E_URL || 'http://127.0.0.1:5173', browserName: 'chromium', channel: 'chrome', headless: true },
  reporter: 'list',
  timeout: 120_000,
})
