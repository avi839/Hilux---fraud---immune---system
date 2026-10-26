/// <reference types="vitest/config" />
import type { IncomingMessage } from "node:http";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// /api and /ws are proxied to the API_Server, so the browser only ever talks to the dev
// server (same origin, no CORS needed).
const API = "http://127.0.0.1:8000";
const PORT = 5173;
// The backend accepts WebSocket handshakes only from this exact Origin (Req 27.4).
const DASHBOARD_ORIGIN = `http://localhost:${PORT}`;
// The same dashboard reached by IP instead of name. Its handshakes carry this Origin and are
// re-labelled as DASHBOARD_ORIGIN by the proxy; every other Origin (another site, another
// port) passes through unchanged, so the backend still rejects it.
const LOOPBACK_ALIASES = new Set([`http://127.0.0.1:${PORT}`, `http://[::1]:${PORT}`]);

export default defineConfig({
  plugins: [react()],
  server: {
    // Bind IPv4 loopback explicitly. With "localhost", Node on Windows binds ::1 only, so
    // http://127.0.0.1:5173 is refused; browsers resolve "localhost" to 127.0.0.1 as well,
    // so both URLs work with this bind.
    host: "127.0.0.1",
    port: PORT,
    strictPort: true,
    // The "Load demo scenario" control imports ../docs/sample_transactions.jsonl.
    fs: { allow: [".", "../docs"] },
    proxy: {
      "/api": { target: API, changeOrigin: false },
      "/ws": {
        target: API,
        ws: true,
        // changeOrigin stays false so the browser's Origin header reaches the backend.
        changeOrigin: false,
        configure: (proxy) => {
          proxy.on("proxyReqWs", (proxyReq, req: IncomingMessage) => {
            const origin = req.headers.origin;
            if (typeof origin === "string" && LOOPBACK_ALIASES.has(origin)) proxyReq.setHeader("origin", DASHBOARD_ORIGIN);
          });
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // e2e/ holds Playwright specs, which run in a real browser via `npm run e2e`.
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
    restoreMocks: true,
  },
});
