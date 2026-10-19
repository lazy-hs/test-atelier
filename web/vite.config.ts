import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

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
  server: { proxy: { '/api': process.env.AI_STUDIO_API_TARGET || 'http://127.0.0.1:8000' } },
})
