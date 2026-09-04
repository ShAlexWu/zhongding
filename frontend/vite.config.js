import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 50009,
    proxy: {
      // 前端 /api、/static 代理到 FastAPI 后端（端口 50011）
      '/api/audit': {
        target: 'http://127.0.0.1:50012',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api\/audit/, '/api'),
      },
      '/api': {
        target: 'http://127.0.0.1:50011',
        changeOrigin: true,
      },
      '/static': {
        target: 'http://127.0.0.1:50011',
        changeOrigin: true,
      },
    },
  },
})
