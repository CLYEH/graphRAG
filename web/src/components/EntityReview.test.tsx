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

  it("pages incrementally — 載入更多 fetches the next cursor on demand (Codex #105 P2)", async () => {
    const a = entity({ id: "e-a", canonical_name: "海祭" });
    const b = entity({ id: "e-b", canonical_name: "豐年祭" });
    const get = vi.spyOn(api, "GET").mockImplementation(((_path: string, opts: unknown) => {
      const cursor = (opts as { params: { query: { cursor?: string } } }).params.query.cursor;
      return Promise.resolve(
        cursor === "c2"
          ? { data: { data: [b], meta: { ...META, next_cursor: null } }, error: undefined }
          : { data: { data: [a], meta: { ...META, next_cursor: "c2" } }, error: undefined },
      );
    }) as never);

    renderWithProviders(<EntityReview project="acme" />);
    // WHY incremental: only page 1 renders up front (page-to-exhaustion would
    // serialize every page before first paint on a corpus-sized backlog)
    expect(await screen.findByText("海祭")).toBeInTheDocument();
    expect(screen.queryByText("豐年祭")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "載入更多" }));
    expect(await screen.findByText("豐年祭")).toBeInTheDocument();
    // page 2 was fetched with the server cursor, on demand
    expect(get).toHaveBeenLastCalledWith(
      "/projects/{project}/entities",
      expect.objectContaining({
        params: expect.objectContaining({ query: expect.objectContaining({ cursor: "c2" }) }),
      }),
    );
  });

  it("fails loud when the active build changes between pages (the pin rides the pageParam)", async () => {
    const a = entity({ id: "e-a", canonical_name: "海祭" });
    const b = entity({ id: "e-b", canonical_name: "豐年祭" });
    vi.spyOn(api, "GET").mockImplementation(((_path: string, opts: unknown) => {
      const cursor = (opts as { params: { query: { cursor?: string } } }).params.query.cursor;
      return Promise.resolve(
        cursor === "c2"
          ? // page 2 arrives from a DIFFERENT active build — a spliced list
            {
              data: { data: [b], meta: { ...META, build_id: "b2", next_cursor: null } },
              error: undefined,
            }
          : { data: { data: [a], meta: { ...META, next_cursor: "c2" } }, error: undefined },
      );
    }) as never);

    renderWithProviders(<EntityReview project="acme" />);
    await screen.findByText("海祭");
    fireEvent.click(screen.getByRole("button", { name: "載入更多" }));

    // the pin must refuse the cross-build splice (its rows would 404 on decide) —
    // loaded rows stay, the failure is inline (#102 next-page discipline)
    expect(await screen.findByText(/active build changed/i)).toBeInTheDocument();
    expect(screen.getByText("海祭")).toBeInTheDocument();
    expect(screen.queryByText("豐年祭")).not.toBeInTheDocument();
  });

  it("keeps stale rows LOCKED when the post-decision refetch fails (Codex #108 P1)", async () => {
    const e = entity({ id: "e-a", canonical_name: "海祭" });
    let getCalls = 0;
    vi.spyOn(api, "GET").mockImplementation((() => {
      getCalls += 1;
      // initial load OK; the post-decision refetch ERRORS → react-query keeps the
      // stale pages, clears isFetching, and sets isError
      return getCalls === 1
        ? Promise.resolve({ data: { data: [e], meta: META }, error: undefined })
        : Promise.resolve({
            data: undefined,
            error: {
              error: { code: "STORE_UNAVAILABLE", message: "down", details: null, request_id: "r" },
            },
            response: { status: 503 },
          });
    }) as never);
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: { data: { ...e, status: "active", review_status: "approved" }, meta: META },
      error: undefined,
    } as never);

    renderWithProviders(<EntityReview project="acme" />);
    fireEvent.click(await screen.findByRole("button", { name: "保留" }));
    await waitFor(() => expect(post).toHaveBeenCalled());

    // the stale decided row is still on screen — its controls MUST stay locked,
    // or an opposite verb would silently reverse the decision just made
    await waitFor(() => expect(screen.getByText(/載入失敗/)).toBeInTheDocument());
    expect(screen.getByText("海祭")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保留" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "排除" })).toBeDisabled();
  });

  it("重新載入 after a build-swap pin trip restarts from page 1 and recovers (Codex #108 P2)", async () => {
    const a = entity({ id: "e-a", canonical_name: "海祭" });
    const b = entity({ id: "e-b", canonical_name: "豐年祭" });
    let swapped = false;
    vi.spyOn(api, "GET").mockImplementation(((_path: string, opts: unknown) => {
      const cursor = (opts as { params: { query: { cursor?: string } } }).params.query.cursor;
      if (!swapped) {
        if (cursor === "c2") {
          // page 2 arrives from the NEW build — the pin trips; from here on the
          // world has swapped to build b2
          swapped = true;
          return Promise.resolve({
            data: { data: [b], meta: { ...META, build_id: "b2", next_cursor: null } },
            error: undefined,
          });
        }
        return Promise.resolve({
          data: { data: [a], meta: { ...META, next_cursor: "c2" } },
          error: undefined,
        });
      }
      // post-swap world: a single clean page from build b2
      return Promise.resolve({
        data: { data: [b], meta: { ...META, build_id: "b2", next_cursor: null } },
        error: undefined,
      });
    }) as never);

    renderWithProviders(<EntityReview project="acme" />);
    await screen.findByText("海祭");
    fireEvent.click(screen.getByRole("button", { name: "載入更多" }));
    await screen.findByText(/active build changed/i);

    // a fetchNextPage retry would replay the stale cursor + old pin forever; the
    // full-refetch retry restarts from page 1 and lands on the new build cleanly
    fireEvent.click(screen.getByRole("button", { name: "重新載入" }));
    expect(await screen.findByText("豐年祭")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText(/active build changed/i)).not.toBeInTheDocument(),
    );
  });

  it("lists rejected entities in the 已排除 view and restores with a FRESH key per attempt (GOV2-fe-4a)", async () => {
    const rej = entity({
      id: "e-a",
      canonical_name: "海祭",
      status: "rejected",
      review_status: "rejected",
    });
    const get = vi.spyOn(api, "GET").mockImplementation(((_path: string, opts: unknown) => {
      const filter = (opts as { params: { query: { filter?: { status?: string } } } }).params.query
        .filter;
      return Promise.resolve({
        data: { data: filter?.status === "rejected" ? [rej] : [], meta: META },
        error: undefined,
      });
    }) as never);
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: { data: { ...rej, status: "active", review_status: "approved" }, meta: META },
      error: undefined,
    } as never);

    renderWithProviders(<EntityReview project="acme" />);
    fireEvent.click(await screen.findByRole("button", { name: "已排除" }));

    // the decided view selects the rejected lifecycle facet
    expect(await screen.findByText("海祭")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith(
      "/projects/{project}/entities",
      expect.objectContaining({
        params: expect.objectContaining({
          query: expect.objectContaining({ filter: { status: "rejected" } }),
        }),
      }),
    );

    // restore = approve, but as a DELIBERATE re-decision: the deterministic
    // `${id}:approve` would replay an earlier cycle's stored response, so each
    // attempt mints a fresh key
    fireEvent.click(screen.getByRole("button", { name: /復原/ }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledWith(
      "/projects/{project}/entities/{entity_id}/approve",
      expect.anything(),
    );
    const k1 = idemKeyOf(post.mock.calls[0][1]);
    expect(k1).not.toBe("e-a:approve");
    await waitFor(() => expect(screen.getByRole("button", { name: /復原/ })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /復原/ }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(idemKeyOf(post.mock.calls[1][1])).not.toBe(k1);
  });

  it("reuses the SAME restore key across a failed retry — one key per logical restore (Codex #108 R2)", async () => {
    const rej = entity({
      id: "e-a",
      canonical_name: "海祭",
      status: "rejected",
      review_status: "rejected",
    });
    vi.spyOn(api, "GET").mockImplementation(((_path: string, opts: unknown) => {
      const filter = (opts as { params: { query: { filter?: { status?: string } } } }).params.query
        .filter;
      return Promise.resolve({
        data: { data: filter?.status === "rejected" ? [rej] : [], meta: META },
        error: undefined,
      });
    }) as never);
    let postCalls = 0;
    const post = vi.spyOn(api, "POST").mockImplementation((() => {
      postCalls += 1;
      // the FIRST restore attempt fails (e.g. the response is lost / 503); the
      // retry must replay the SAME key or the append-only ledger records a second
      // approval whose newer timestamp could override an intervening decision
      return postCalls === 1
        ? Promise.resolve({
            data: undefined,
            error: {
              error: { code: "STORE_UNAVAILABLE", message: "down", details: null, request_id: "r" },
            },
            response: { status: 503 },
          })
        : Promise.resolve({
            data: { data: { ...rej, status: "active", review_status: "approved" }, meta: META },
            error: undefined,
          });
    }) as never);

    renderWithProviders(<EntityReview project="acme" />);
    fireEvent.click(await screen.findByRole("button", { name: "已排除" }));

    fireEvent.click(await screen.findByRole("button", { name: /復原/ }));
    await waitFor(() => expect(screen.getByText(/決定失敗/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /復原/ }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));

    const k1 = idemKeyOf(post.mock.calls[0][1]);
    const k2 = idemKeyOf(post.mock.calls[1][1]);
    expect(k1).toBe(k2);
    expect(k1).not.toBe("e-a:approve");
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
