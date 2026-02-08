import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    strictPort: true,
    port: 3001,
    proxy: {
      // REST API
      '/api': {
        target: process.env.VITE_HTTP_PROXY_TARGET ?? 'http://localhost:8200',
        changeOrigin: true,
      },
      // WebSocket
      '/api/ws': {
        target: process.env.VITE_WS_PROXY_TARGET ?? 'ws://localhost:8200',
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
