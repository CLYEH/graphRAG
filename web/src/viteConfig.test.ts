import { describe, expect, it } from "vitest";

import config from "../vite.config";

// Why: the fork-pool cap (H21) is invisible to CI — on 2-4 core runners
// min(4, cores-1) equals vitest's own default worker count, so a silently
// dropped cap (a vitest upgrade renaming the option again, a bad merge) keeps
// CI green and only re-starves first-settle waitFors on many-core dev boxes
// (#112: isolated green / full suite red / CI green). These pin INVARIANTS,
// not the literal expression, so they fail on every bypass shape:
// - cap removed → undefined fails the integer assertion;
// - `maxWorkers: 0` → vitest's truthiness-based resolution treats 0 as
//   ABSENT and falls back to its default max(cores-1, 1) fan-out, so 0 must
//   be rejected even though it is a number ≤ 4 (local codex batch P2);
// - a "50%"-style string → core-count-dependent, fails the integer assertion;
// - VITEST_MAX_WORKERS env var → applied AFTER config resolution, so the
//   config value alone cannot vouch for the running pool; when the var is
//   set, the suite is only defended if the override itself stays within the
//   proven budget.
describe("vitest fork-pool cap (H21)", () => {
  it("keeps maxWorkers an integer in [1, 4] — dropping or zeroing the cap re-starves first-settle waitFors on many-core boxes", () => {
    const maxWorkers = (config as { test?: { maxWorkers?: unknown } }).test?.maxWorkers;
    expect(Number.isInteger(maxWorkers)).toBe(true);
    expect(maxWorkers as number).toBeGreaterThanOrEqual(1);
    expect(maxWorkers as number).toBeLessThanOrEqual(4);
  });

  it("rejects a VITEST_MAX_WORKERS override above the proven budget — it bypasses the config cap entirely", () => {
    const override = process.env.VITEST_MAX_WORKERS;
    // falsy matches vitest's own truthiness check: unset AND empty string are
    // both "no override" at runtime, so neither may fail the guard
    if (!override) return;
    const parsed = Number(override);
    expect(Number.isInteger(parsed)).toBe(true);
    expect(parsed).toBeGreaterThanOrEqual(1);
    expect(parsed).toBeLessThanOrEqual(4);
  });
});
