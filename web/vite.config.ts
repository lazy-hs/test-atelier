import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import tailwindcss from '@tailwindcss/vite'
import fs from 'node:fs'
import path from 'node:path'

function apiTargetFromRuntimeConfig(): string {
  // Platform launchers pass the actual API listener when they allocate a
  // non-default port.  Prefer it over a stale checked-in runtime snapshot;
  // this keeps the browser proxy aligned with the process that was started.
  const environmentTarget = process.env.AI_STUDIO_API_TARGET?.trim()
  if (environmentTarget) return environmentTarget
  const configPath = path.resolve(import.meta.dirname, '../config/runtime.json')
  try {
    const document = JSON.parse(fs.readFileSync(configPath, 'utf8')) as {
      server?: { api_target?: string; api_port?: number }
    }
    const target = document.server?.api_target?.trim()
    if (target) return target
    const port = Number(document.server?.api_port)
    if (Number.isInteger(port) && port >= 1 && port <= 65535) return `http://127.0.0.1:${port}`
  } catch {
    // Development without a runtime file keeps the historical local default.
  }
  return 'http://127.0.0.1:8000'
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': path.resolve(import.meta.dirname, './src') } },
  // Keep the always-loaded shell below the browser's large-chunk warning.
  // Route modules are already lazy; these vendor groups prevent motion,
  // icons and router code from being duplicated into the shared entry chunk.
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('/node_modules/')) return undefined
          if (id.includes('/react/') || id.includes('/react-dom/') || id.includes('/react-router')) return 'react-vendor'
          if (id.includes('/motion/')) return 'motion-vendor'
          if (id.includes('/lucide-react/')) return 'icons-vendor'
          return undefined
        },
      },
    },
  },
  server: { proxy: { '/api': apiTargetFromRuntimeConfig() } },
})
