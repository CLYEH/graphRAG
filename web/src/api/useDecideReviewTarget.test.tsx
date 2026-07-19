import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./client";
import { useDecideReviewTarget } from "./queries";

import type { ReactNode } from "react";

const META = { next_cursor: null, build_id: "b1", request_id: "r", elapsed_ms: 1 };

const idemKeyOf = (call: unknown) =>
  (call as { params: { header: Record<string, string> } }).params.header["Idempotency-Key"];

afterEach(() => {
  vi.restoreAllMocks();
});

// The shared decide hook's RELATION branch has no UI caller until GOV2-fe-2 adds
// the relation tab. This hook-level test pins its contract NOW: the relation verb
// path AND the `["relation-review", project]` invalidation key that GOV2-fe-2's
// queue hook must match — a key mismatch would silently no-op the post-decision
// refresh, and the entity-only component tests can't cover this branch.
describe("useDecideReviewTarget (relation branch)", () => {
  it("posts the relation approve path with a deterministic idem-key and invalidates the relation-review queue", async () => {
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: { data: { id: "r-1", status: "active" }, meta: META },
      error: undefined,
    } as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useDecideReviewTarget("acme"), { wrapper });
    result.current.mutate({ kind: "relation", targetId: "r-1", verb: "approve", reason: null });

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/projects/{project}/relations/{relation_id}/approve",
        expect.objectContaining({
          params: expect.objectContaining({ path: { project: "acme", relation_id: "r-1" } }),
        }),
      ),
    );
    // deterministic key per (target, verb) — a lost-response retry replays the 200
    // instead of double-recording in the append-only ledger (Codex #105)
    expect(idemKeyOf(post.mock.calls[0][1])).toBe("r-1:approve");
    // the cross-slice contract GOV2-fe-2's useRelationReviewQueue must key on
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ["relation-review", "acme"] }),
    );
  });
});
