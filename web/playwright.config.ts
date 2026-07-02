import { defineConfig } from "@playwright/test";

// End-to-end tests. Run with `npm run test:e2e` (needs `npx playwright install` once).
// Not part of the fast `npm run check` gate — see docs/LOOP.md testing tiers.
export default defineConfig({
  testDir: "./e2e",
  use: { baseURL: "http://localhost:5173" },
  webServer: {
    command: "npm run dev",
    url: "http://localhost:5173",
    reuseExistingServer: true,
  },
});
