import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ReviewCases } from "./ReviewCases";
import { api } from "../api/client";
import {
  mergeCandidate,
  renderWithProviders,
  stubApiError,
  stubDecision,
  stubReviewWorld,
} from "../test-utils";

import type { MergeCandidate } from "../api/queries";

const CID = "c1111111-1111-1111-1111-111111111111";
const LEFT = "e1000000-0000-0000-0000-000000000000";
const RIGHT = "e2000000-0000-0000-0000-000000000000";

const META_NULL = {
  request_id: "00000000-0000-0000-0000-000000000000",
  build_id: null,
  elapsed_ms: 1,
  next_cursor: null,
};

// A named case whose snapshot names deliberately differ from every id prefix —
// the discriminating fixture for "names, never ids": a UI that renders
// id.slice(0, 8) (the pre-UXA1 behavior) cannot pass by accident.
function namedCandidate(overrides: Partial<MergeCandidate> = {}): MergeCandidate {
  return mergeCandidate({
    id: CID,
    left_entity_id: LEFT,
    right_entity_id: RIGHT,
    left_snapshot: { name: "台灣海洋科技館", type: "FACILITY" },
    right_snapshot: { name: "台灣海洋科技館(海科館)", type: "FACILITY" },
    ...overrides,
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ReviewCases", () => {
  it("leads with the snapshot names — never the id prefix", async () => {
    stubReviewWorld({ candidates: [namedCandidate()] });
    renderWithProviders(<ReviewCases project="acme" />);

    expect(await screen.findByText("台灣海洋科技館")).toBeInTheDocument();
    expect(screen.getByText("台灣海洋科技館(海科館)")).toBeInTheDocument();
    // the pre-UXA1 table rendered exactly id.slice(0, 8) as the row identity;
    // its reappearance anywhere means the translation layer regressed
    expect(screen.queryByText(CID.slice(0, 8))).not.toBeInTheDocument();
  });

  it("says so honestly when a snapshot carries no name", async () => {
    stubReviewWorld({ candidates: [namedCandidate({ left_snapshot: null })] });
    renderWithProviders(<ReviewCases project="acme" />);

    expect(await screen.findByText("(名稱快照缺失)")).toBeInTheDocument();
  });

  it("keeps the terminal verbs behind an explicit confirm — no POST until 確定", async () => {
    // approve/reject are §17-terminal (BA5): one accidental click must be
    // recoverable BY INTERACTION, so the first click only opens the confirm.
    stubReviewWorld({ candidates: [namedCandidate()] });
    const post = stubDecision(namedCandidate({ status: "approved" }));
    renderWithProviders(<ReviewCases project="acme" />);

    fireEvent.click(await screen.findByRole("button", { name: "是,合併" }));
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByRole("alertdialog", { name: "確認決定" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "確定合併" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/projects/{project}/merge-candidates/{candidate_id}/approve",
        {
          params: {
            path: { project: "acme", candidate_id: CID },
            // deterministic per (candidate, verb): a lost 2xx replays on retry
            // instead of 400-ing on the already-decided candidate
            header: { "Idempotency-Key": `${CID}:approve` },
          },
          body: { reason: null },
        },
      ),
    );
  });

  it("取消 backs out of the confirm without posting", async () => {
    stubReviewWorld({ candidates: [namedCandidate()] });
    const post = stubDecision(namedCandidate({ status: "rejected" }));
    renderWithProviders(<ReviewCases project="acme" />);

    fireEvent.click(await screen.findByRole("button", { name: "不是,分開" }));
    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(post).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "是,合併" })).toBeEnabled();
  });

  it("posts the typed reason with the chosen verb on its own path", async () => {
    // the verb rides the URL, not the body — a mis-wired verb→path or a dropped
    // reason would merge/split the wrong way, so pin both together
    stubReviewWorld({ candidates: [namedCandidate()] });
    const post = stubDecision(namedCandidate({ status: "rejected" }));
    renderWithProviders(<ReviewCases project="acme" />);

    fireEvent.change(await screen.findByLabelText("決定理由"), {
      target: { value: "不同展區,字面相似而已" },
    });
    fireEvent.click(screen.getByRole("button", { name: "不是,分開" }));
    fireEvent.click(screen.getByRole("button", { name: "確定分開" }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/projects/{project}/merge-candidates/{candidate_id}/reject",
        {
          params: {
            path: { project: "acme", candidate_id: CID },
            header: { "Idempotency-Key": `${CID}:reject` },
          },
          body: { reason: "不同展區,字面相似而已" },
        },
      ),
    );
  });

  it("skip (defer) posts immediately — it is not terminal, so no confirm gate", async () => {
    stubReviewWorld({ candidates: [namedCandidate()] });
    const post = stubDecision(namedCandidate({ status: "deferred" }));
    renderWithProviders(<ReviewCases project="acme" />);

    fireEvent.click(await screen.findByRole("button", { name: "跳過,下次再問" }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/projects/{project}/merge-candidates/{candidate_id}/defer",
        expect.objectContaining({ body: { reason: null } }),
      ),
    );
  });

  it("advances past a skipped case instead of re-presenting it (Codex #76)", async () => {
    // a deferred row intentionally STAYS in the queue (review.py returns
    // pending+deferred), so unlike approve/reject the clamp can't advance by
    // itself — without an explicit step forward,「跳過,下次再問」re-renders
    // the same pair as deferred and skips nothing
    const second = namedCandidate({
      id: "c2222222-2222-2222-2222-222222222222",
      left_snapshot: { name: "區域探索館", type: "FACILITY" },
      right_snapshot: { name: "區域探索廳", type: "FACILITY" },
    });
    stubReviewWorld({ candidates: [namedCandidate(), second] });
    stubDecision(namedCandidate({ status: "deferred" }));
    renderWithProviders(<ReviewCases project="acme" />);

    fireEvent.click(await screen.findByRole("button", { name: "跳過,下次再問" }));

    expect(await screen.findByText("區域探索館")).toBeInTheDocument();
    expect(screen.getByText("第 2 筆,共 2 筆")).toBeInTheDocument();
  });

  it("locks navigation while a decision POST is in flight (Codex #76 R4)", async () => {
    // clicking 下一筆 mid-POST advances the index against the OLD list; when
    // the decided row is removed, that index lands one case further and a
    // pending case is silently skipped — so the nav freezes with the verbs
    const second = namedCandidate({
      id: "c2222222-2222-2222-2222-222222222222",
      left_snapshot: { name: "區域探索館", type: "FACILITY" },
      right_snapshot: { name: "區域探索廳", type: "FACILITY" },
    });
    stubReviewWorld({ candidates: [namedCandidate(), second] });
    // a POST that never settles keeps the decision permanently in flight
    vi.spyOn(api, "POST").mockImplementation((() => new Promise(() => {})) as never);
    renderWithProviders(<ReviewCases project="acme" />);

    expect(await screen.findByRole("button", { name: "下一筆" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "是,合併" }));
    fireEvent.click(screen.getByRole("button", { name: "確定合併" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "下一筆" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "上一筆" })).toBeDisabled();
  });

  it("resets the walk when the queue is REPLACED externally (Codex #76 R6)", async () => {
    // an external replacement (new active build, another tab) with a retained
    // index would bury a shorter queue behind the end panel — the walk resets
    const second = namedCandidate({
      id: "c2222222-2222-2222-2222-222222222222",
      left_snapshot: { name: "區域探索館", type: "FACILITY" },
      right_snapshot: { name: "區域探索廳", type: "FACILITY" },
    });
    const fresh = namedCandidate({
      id: "c3333333-3333-3333-3333-333333333333",
      left_snapshot: { name: "海祭", type: "EVENT" },
      right_snapshot: { name: "海祭儀式", type: "EVENT" },
    });
    let phase = 0;
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path === "/projects/{project}/merge-candidates")
        return Promise.resolve({
          data: {
            data: phase === 0 ? [namedCandidate(), second] : [fresh],
            meta: META_NULL,
          },
          error: undefined,
        });
      if (path === "/projects/{project}/graph/subgraph")
        return Promise.resolve({
          data: { data: { nodes: [], edges: [] }, meta: META_NULL },
          error: undefined,
        });
      throw new Error(`unstubbed GET ${path}`);
    }) as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ReviewCases project="acme" />
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "下一筆" }));
    expect(await screen.findByText("區域探索館")).toBeInTheDocument();

    phase = 1;
    void client.invalidateQueries({ queryKey: ["merge-candidates", "acme"] });

    // without the reset, index 1 >= length 1 would show the end panel and hide
    // the fresh case entirely
    expect(await screen.findByText("海祭")).toBeInTheDocument();
    expect(screen.getByText("第 1 筆,共 1 筆")).toBeInTheDocument();
  });

  it("keeps the walk position when OUR decision shrank the queue (Codex #76 R6)", async () => {
    // approving case 2 of 3: the next case slides into the same slot — a reset
    // here would yank the curator back to case 1 after every decision
    const a = namedCandidate();
    const b = namedCandidate({
      id: "c2222222-2222-2222-2222-222222222222",
      left_snapshot: { name: "區域探索館", type: "FACILITY" },
      right_snapshot: { name: "區域探索廳", type: "FACILITY" },
    });
    const c = namedCandidate({
      id: "c3333333-3333-3333-3333-333333333333",
      left_snapshot: { name: "海祭", type: "EVENT" },
      right_snapshot: { name: "海祭儀式", type: "EVENT" },
    });
    let decided = false;
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path === "/projects/{project}/merge-candidates")
        return Promise.resolve({
          data: { data: decided ? [a, c] : [a, b, c], meta: META_NULL },
          error: undefined,
        });
      if (path === "/projects/{project}/graph/subgraph")
        return Promise.resolve({
          data: { data: { nodes: [], edges: [] }, meta: META_NULL },
          error: undefined,
        });
      throw new Error(`unstubbed GET ${path}`);
    }) as never);
    vi.spyOn(api, "POST").mockImplementation((() => {
      decided = true;
      return Promise.resolve({
        data: { data: { ...b, status: "approved" }, meta: META_NULL },
        error: undefined,
      });
    }) as never);
    renderWithProviders(<ReviewCases project="acme" />);

    fireEvent.click(await screen.findByRole("button", { name: "下一筆" }));
    expect(await screen.findByText("區域探索館")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "是,合併" }));
    fireEvent.click(screen.getByRole("button", { name: "確定合併" }));

    // case 3 slides into slot 2; a reset would show case 1 (台灣海洋科技館)
    expect(await screen.findByText("海祭")).toBeInTheDocument();
    expect(screen.getByText("第 2 筆,共 2 筆")).toBeInTheDocument();
  });

  it("a FAILED decision disarms the expected-shrink flag (Codex #76 R7)", async () => {
    // the flag is armed BEFORE the mutate call (the invalidation refetch can
    // unmount the card and skip every mutate-level callback), so a failed POST
    // must disarm it in onError — otherwise the next EXTERNAL replacement
    // would consume the stale flag and keep a position that belongs to a
    // different queue
    const second = namedCandidate({
      id: "c2222222-2222-2222-2222-222222222222",
      left_snapshot: { name: "區域探索館", type: "FACILITY" },
      right_snapshot: { name: "區域探索廳", type: "FACILITY" },
    });
    const fresh = namedCandidate({
      id: "c3333333-3333-3333-3333-333333333333",
      left_snapshot: { name: "海祭", type: "EVENT" },
      right_snapshot: { name: "海祭儀式", type: "EVENT" },
    });
    let phase = 0;
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path === "/projects/{project}/merge-candidates")
        return Promise.resolve({
          data: { data: phase === 0 ? [namedCandidate(), second] : [fresh], meta: META_NULL },
          error: undefined,
        });
      if (path === "/projects/{project}/graph/subgraph")
        return Promise.resolve({
          data: { data: { nodes: [], edges: [] }, meta: META_NULL },
          error: undefined,
        });
      throw new Error(`unstubbed GET ${path}`);
    }) as never);
    vi.spyOn(api, "POST").mockResolvedValue({
      data: undefined,
      error: { error: { code: "INTERNAL", message: "decide blew up" } },
    } as never);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, retryDelay: 0 } },
    });
    render(
      <QueryClientProvider client={client}>
        <ReviewCases project="acme" />
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "下一筆" }));
    expect(await screen.findByText("區域探索館")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "是,合併" }));
    fireEvent.click(screen.getByRole("button", { name: "確定合併" }));
    expect(await screen.findByText(/決定送出失敗/)).toBeInTheDocument();

    // external replacement AFTER the failed decision: the walk must reset —
    // a stale armed flag would keep index 1 and bury the single fresh case
    phase = 1;
    void client.invalidateQueries({ queryKey: ["merge-candidates", "acme"] });
    expect(await screen.findByText("海祭")).toBeInTheDocument();
    expect(screen.getByText("第 1 筆,共 1 筆")).toBeInTheDocument();
  });

  it("scope-proof context reads do not retry (Codex #76 R6)", async () => {
    // the production QueryClient retries failed queries by default; a
    // deterministic 404 that PROVES scope loss must not sit behind a retry
    // backoff with the verbs still enabled — the hooks disable retry for the
    // scope/policy error classes, so the verdict lands on the first failure
    let subgraphCalls = 0;
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path === "/projects/{project}/merge-candidates")
        return Promise.resolve({
          data: { data: [namedCandidate()], meta: META_NULL },
          error: undefined,
        });
      if (path === "/projects/{project}/graph/subgraph") {
        subgraphCalls += 1;
        return Promise.resolve({
          data: undefined,
          error: { error: { code: "VALIDATION_ERROR", message: "entity not in active build" } },
          response: { status: 404 },
        });
      }
      throw new Error(`unstubbed GET ${path}`);
    }) as never);
    // production-like client: retries NOT disabled
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <ReviewCases project="acme" />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getAllByText(/知識庫版本已切換,此案已過期/)).toHaveLength(2));
    // one call per entity, zero retries — with default retry the verdict would
    // wait out ~3 backoff rounds per side and this assert would see 4+
    expect(subgraphCalls).toBe(2);
  });

  it("locks the verbs while the queue itself is refetching (Codex #76 R5)", async () => {
    // FE1's fail-closed gate on the write side: during a BACKGROUND refetch
    // (refocus, external invalidation) the rows on screen may be about to be
    // replaced, and a decide against the old snapshot can 404 — so the verbs
    // wait for the settled queue. A hung second GET keeps isFetching latched;
    // the refetch is started directly on the query client, with no mutation
    // and no scope proof in play, so ONLY the new queueRefreshing gate can be
    // what disables the verbs.
    let queueCalls = 0;
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path === "/projects/{project}/merge-candidates") {
        queueCalls += 1;
        if (queueCalls > 1) return new Promise(() => {});
        return Promise.resolve({
          data: {
            data: [namedCandidate()],
            meta: {
              request_id: "00000000-0000-0000-0000-000000000000",
              build_id: null,
              elapsed_ms: 1,
              next_cursor: null,
            },
          },
          error: undefined,
        });
      }
      if (path === "/projects/{project}/graph/subgraph")
        return Promise.resolve({
          data: {
            data: { nodes: [], edges: [] },
            meta: {
              request_id: "00000000-0000-0000-0000-000000000000",
              build_id: null,
              elapsed_ms: 1,
              next_cursor: null,
            },
          },
          error: undefined,
        });
      throw new Error(`unstubbed GET ${path}`);
    }) as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ReviewCases project="acme" />
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("button", { name: "是,合併" })).toBeEnabled();
    void client.invalidateQueries({ queryKey: ["merge-candidates", "acme"] });

    await waitFor(() => expect(screen.getByRole("button", { name: "是,合併" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "不是,分開" })).toBeDisabled();
  });

  it("one card's scope proof freezes the WHOLE queue, navigation included (Codex #76 R4)", async () => {
    // the freeze lives in the parent: every card in the snapshot is from the
    // same dead build, so navigating to case 2 must not re-arm the verbs
    const second = namedCandidate({
      id: "c2222222-2222-2222-2222-222222222222",
      left_snapshot: { name: "區域探索館", type: "FACILITY" },
      right_snapshot: { name: "區域探索廳", type: "FACILITY" },
    });
    stubReviewWorld({ candidates: [namedCandidate(), second], failSubgraph: "scope" });
    renderWithProviders(<ReviewCases project="acme" />);

    await waitFor(() => expect(screen.getByRole("button", { name: "是,合併" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "下一筆" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "上一筆" })).toBeDisabled();
    expect(screen.getByText(/暫停所有決定/)).toBeInTheDocument();
  });

  it("offers a deferred case no skip and says why (§17: never re-defer)", async () => {
    stubReviewWorld({ candidates: [namedCandidate({ status: "deferred" })] });
    renderWithProviders(<ReviewCases project="acme" />);

    expect(await screen.findByRole("button", { name: "是,合併" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "不是,分開" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "跳過,下次再問" })).not.toBeInTheDocument();
    expect(screen.getByText(/先前已跳過/)).toBeInTheDocument();
  });

  it("renders each side's evidenced relation with its quote as the context", async () => {
    stubReviewWorld({
      candidates: [namedCandidate()],
      subgraph: {
        nodes: [
          { id: LEFT, label: "台灣海洋科技館" },
          { id: RIGHT, label: "台灣海洋科技館(海科館)" },
          { id: "e3000000-0000-0000-0000-000000000000", label: "基隆八斗子" },
        ],
        edges: [
          {
            id: "r1000000-0000-0000-0000-000000000000",
            src: LEFT,
            dst: "e3000000-0000-0000-0000-000000000000",
            type: "LOCATED_IN",
          },
        ],
      },
      relation: {
        id: "r1000000-0000-0000-0000-000000000000",
        evidence: [
          {
            id: "ev100000-0000-0000-0000-000000000000",
            evidence_type: "chunk",
            quote: "位於基隆八斗子",
          },
        ],
      },
    });
    renderWithProviders(<ReviewCases project="acme" />);

    expect(await screen.findAllByText(/LOCATED_IN/)).not.toHaveLength(0);
    expect(await screen.findAllByText("「位於基隆八斗子」")).not.toHaveLength(0);
  });

  it("a scope-NEUTRAL context failure says so but never blocks the decision", async () => {
    // the context is the decision AID; the graph store being down must not
    // freeze the review queue — but it must say so, not render an empty box.
    // (the over-blocking dual of the scope-proof tests below: neutral outages
    // must NOT trip the scope gate)
    stubReviewWorld({ candidates: [namedCandidate()], failSubgraph: "neutral" });
    renderWithProviders(<ReviewCases project="acme" />);

    // both panels, not just the first to settle — findAll returns on ≥1 match,
    // so the two-sided assert needs waitFor
    await waitFor(() => expect(screen.getAllByText(/上下文載入失敗/)).toHaveLength(2));
    expect(screen.getByRole("button", { name: "是,合併" })).toBeEnabled();
  });

  it("a scope-PROOF context failure blocks deciding and refetches the queue (Codex #76 R2)", async () => {
    // SubgraphScopeError proves the active build moved on: the whole queue was
    // read from a dead build and a decide would 404 against the rebound build
    // (review.py re-resolves per request) — so the verbs lock and the queue
    // refetches, instead of a "仍可作決定" line inviting a doomed POST
    const get = stubReviewWorld({ candidates: [namedCandidate()], failSubgraph: "scope" });
    renderWithProviders(<ReviewCases project="acme" />);

    await waitFor(() => expect(screen.getAllByText(/知識庫版本已切換,此案已過期/)).toHaveLength(2));
    expect(screen.getByText(/正在重新載入最新佇列/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "是,合併" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "不是,分開" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "跳過,下次再問" })).toBeDisabled();
    // the invalidation refetched the queue (initial load + ≥1 refetch)
    await waitFor(() => {
      const queueCalls = get.mock.calls.filter(
        (c) => c[0] === "/projects/{project}/merge-candidates",
      );
      expect(queueCalls.length).toBeGreaterThanOrEqual(2);
    });
  });

  it("a context read stamped with a DIFFERENT build proves the same scope loss", async () => {
    // even a SUCCESSFUL subgraph read betrays the swap when its meta names a
    // build other than the candidate's — same verdict as the error path
    stubReviewWorld({
      candidates: [namedCandidate()],
      subgraphBuildId: "b9999999-9999-9999-9999-999999999999",
    });
    renderWithProviders(<ReviewCases project="acme" />);

    await waitFor(() => expect(screen.getAllByText(/知識庫版本已切換,此案已過期/)).toHaveLength(2));
    expect(screen.getByRole("button", { name: "是,合併" })).toBeDisabled();
  });

  it("a relation-detail scope loss blocks deciding like the subgraph path (Codex #76 R3)", async () => {
    // the second hop is the same sentinel: the subgraph SUCCEEDS, then the
    // relation detail 404s because the build swapped between the two reads —
    // DetailScopeGoneError proves the queue is stale exactly like
    // SubgraphScopeError does
    stubReviewWorld({
      candidates: [namedCandidate()],
      subgraph: {
        nodes: [
          { id: LEFT, label: "左" },
          { id: RIGHT, label: "右" },
          { id: "e3000000-0000-0000-0000-000000000000", label: "鄰居" },
        ],
        edges: [
          {
            id: "r1000000-0000-0000-0000-000000000000",
            src: LEFT,
            dst: "e3000000-0000-0000-0000-000000000000",
            type: "LOCATED_IN",
          },
          {
            id: "r2000000-0000-0000-0000-000000000000",
            src: RIGHT,
            dst: "e3000000-0000-0000-0000-000000000000",
            type: "LOCATED_IN",
          },
        ],
      },
      failRelation: "scope",
    });
    renderWithProviders(<ReviewCases project="acme" />);

    await waitFor(() => expect(screen.getAllByText(/知識庫版本已切換,此案已過期/)).toHaveLength(2));
    expect(screen.getByRole("button", { name: "是,合併" })).toBeDisabled();
  });

  it("a scope-NEUTRAL relation failure stays local and never blocks (over-block dual)", async () => {
    stubReviewWorld({
      candidates: [namedCandidate()],
      subgraph: {
        nodes: [
          { id: LEFT, label: "左" },
          { id: RIGHT, label: "右" },
          { id: "e3000000-0000-0000-0000-000000000000", label: "鄰居" },
        ],
        edges: [
          {
            id: "r1000000-0000-0000-0000-000000000000",
            src: LEFT,
            dst: "e3000000-0000-0000-0000-000000000000",
            type: "LOCATED_IN",
          },
          {
            id: "r2000000-0000-0000-0000-000000000000",
            src: RIGHT,
            dst: "e3000000-0000-0000-0000-000000000000",
            type: "LOCATED_IN",
          },
        ],
      },
      failRelation: "neutral",
    });
    renderWithProviders(<ReviewCases project="acme" />);

    await waitFor(() => expect(screen.getAllByText(/原文載入失敗/)).toHaveLength(2));
    expect(screen.getByRole("button", { name: "是,合併" })).toBeEnabled();
  });

  it("deferring the tail case ends the pass instead of forcing a decision (Codex #76 R3)", async () => {
    // a deferred row stays in the queue, so advancing past the LAST case used
    // to clamp straight back onto it — now with the skip button gone, which
    // FORCED a decision on the very pair the curator declined to decide
    stubReviewWorld({ candidates: [namedCandidate()] });
    stubDecision(namedCandidate({ status: "deferred" }));
    renderWithProviders(<ReviewCases project="acme" />);

    fireEvent.click(await screen.findByRole("button", { name: "跳過,下次再問" }));

    expect(await screen.findByText(/這一輪看到底了/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "是,合併" })).not.toBeInTheDocument();
    // the pass can restart deliberately — the case comes back by CHOICE
    fireEvent.click(screen.getByRole("button", { name: "從頭再看一輪" }));
    expect(await screen.findByText("台灣海洋科技館")).toBeInTheDocument();
  });

  it("an entity with no evidenced edges gets an honest empty-context line", async () => {
    stubReviewWorld({ candidates: [namedCandidate()], subgraph: { nodes: [], edges: [] } });
    renderWithProviders(<ReviewCases project="acme" />);

    await waitFor(() => expect(screen.getAllByText(/沒有帶證據的關聯可參考/)).toHaveLength(2));
  });

  it("shows progress and walks the queue without deciding", async () => {
    const second = namedCandidate({
      id: "c2222222-2222-2222-2222-222222222222",
      left_snapshot: { name: "區域探索館", type: "FACILITY" },
      right_snapshot: { name: "區域探索廳", type: "FACILITY" },
    });
    stubReviewWorld({ candidates: [namedCandidate(), second] });
    renderWithProviders(<ReviewCases project="acme" />);

    expect(await screen.findByText("第 1 筆,共 2 筆")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一筆" }));
    expect(await screen.findByText("區域探索館")).toBeInTheDocument();
    expect(screen.getByText("第 2 筆,共 2 筆")).toBeInTheDocument();
  });

  it("explains an empty queue instead of showing a blank page", async () => {
    stubReviewWorld({ candidates: [] });
    renderWithProviders(<ReviewCases project="acme" />);

    expect(await screen.findByText(/目前沒有需要審核的項目/)).toBeInTheDocument();
  });

  it("keeps terminal rows out of the flow even if the server ever sends them", async () => {
    // the queue endpoint excludes approved/rejected today (review.py keeps the
    // list = §19's pending_review gauge); this pins the client-side backstop so
    // a future loosening of that server rule can't feed decided cases back into
    // a flow whose verbs would then be doomed 400s
    stubReviewWorld({
      candidates: [
        namedCandidate({
          id: "c9999999-9999-9999-9999-999999999999",
          status: "approved",
          left_snapshot: { name: "已決定的案子", type: "EVENT" },
        }),
        namedCandidate(),
      ],
    });
    renderWithProviders(<ReviewCases project="acme" />);

    expect(await screen.findByText("第 1 筆,共 1 筆")).toBeInTheDocument();
    expect(screen.queryByText("已決定的案子")).not.toBeInTheDocument();
  });

  it("fails loud when the queue can't be loaded", async () => {
    stubApiError();
    renderWithProviders(<ReviewCases project="acme" />);

    expect(await screen.findByText(/無法載入審核佇列/)).toBeInTheDocument();
  });

  it("fails loud when the active build swaps mid-pagination (Codex #68)", async () => {
    // page 1 is served by the old active build, page 2 by a newly-activated one;
    // concatenating them would show a mixed queue whose stale rows 404 on decide
    const meta = (build_id: string, next_cursor: string | null) => ({
      request_id: "00000000-0000-0000-0000-000000000000",
      build_id,
      elapsed_ms: 1,
      next_cursor,
    });
    let call = 0;
    vi.spyOn(api, "GET").mockImplementation((() => {
      const first = call++ === 0;
      return Promise.resolve({
        data: {
          data: [namedCandidate({ id: first ? CID : "c2222222-2222-2222-2222-222222222222" })],
          meta: first ? meta("b-old", "cursor-2") : meta("b-new", null),
        },
        error: undefined,
      });
    }) as never);
    renderWithProviders(<ReviewCases project="acme" />);

    expect(await screen.findByText(/active build changed/i)).toBeInTheDocument();
  });
});
