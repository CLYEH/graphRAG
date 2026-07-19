import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EntityReview } from "./EntityReview";
import { api } from "../api/client";
import { entity, renderWithProviders } from "../test-utils";

const META = { next_cursor: null, build_id: "b1", request_id: "r", elapsed_ms: 1 };

// extract the Idempotency-Key header from a recorded api.POST call
const idemKeyOf = (call: unknown) =>
  (call as { params: { header: Record<string, string> } }).params.header["Idempotency-Key"];

afterEach(() => {
  vi.restoreAllMocks();
});

describe("EntityReview", () => {
  it("lists the needs_review entities from /entities on the needs_review status facet, in operator words", async () => {
    const e = entity({ id: "e-a", canonical_name: "海祭", type: "EVENT" });
    const get = vi.spyOn(api, "GET").mockResolvedValue({
      data: { data: [e], meta: META },
      error: undefined,
    } as never);

    renderWithProviders(<EntityReview project="acme" />);

    expect(await screen.findByText("海祭")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保留" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "排除" })).toBeInTheDocument();
    // WHY: the queue MUST select on the LIFECYCLE status `needs_review` — the same
    // facet health.py counts as needs_review_entities — or the tab and the Health
    // gauge would count different rows (a review_status=unreviewed facet drifts)
    expect(get).toHaveBeenCalledWith(
      "/projects/{project}/entities",
      expect.objectContaining({
        params: expect.objectContaining({
          query: expect.objectContaining({ filter: { status: "needs_review" } }),
        }),
      }),
    );
  });

  it("keeps an entity inline (no confirm) via the approve path with a deterministic idem-key", async () => {
    const e = entity({ id: "e-a", canonical_name: "海祭" });
    vi.spyOn(api, "GET").mockResolvedValue({
      data: { data: [e], meta: META },
      error: undefined,
    } as never);
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: { data: { ...e, status: "active", review_status: "approved" }, meta: META },
      error: undefined,
    } as never);

    renderWithProviders(<EntityReview project="acme" />);

    // 保留 (approve) is non-destructive → fires inline, no alertdialog
    fireEvent.click(await screen.findByRole("button", { name: "保留" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/projects/{project}/entities/{entity_id}/approve",
        expect.objectContaining({
          params: expect.objectContaining({ path: { project: "acme", entity_id: "e-a" } }),
        }),
      ),
    );
    // deterministic key: a lost-response retry replays the stored 200 instead of
    // appending a SECOND ledger decision for one logical action (Codex #105)
    expect(idemKeyOf(post.mock.calls[0][1])).toBe("e-a:approve");
  });

  it("guards 排除 (reject) behind a confirm and posts the reject path only on 確定", async () => {
    const e = entity({ id: "e-a", canonical_name: "海祭" });
    vi.spyOn(api, "GET").mockResolvedValue({
      data: { data: [e], meta: META },
      error: undefined,
    } as never);
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: { data: { ...e, status: "rejected", review_status: "rejected" }, meta: META },
      error: undefined,
    } as never);

    renderWithProviders(<EntityReview project="acme" />);

    // 排除 removes the entity from the active graph with no in-Console undo yet →
    // the first click only ARMS a confirm; nothing posts
    fireEvent.click(await screen.findByRole("button", { name: "排除" }));
    expect(await screen.findByRole("alertdialog", { name: "確認排除" })).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
    // 取消 backs out, still nothing posted
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(post).not.toHaveBeenCalled();

    // re-arm and 確定排除 → the reject path with its deterministic key
    fireEvent.click(await screen.findByRole("button", { name: "排除" }));
    fireEvent.click(await screen.findByRole("button", { name: "確定排除" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/projects/{project}/entities/{entity_id}/reject",
        expect.objectContaining({
          params: expect.objectContaining({ path: { project: "acme", entity_id: "e-a" } }),
        }),
      ),
    );
    expect(idemKeyOf(post.mock.calls[0][1])).toBe("e-a:reject");
  });

  it("stays locked while the queue refreshes after a decision, so a decided row can't be re-decided (Codex #106 P1d)", async () => {
    const e = entity({ id: "e-a", canonical_name: "海祭" });
    let queueCalls = 0;
    vi.spyOn(api, "GET").mockImplementation((() => {
      queueCalls += 1;
      // the initial load resolves; the post-decision refetch HANGS → isFetching
      // stays true while the (stale) decided row is still on screen
      return queueCalls === 1
        ? Promise.resolve({ data: { data: [e], meta: META }, error: undefined })
        : new Promise(() => {});
    }) as never);
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: { data: { ...e, status: "active", review_status: "approved" }, meta: META },
      error: undefined,
    } as never);

    renderWithProviders(<EntityReview project="acme" />);
    fireEvent.click(await screen.findByRole("button", { name: "保留" }));
    await waitFor(() => expect(post).toHaveBeenCalled());

    // the POST resolved (decide.isPending clears) but the invalidated GET is still
    // in flight → the row must NOT re-enable, or a second approve/reject would
    // re-decide it and latest-manual-wins would reverse the one just made
    await waitFor(() => expect(screen.getByRole("button", { name: "保留" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "排除" })).toBeDisabled();
  });

  it("locks the whole queue while a decision is in flight (single-observer concurrency, Codex #104 P2)", async () => {
    const a = entity({ id: "e-a", canonical_name: "海祭" });
    const b = entity({ id: "e-b", canonical_name: "豐年祭" });
    vi.spyOn(api, "GET").mockResolvedValue({
      data: { data: [a, b], meta: META },
      error: undefined,
    } as never);
    // decision never settles → the in-flight window is observable
    vi.spyOn(api, "POST").mockReturnValue(new Promise(() => {}) as never);

    renderWithProviders(<EntityReview project="acme" />);
    const keeps = () => screen.getAllByRole("button", { name: "保留" });
    await waitFor(() => expect(keeps()).toHaveLength(2));

    // 保留 fires inline; deciding row A locks row B too — a second concurrent
    // mutate() on the shared useMutation observer would strand the first's lifecycle
    fireEvent.click(keeps()[0]);
    await waitFor(() => expect(keeps()[0]).toBeDisabled());
    expect(keeps()[1]).toBeDisabled();
  });
});
