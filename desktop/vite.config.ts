import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

export default defineConfig({
  plugins: [react()],
  base: './',
  // These pure CommonJS helpers are shared with Electron; prebundle for source HMR.
  resolve: { alias: {
    '@noteford/permission-guide': fileURLToPath(new URL('./electron/permission-guide.cjs', import.meta.url)),
    '@noteford/wechat-connect': fileURLToPath(new URL('./electron/wechat-connect.cjs', import.meta.url)),
  } },
  optimizeDeps: { include: ['@noteford/permission-guide', '@noteford/wechat-connect'] },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    host: '127.0.0.1',
    port: 5187,
    strictPort: true,
  },
})
