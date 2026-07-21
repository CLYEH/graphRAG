import { availableParallelism } from "node:os";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The API runs locally (uvicorn, default :8000) and the frozen contract is
// same-origin (`servers: [{ url: / }]`), so in dev Vite proxies the API path
// prefixes to it. The SPA owns `/` and `/p/*`; the API owns `/projects` and
// `/jobs` (no overlap — that's why routes use the `/p/` prefix). Override the
// target with VITE_API_PROXY.
const apiTarget = process.env.VITE_API_PROXY ?? "http://localhost:8000";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/projects": apiTarget,
      "/jobs": apiTarget,
    },
  },
  test: {
    // Unit/component tests only; Playwright e2e (e2e/) runs separately via `npm run test:e2e`.
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    environment: "jsdom",
    globals: false,
    setupFiles: ["./src/setupTests.ts"],
    // Cap the fork fan-out (H21): vitest's default = max(cores - 1, 1), and
    // on a many-core box the full suite oversubscribes the CPU enough that
    // testing-library's 1s waitFor budgets starve DETERMINISTICALLY
    // (isolated file green / full suite red / CI green — #112). Mirroring the
    // default's cores-1 inside the min keeps small CI runners (2–4 cores) at
    // exactly their current worker count; only boxes with 6+ cores are
    // capped. NOTE the VITEST_MAX_WORKERS env var still overrides this after
    // config resolution — viteConfig.test.ts guards oversubscribing values.
    maxWorkers: Math.min(4, Math.max(availableParallelism() - 1, 1)),
  },
});
