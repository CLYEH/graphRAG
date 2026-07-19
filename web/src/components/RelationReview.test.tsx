import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RelationReview } from "./RelationReview";
import { api } from "../api/client";
import { relation, renderWithProviders } from "../test-utils";

const META = { next_cursor: null, build_id: "b1", request_id: "r", elapsed_ms: 1 };

const idemKeyOf = (call: unknown) =>
  (call as { params: { header: Record<string, string> } }).params.header["Idempotency-Key"];

afterEach(() => {
  vi.restoreAllMocks();
});

describe("RelationReview", () => {
  it("lists needs_review relations on the needs_review facet and lazily loads evidence on demand (the list omits it)", async () => {
    // the LIST row carries NO evidence (api/schemas.py relation_dto omits it —
    // detail-only); the detail GET supplies the quote
    const listRow = relation({ id: "r-a", type: "PRACTICED_BY", evidence: [] });
    const get = vi.spyOn(api, "GET").mockImplementation(((path: string) =>
      path === "/projects/{project}/relations/{relation_id}"
        ? Promise.resolve({
            data: {
              data: {
                ...listRow,
                evidence: [{ id: "ev-1", evidence_type: "chunk", quote: "頭目率領族人舉行" }],
              },
              meta: META,
            },
            error: undefined,
          })
        : Promise.resolve({ data: { data: [listRow], meta: META }, error: undefined })) as never);

    renderWithProviders(<RelationReview project="acme" />);

    expect(await screen.findByText("PRACTICED_BY")).toBeInTheDocument();
    // WHY: the list omits evidence, so NOTHING is fetched/shown until the reviewer
    // expands it — the old inline read would have silently shown "no evidence"
    expect(screen.queryByText(/頭目率領族人舉行/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看原文證據" }));
    // now the DETAIL endpoint (with evidence) is hit and the quote appears
    expect(await screen.findByText(/頭目率領族人舉行/)).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith(
      "/projects/{project}/relations/{relation_id}",
      expect.objectContaining({
        params: expect.objectContaining({ path: { project: "acme", relation_id: "r-a" } }),
      }),
    );
    // and the queue selected on the needs_review lifecycle facet (Health gauge
    // parity — a review_status facet would drift)
    expect(get).toHaveBeenCalledWith(
      "/projects/{project}/relations",
      expect.objectContaining({
        params: expect.objectContaining({
          query: expect.objectContaining({ filter: { status: "needs_review" } }),
        }),
      }),
    );
  });

  it("keeps a relation inline (no confirm) via the approve path with a deterministic idem-key", async () => {
    const r = relation({ id: "r-a" });
    vi.spyOn(api, "GET").mockResolvedValue({
      data: { data: [r], meta: META },
      error: undefined,
    } as never);
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: { data: { ...r, status: "active", review_status: "approved" }, meta: META },
      error: undefined,
    } as never);

    renderWithProviders(<RelationReview project="acme" />);

    fireEvent.click(await screen.findByRole("button", { name: "保留" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/projects/{project}/relations/{relation_id}/approve",
        expect.objectContaining({
          params: expect.objectContaining({ path: { project: "acme", relation_id: "r-a" } }),
        }),
      ),
    );
    expect(idemKeyOf(post.mock.calls[0][1])).toBe("r-a:approve");
  });

  it("guards 排除 (reject) behind a confirm and posts the reject path only on 確定", async () => {
    const r = relation({ id: "r-a" });
    vi.spyOn(api, "GET").mockResolvedValue({
      data: { data: [r], meta: META },
      error: undefined,
    } as never);
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: { data: { ...r, status: "rejected", review_status: "rejected" }, meta: META },
      error: undefined,
    } as never);

    renderWithProviders(<RelationReview project="acme" />);

    fireEvent.click(await screen.findByRole("button", { name: "排除" }));
    expect(await screen.findByRole("alertdialog", { name: "確認排除" })).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(post).not.toHaveBeenCalled();

    fireEvent.click(await screen.findByRole("button", { name: "排除" }));
    fireEvent.click(await screen.findByRole("button", { name: "確定排除" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/projects/{project}/relations/{relation_id}/reject",
        expect.objectContaining({
          params: expect.objectContaining({ path: { project: "acme", relation_id: "r-a" } }),
        }),
      ),
    );
    expect(idemKeyOf(post.mock.calls[0][1])).toBe("r-a:reject");
  });

  it("locks the whole queue while a decision is in flight (Codex #104 P2)", async () => {
    const a = relation({ id: "r-a", type: "PRACTICED_BY" });
    const b = relation({ id: "r-b", type: "LOCATED_IN" });
    vi.spyOn(api, "GET").mockResolvedValue({
      data: { data: [a, b], meta: META },
      error: undefined,
    } as never);
    vi.spyOn(api, "POST").mockReturnValue(new Promise(() => {}) as never);

    renderWithProviders(<RelationReview project="acme" />);
    const keeps = () => screen.getAllByRole("button", { name: "保留" });
    await waitFor(() => expect(keeps()).toHaveLength(2));

    fireEvent.click(keeps()[0]);
    await waitFor(() => expect(keeps()[0]).toBeDisabled());
    expect(keeps()[1]).toBeDisabled();
  });
});
