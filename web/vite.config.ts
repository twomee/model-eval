/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Build output goes INTO the Python package, so `python -m evalkit serve` serves the UI
// from one process. In dev, `npm run dev` proxies /api to a running `evalkit serve`.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../evalkit/web',
    emptyOutDir: true,
  },
  server: {
    proxy: { '/api': 'http://127.0.0.1:8765' },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    coverage: { provider: 'v8', include: ['src/**'], exclude: ['src/main.tsx', 'src/test/**'] },
  },
})
