import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The browser calls /api/...; the dev server forwards it to the FastAPI service (API_URL in Docker).
const apiUrl = process.env.API_URL ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': path.resolve(import.meta.dirname, './src') } },
  server: {
    host: true,
    port: 5173,
    // the dev server also serves the tunnel's address (ngrok, Cloudflare) on port 8080
    allowedHosts: ['localhost', '.ngrok-free.app', '.ngrok.app', '.ngrok.io', '.trycloudflare.com'],
    // in Docker the source is a mounted folder: file events from the Mac are not reliable, so poll
    watch: process.env.VITE_POLL ? { usePolling: true, interval: 300 } : undefined,
    proxy: {
      // the API builds the sign-in's redirect address from these headers (the browser's host, under /api)
      '/api': {
        target: apiUrl,
        rewrite: (p) => p.replace(/^\/api/, ''),
        configure: (proxy) => {
          proxy.on('proxyReq', (req, incoming) => {
            req.setHeader('X-Forwarded-Host', incoming.headers.host ?? '')
            req.setHeader('X-Forwarded-Proto', 'http')
            req.setHeader('X-Forwarded-Prefix', '/api')
          })
        },
      },
    },
  },
})
