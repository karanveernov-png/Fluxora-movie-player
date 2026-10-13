import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],

  server: {
    port: 5173,
    cors: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        secure: false,

        // ── Bug fix: mastermaster path duplication + 502 segment storm ────────
        //
        // Root cause summary:
        //   The backend's ForwardedHeadersMiddleware rewrote request.base_url to
        //   http://localhost:5173 (the frontend) because we forwarded X-Forwarded-Host.
        //   The proxy module then embedded http://localhost:5173/api/... in every
        //   rewritten HLS manifest segment URL. hls.js fetched those URLs, Vite
        //   proxied them to FastAPI, and FastAPI's SSRF guard (validate_url) blocked
        //   them because localhost resolves to 127.0.0.1 — a private IP.
        //   Result: every segment request → 422 → hls.js retried → 502 storm.
        //
        // Fix applied on the backend: proxy.py now uses _backend_origin()
        //   (the raw ASGI scope server address, http://127.0.0.1:8000) instead of
        //   request.base_url when constructing proxy segment URLs. Those URLs are
        //   therefore always /api/... paths on the backend itself, which the Vite
        //   proxy forwards correctly.
        //
        // What we still set here:
        //   X-Forwarded-Host / X-Forwarded-Proto — still needed for ForwardedHeaders
        //   Middleware to reconstruct the correct public-facing base_url for
        //   play_url, proxy_url, and other URLs returned to the browser that the
        //   browser itself will fetch. Segment URLs inside manifests no longer use
        //   this rewritten base_url (see B13 fix in proxy.py), so the forwarded
        //   host header no longer causes the SSRF loop.
        //
        // The proxyRes CORS override is REMOVED: it was clobbering the backend's
        //   own CORS headers with a wildcard '*', which broke credentialed requests
        //   and was unnecessary since the Vite proxy itself handles CORS for the
        //   dev server. The backend's CORSMiddleware sets the right headers.
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq, req) => {
            const host = req.headers['host'] ?? 'localhost:5173'
            proxyReq.setHeader('X-Forwarded-Host', host)
            proxyReq.setHeader('X-Forwarded-Proto', 'http')
          })
        },
      },
    },
  },

  build: {
    target: 'es2020',

    rollupOptions: {
      output: {
        manualChunks: (id) => {
          if (id.includes('hls.js') || id.includes('hls/dist')) return 'hls'
        },
      },
    },

    chunkSizeWarningLimit: 650,
    reportCompressedSize: true,
    sourcemap: false,
    cssCodeSplit: true,
  },
})
