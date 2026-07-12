import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// With `globals: false` (vite.config.ts) Testing Library's auto-cleanup does
// not self-register, so mounted trees would leak across tests — unmount here.
afterEach(() => {
  cleanup();
});
