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
    // reversible actions in operator words (UXA3), not raw approve/reject
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

  it("keeps an entity via the approve path immediately, with no confirm step (decisions are reversible)", async () => {
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

    // 保留 posts the APPROVE verb path with NO alertdialog — a misclick is
    // recoverable by re-deciding, so the terminal confirm (proposal/merge) is
    // deliberately absent here
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
    // a deterministic `${id}:approve` would replay THIS approve on a legitimate
    // later re-decision — the key must be a fresh random one
    expect(idemKeyOf(post.mock.calls[0][1])).not.toBe("e-a:approve");
  });

  it("mints a FRESH random idem-key per decision so a reversible re-decision cannot replay the last", async () => {
    const e = entity({ id: "e-a" });
    vi.spyOn(api, "GET").mockResolvedValue({
      data: { data: [e], meta: META },
      error: undefined,
    } as never);
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: { data: { ...e, status: "active" }, meta: META },
      error: undefined,
    } as never);

    renderWithProviders(<EntityReview project="acme" />);
    // the mocked queue always returns the row, so it stays after invalidation →
    // a second decision is possible (the reversibility this key strategy exists for)
    fireEvent.click(await screen.findByRole("button", { name: "保留" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole("button", { name: "保留" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "保留" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));

    expect(idemKeyOf(post.mock.calls[0][1])).not.toBe(idemKeyOf(post.mock.calls[1][1]));
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

    // deciding row A locks row B too — a second concurrent mutate() on the shared
    // useMutation observer would strand the first's lifecycle
    fireEvent.click(keeps()[0]);
    await waitFor(() => expect(keeps()[0]).toBeDisabled());
    expect(keeps()[1]).toBeDisabled();
  });
});
