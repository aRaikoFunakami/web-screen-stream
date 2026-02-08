import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      // REST API
      '/api': {
        target: 'http://localhost:8200',
        changeOrigin: true,
      },
      // WebSocket
      '/api/ws': {
        target: 'ws://localhost:8200',
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
