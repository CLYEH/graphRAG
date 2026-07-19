import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RelationReview } from "./RelationReview";
import { api } from "../api/client";
import { entity, relation, renderWithProviders } from "../test-utils";

import type { Relation } from "../api/queries";

const META = { next_cursor: null, build_id: "b1", request_id: "r", elapsed_ms: 1 };

const idemKeyOf = (call: unknown) =>
  (call as { params: { header: Record<string, string> } }).params.header["Idempotency-Key"];

// Route-aware GET: the relation LIST (no evidence — the endpoint omits it), the
// relation DETAIL (with evidence), and the src/dst ENTITY details (canonical names,
// keyed by the id param). `list` has src_entity_id "e-src"/dst "e-dst" by default.
function stubRelationWorld({
  list,
  quote = "頭目率領族人舉行",
}: {
  list: Relation[];
  quote?: string | null;
}) {
  return vi.spyOn(api, "GET").mockImplementation(((path: string, opts: unknown) => {
    if (path === "/projects/{project}/relations")
      return Promise.resolve({ data: { data: list, meta: META }, error: undefined });
    if (path === "/projects/{project}/relations/{relation_id}") {
      const rid = (opts as { params: { path: { relation_id: string } } }).params.path.relation_id;
      const base = list.find((r) => r.id === rid) ?? list[0];
      return Promise.resolve({
        data: {
          data: {
            ...base,
            evidence: quote ? [{ id: "ev-1", evidence_type: "chunk", quote }] : [],
          },
          meta: META,
        },
        error: undefined,
      });
    }
    if (path === "/projects/{project}/entities/{entity_id}") {
      const eid = (opts as { params: { path: { entity_id: string } } }).params.path.entity_id;
      const name = eid.startsWith("e-src") || eid.startsWith("e1") ? "海祭" : "阿美族";
      return Promise.resolve({
        data: { data: entity({ id: eid, canonical_name: name }), meta: META },
        error: undefined,
      });
    }
    return Promise.resolve({ data: { data: [], meta: META }, error: undefined });
  }) as never);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("RelationReview", () => {
  it("renders src→type→dst names (resolved from the endpoint entities) and the needs_review facet", async () => {
    const r = relation({
      id: "r-a",
      type: "PRACTICED_BY",
      src_entity_id: "e-src",
      dst_entity_id: "e-dst",
    });
    const get = stubRelationWorld({ list: [r] });

    renderWithProviders(<RelationReview project="acme" />);

    // the operator sees WHICH pair — names, not raw uuids (Codex #106 P1b)
    expect(await screen.findByText(/海祭/)).toBeInTheDocument();
    expect(screen.getByText(/阿美族/)).toBeInTheDocument();
    expect(screen.getByText(/PRACTICED_BY/)).toBeInTheDocument();
    // queue selects on the needs_review lifecycle facet (Health gauge parity)
    expect(get).toHaveBeenCalledWith(
      "/projects/{project}/relations",
      expect.objectContaining({
        params: expect.objectContaining({
          query: expect.objectContaining({ filter: { status: "needs_review" } }),
        }),
      }),
    );
  });

  it("keeps the decision disabled until both endpoint names resolve (must see the pair first)", async () => {
    const r = relation({ id: "r-a", src_entity_id: "e-src", dst_entity_id: "e-dst" });
    // the entity-name fetches never resolve → names stay pending
    vi.spyOn(api, "GET").mockImplementation(((path: string) =>
      path === "/projects/{project}/entities/{entity_id}"
        ? new Promise(() => {})
        : Promise.resolve({
            data: {
              data: path === "/projects/{project}/relations" ? [r] : r,
              meta: META,
            },
            error: undefined,
          })) as never);

    renderWithProviders(<RelationReview project="acme" />);

    // both actions locked while the pair is unknown
    await waitFor(() => expect(screen.getByRole("button", { name: "保留" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "排除" })).toBeDisabled();
  });

  it("keeps the decision locked when an endpoint name FAILS to load (Codex #106 P1c) and offers a retry", async () => {
    const r = relation({ id: "r-a", src_entity_id: "e-src", dst_entity_id: "e-dst" });
    // the entity-name fetches error out (a 503, retried then failed)
    vi.spyOn(api, "GET").mockImplementation(((path: string) =>
      path === "/projects/{project}/entities/{entity_id}"
        ? Promise.resolve({
            data: undefined,
            error: {
              error: { code: "STORE_UNAVAILABLE", message: "down", details: null, request_id: "r" },
            },
            response: { status: 503 },
          })
        : Promise.resolve({
            data: { data: path === "/projects/{project}/relations" ? [r] : r, meta: META },
            error: undefined,
          })) as never);

    renderWithProviders(<RelationReview project="acme" />);

    // a FAILED lookup shows the retry affordance and keeps BOTH actions locked —
    // enabling on error would defeat the "see the pair first" safeguard
    expect(await screen.findByRole("button", { name: "重試" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保留" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "排除" })).toBeDisabled();
  });

  it("lazily loads the evidence quote on demand (the list omits it)", async () => {
    const r = relation({ id: "r-a", src_entity_id: "e-src", dst_entity_id: "e-dst", evidence: [] });
    const get = stubRelationWorld({ list: [r], quote: "頭目率領族人舉行" });

    renderWithProviders(<RelationReview project="acme" />);
    await screen.findByText(/海祭/);

    // nothing evidenced until the reviewer expands it
    expect(screen.queryByText(/頭目率領族人舉行/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看原文證據" }));
    expect(await screen.findByText(/頭目率領族人舉行/)).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith(
      "/projects/{project}/relations/{relation_id}",
      expect.objectContaining({
        params: expect.objectContaining({ path: { project: "acme", relation_id: "r-a" } }),
      }),
    );
  });

  it("keeps a relation inline (no confirm) via the approve path with a deterministic idem-key", async () => {
    const r = relation({ id: "r-a", src_entity_id: "e-src", dst_entity_id: "e-dst" });
    stubRelationWorld({ list: [r] });
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: { data: { ...r, status: "active", review_status: "approved" }, meta: META },
      error: undefined,
    } as never);

    renderWithProviders(<RelationReview project="acme" />);
    // wait until the pair resolves and the decision unlocks
    await waitFor(() => expect(screen.getByRole("button", { name: "保留" })).toBeEnabled());

    fireEvent.click(screen.getByRole("button", { name: "保留" }));
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
    const r = relation({ id: "r-a", src_entity_id: "e-src", dst_entity_id: "e-dst" });
    stubRelationWorld({ list: [r] });
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: { data: { ...r, status: "rejected", review_status: "rejected" }, meta: META },
      error: undefined,
    } as never);

    renderWithProviders(<RelationReview project="acme" />);
    await waitFor(() => expect(screen.getByRole("button", { name: "排除" })).toBeEnabled());

    fireEvent.click(screen.getByRole("button", { name: "排除" }));
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
    const a = relation({
      id: "r-a",
      type: "PRACTICED_BY",
      src_entity_id: "e-src",
      dst_entity_id: "e-dst",
    });
    const b = relation({
      id: "r-b",
      type: "LOCATED_IN",
      src_entity_id: "e-src",
      dst_entity_id: "e-dst",
    });
    stubRelationWorld({ list: [a, b] });
    vi.spyOn(api, "POST").mockReturnValue(new Promise(() => {}) as never);

    renderWithProviders(<RelationReview project="acme" />);
    const keeps = () => screen.getAllByRole("button", { name: "保留" });
    // names resolve → both rows unlock
    await waitFor(() => expect(keeps()[0]).toBeEnabled());
    await waitFor(() => expect(keeps()[1]).toBeEnabled());

    fireEvent.click(keeps()[0]);
    await waitFor(() => expect(keeps()[0]).toBeDisabled());
    expect(keeps()[1]).toBeDisabled();
  });
});
