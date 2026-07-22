import { screen, waitFor } from "@testing-library/react";
import { fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RelationGapList } from "./RelationGapList";
import { api } from "../api/client";
import { entity, relation, renderWithProviders } from "../test-utils";

// Why: the gap lists reuse RelationRow (whose lock/restore/see-the-pair
// behavior is pinned in RelationReview.test.tsx) — what is NEW here and must
// not silently break is the list's own wiring: (a) the query key rides the
// "relation-review" FAMILY, so useDecideReviewTarget's existing prefix
// invalidation refreshes a gap list after a decision with no new wiring —
// that family membership is a load-bearing design fact, pinned here
// BEHAVIORALLY (decide → the gap list refetches); (b) rows render with the
// decision affordances the tab intro promises.

const META = { next_cursor: null, build_id: "b1", request_id: "r", elapsed_ms: 1 };

afterEach(() => {
  vi.restoreAllMocks();
});

function stubGapWorld() {
  const r = relation({ id: "r-gap", src_entity_id: "e-src", dst_entity_id: "e-dst" });
  let listCalls = 0;
  vi.spyOn(api, "GET").mockImplementation(((path: string, opts: unknown) => {
    if (path === "/projects/{project}/relations") {
      listCalls += 1;
      return Promise.resolve({ data: { data: [r], meta: META }, error: undefined });
    }
    if (path === "/projects/{project}/entities/{entity_id}") {
      const id = (opts as { params: { path: { entity_id: string } } }).params.path.entity_id;
      return Promise.resolve({
        data: {
          data: entity({ id, canonical_name: id === "e-src" ? "海祭" : "阿美族" }),
          meta: META,
        },
        error: undefined,
      });
    }
    return Promise.resolve({ data: { data: r, meta: META }, error: undefined });
  }) as never);
  return { calls: () => listCalls };
}

describe("RelationGapList", () => {
  it("renders gap rows with the decision affordances the intro promises", async () => {
    stubGapWorld();
    renderWithProviders(<RelationGapList project="acme" facet="confidence" />);
    expect(await screen.findByText(/海祭/)).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "保留" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "排除" })).toBeEnabled();
  });

  it("mints a FRESH key per logical decision — the deterministic key would replay an earlier cycle (Codex #119 P1)", async () => {
    // gap rows never leave the list, so reject→restore→reject is reachable:
    // with the decided-once `${id}:reject` key, the second reject would
    // replay the FIRST rejection's stored 200 and silently no-op — the
    // operator's exclusion does nothing and the row survives the refetch
    stubGapWorld();
    const keys: (string | undefined)[] = [];
    const post = vi.spyOn(api, "POST").mockImplementation(((_p: string, opts: unknown) => {
      keys.push(
        (opts as { params: { header: Record<string, string> } }).params.header["Idempotency-Key"],
      );
      return Promise.resolve({
        data: { data: relation({ id: "r-gap" }), meta: META },
        error: undefined,
      });
    }) as never);
    renderWithProviders(<RelationGapList project="acme" facet="confidence" />);

    const keep = await screen.findByRole("button", { name: "保留" });
    await waitFor(() => expect(keep).toBeEnabled(), { timeout: 5000 });
    fireEvent.click(keep);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    // the row survives the refetch (facet unchanged) — decide the same row again
    const keepAgain = await screen.findByRole("button", { name: "保留" });
    await waitFor(() => expect(keepAgain).toBeEnabled(), { timeout: 5000 });
    fireEvent.click(keepAgain);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));

    expect(keys[0]).not.toBe("r-gap:approve"); // not the decided-once default
    expect(keys[1]).not.toBe(keys[0]); // success cleared → next cycle minted fresh
  });

  it("retains the key across a FAILED attempt — a lost-response retry must replay, not double-record", async () => {
    stubGapWorld();
    const keys: (string | undefined)[] = [];
    let calls = 0;
    const post = vi.spyOn(api, "POST").mockImplementation(((_p: string, opts: unknown) => {
      keys.push(
        (opts as { params: { header: Record<string, string> } }).params.header["Idempotency-Key"],
      );
      calls += 1;
      if (calls === 1) return Promise.reject(new Error("network lost"));
      return Promise.resolve({
        data: { data: relation({ id: "r-gap" }), meta: META },
        error: undefined,
      });
    }) as never);
    renderWithProviders(<RelationGapList project="acme" facet="confidence" />);

    const keep = await screen.findByRole("button", { name: "保留" });
    await waitFor(() => expect(keep).toBeEnabled(), { timeout: 5000 });
    fireEvent.click(keep);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText(/決定失敗/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "保留" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));

    expect(keys[1]).toBe(keys[0]); // same logical decision → same key on retry
  });

  it("refetches after a decision WITHOUT dedicated invalidation wiring — the family-key contract", async () => {
    // the gap list's queryKey lives under ["relation-review", project] so the
    // decide hook's existing prefix invalidation covers it; if the key ever
    // moves out of the family, a decision would leave a STALE gap list on
    // screen (the decided row still visible) and this test goes red
    const world = stubGapWorld();
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: { data: relation({ id: "r-gap" }), meta: META },
      error: undefined,
    } as never);
    renderWithProviders(<RelationGapList project="acme" facet="evidence" />);

    const keep = await screen.findByRole("button", { name: "保留" });
    await waitFor(() => expect(keep).toBeEnabled(), { timeout: 5000 });
    const before = world.calls();
    fireEvent.click(keep);
    await waitFor(() => expect(post).toHaveBeenCalled());
    await waitFor(() => expect(world.calls()).toBeGreaterThan(before));
  });
});
