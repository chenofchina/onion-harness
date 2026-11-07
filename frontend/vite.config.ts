import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const root = dirname(fileURLToPath(import.meta.url))
const outDir = resolve(root, '../onion_harness/app/static')

export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir,
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      '/api': {
        target: process.env.ONION_API || 'http://127.0.0.1:8765',
        changeOrigin: true,
        ws: false,
      },
    },
  },
})