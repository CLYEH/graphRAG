import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useDecisionLock, useRestoreKeys } from "./queries";

// Why: these two hooks ARE the mechanized class-17 axes (H20c) — every
// decision surface derives its lock and restore keys from them instead of
// re-deriving (the drift that produced #108 P1 in the first place). The
// tests pin the axis SEMANTICS, so a "simplification" that weakens an axis
// fails here before it ships to every surface at once.

const idle = { isPending: false };
const pending = { isPending: true };
const clean = { isFetching: false, isError: false };

function lock(args: Parameters<typeof useDecisionLock>[0]): boolean {
  return renderHook(() => useDecisionLock(args)).result.current;
}

describe("useDecisionLock", () => {
  it("unlocks only on a clean settled load with no decision in flight", () => {
    expect(lock({ decide: idle, list: clean })).toBe(false);
  });

  it("locks while a decision posts", () => {
    expect(lock({ decide: pending, list: clean })).toBe(true);
  });

  it("locks through the stale-while-revalidate window (#106 P1d)", () => {
    // a resolved POST clears isPending before the invalidated GET lands; a
    // second decision in that window re-hits the now-terminal target
    expect(lock({ decide: idle, list: { isFetching: true, isError: false } })).toBe(true);
  });

  it("error NEVER unlocks (#108 P1) — a failed refetch keeps stale rows on screen", () => {
    // isFetching clears and isError sets while the old rows stay rendered;
    // re-enabling them lets an opposite verb silently reverse the decision
    // just made
    expect(lock({ decide: idle, list: { isFetching: false, isError: true } })).toBe(true);
  });

  it("composes surface-specific extra terms without replacing the core", () => {
    expect(lock({ decide: idle, list: clean, extra: [false, true] })).toBe(true);
    expect(lock({ decide: idle, list: clean, extra: [false, false] })).toBe(false);
    // a surface passing its refresh term via extra (RelationRow) still locks
    // on the mutation axis
    expect(lock({ decide: pending, extra: [false] })).toBe(true);
    expect(lock({ decide: idle, extra: [] })).toBe(false);
  });
});

describe("useRestoreKeys", () => {
  it("retains the key across failed retries — a lost-response retry replays the stored 200 instead of double-recording (#108 R2)", () => {
    const { result } = renderHook(() => useRestoreKeys());
    const first = result.current.mint("r-1");
    expect(result.current.mint("r-1")).toBe(first);
  });

  it("mints FRESH after a success-clear — the next reject→restore cycle must not replay the earlier cycle's stored response", () => {
    const { result } = renderHook(() => useRestoreKeys());
    const first = result.current.mint("r-1");
    result.current.clear("r-1");
    expect(result.current.mint("r-1")).not.toBe(first);
  });

  it("keys are independent per target id — one row's retry must not replay another row's operation", () => {
    const { result } = renderHook(() => useRestoreKeys());
    expect(result.current.mint("a")).not.toBe(result.current.mint("b"));
  });

  it("survives re-render — a parent re-render mid-retry must not re-mint (ref-backed)", () => {
    const { result, rerender } = renderHook(() => useRestoreKeys());
    const first = result.current.mint("r-1");
    rerender();
    expect(result.current.mint("r-1")).toBe(first);
  });
});
