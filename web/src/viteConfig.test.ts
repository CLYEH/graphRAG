import { describe, expect, it } from "vitest";

import config from "../vite.config";

// Why: the fork-pool cap (H21) is invisible to CI — on 2-4 core runners
// min(4, cores) equals the core count, i.e. identical to no cap at all, so a
// silently dropped cap (a vitest upgrade renaming the option again, a bad
// merge) keeps CI green and only re-starves first-settle waitFors on
// many-core dev boxes (#112: isolated green / full suite red / CI green).
// This pins the INVARIANT — a numeric ceiling exists and stays within the
// proven budget — not the literal expression, so it fails on cap removal
// (undefined is not ≤ 4) without mirroring the config source.
describe("vitest fork-pool cap (H21)", () => {
  it("keeps a maxWorkers ceiling of at most 4 — dropping the cap re-starves first-settle waitFors on many-core boxes", () => {
    const test = (config as { test?: { maxWorkers?: unknown } }).test;
    expect(test?.maxWorkers).toBeTypeOf("number");
    expect(test?.maxWorkers as number).toBeLessThanOrEqual(4);
  });
});
