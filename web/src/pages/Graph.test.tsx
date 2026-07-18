import { fireEvent, screen, waitFor } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Graph, radialLayout } from "./Graph";
import { api } from "../api/client";
import { projectRoute, renderWithProviders } from "../test-utils";

const META = {
  request_id: "00000000-0000-0000-0000-000000000000",
  build_id: "b1" as string | null,
  elapsed_ms: 1,
  next_cursor: null as string | null,
};

const E1 = "e1000000-0000-4000-8000-000000000001";
const E2 = "e2000000-0000-4000-8000-000000000002";
const R1 = "r1000000-0000-4000-8000-000000000001";

function entity(overrides: Record<string, unknown> = {}) {
  return {
    id: E1,
    build_id: "b1",
    type: "PERSON",
    canonical_name: "Ada Lovelace",
    entity_key: "person:ada",
    status: "active",
    review_status: "unreviewed",
    created_by: "pipeline",
    attributes: {},
    ...overrides,
  };
}

function relationDetail() {
  return {
    id: R1,
    build_id: "b1",
    src_entity_id: E1,
    dst_entity_id: E2,
    type: "WORKS_WITH",
    status: "active",
    review_status: "approved",
    created_by: "pipeline",
    confidence: 0.87,
    evidence: [
      {
        id: "ev000000-0000-4000-8000-000000000001",
        evidence_type: "chunk",
        quote: "Ada worked with Charles on the Engine.",
        source_uri: "file:///corpus/ada.txt",
      },
    ],
  };
}

function subgraph() {
  return {
    nodes: [
      { id: E1, type: "PERSON", label: "Ada Lovelace", properties: {} },
      { id: E2, type: "PERSON", label: "Charles Babbage", properties: {} },
    ],
    edges: [{ id: R1, src: E1, dst: E2, type: "WORKS_WITH", properties: {} }],
  };
}

/** Route the GET mock by path — list, subgraph, entity detail, relation detail. */
function stubApi(overrides: Partial<Record<string, unknown>> = {}) {
  return vi.spyOn(api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/graph/subgraph"))
      return Promise.resolve(
        overrides["subgraph"] ?? { data: { data: subgraph(), meta: META }, error: undefined },
      );
    if (path.includes("{entity_id}"))
      return Promise.resolve({ data: { data: entity(), meta: META }, error: undefined });
    if (path.includes("{relation_id}"))
      return Promise.resolve({ data: { data: relationDetail(), meta: META }, error: undefined });
    return Promise.resolve({ data: { data: [entity()], meta: META }, error: undefined });
  }) as never);
}

function renderGraph() {
  return renderWithProviders(
    <Routes>
      <Route path="/p/:project/graph" element={<Graph />} />
    </Routes>,
    { route: projectRoute("acme", "graph") },
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("radialLayout", () => {
  it("centers the picked entity and rings neighbors by BFS depth", () => {
    // The layout is the page's only geometry — if the center drifts or a
    // neighbor lands on the center, every click target lies about identity.
    const g = subgraph();
    const placed = radialLayout(g, E1);
    const center = placed.find((p) => p.id === E1);
    const other = placed.find((p) => p.id === E2);
    expect(center).toMatchObject({ x: 320, y: 240 }); // W/2, H/2
    expect(other).toBeDefined();
    expect(Math.hypot((other?.x ?? 0) - 320, (other?.y ?? 0) - 240)).toBeGreaterThan(10);
  });

  it("places nodes the edge set never reaches instead of dropping them", () => {
    // The server may return nodes whose connecting edges were cut by the row
    // ceiling. Dropping them would misreport the neighborhood; they belong on
    // an outer ring, visible.
    const g = {
      nodes: [...subgraph().nodes, { id: "e3", type: null, label: "orphan", properties: {} }],
      edges: subgraph().edges,
    };
    const placed = radialLayout(g, E1);
    expect(placed.map((p) => p.id)).toContain("e3");
  });
});

describe("Graph", () => {
  it("names the missing query_policy condition with configuration guidance — not a generic failure", async () => {
    // The subgraph endpoint is §21-governed and REFUSES an unconfigured project
    // (details.query_policy="missing", read from api/routers/inspect.py). An
    // operator who sees a generic error will retry; one who sees the NAMED
    // condition will PATCH the config. The discriminating half: a DIFFERENT
    // 400 must NOT show the guidance.
    stubApi({
      subgraph: {
        data: undefined,
        error: {
          error: {
            code: "VALIDATION_ERROR",
            message: "project 'acme' has no query_policy configured",
            details: { query_policy: "missing" },
          },
        },
        response: { status: 400 },
      },
    });
    renderGraph();

    fireEvent.click(await screen.findByRole("button", { name: /ada lovelace/i }));

    expect(await screen.findByText(/§21-governed — there is no default/i)).toBeInTheDocument();
    expect(screen.getByText(/PATCH \/projects\//i)).toBeInTheDocument();
  });

  it("shows a plain rejection for a NON-policy 400 — e.g. hops beyond the ceiling", async () => {
    // hops are REJECTED, not clamped (the C6c doctrine) — the server's own
    // message names the ceiling and must reach the operator verbatim, without
    // the policy-missing guidance appearing.
    stubApi({
      subgraph: {
        data: undefined,
        error: {
          error: {
            code: "VALIDATION_ERROR",
            message: "hops=9 is outside the policy ceiling 1..3",
            details: null,
          },
        },
        response: { status: 400 },
      },
    });
    renderGraph();

    fireEvent.click(await screen.findByRole("button", { name: /ada lovelace/i }));

    expect(await screen.findByText(/hops=9 is outside the policy ceiling/i)).toBeInTheDocument();
    expect(screen.queryByText(/PATCH \/projects\//i)).not.toBeInTheDocument();
    // the over-block dual of the page-wide scope teardown: a user-input 400 is
    // LOCAL — the entity list must survive it
    expect(screen.getByRole("button", { name: /ada lovelace/i })).toBeInTheDocument();
  });

  it("renders the subgraph and fetches an edge's detail on click — evidence rides ONLY the detail GET", async () => {
    // Relation.evidence[] is omitted from every list frame (same licensing as
    // Document.raw in FE3): the edge click must be a real fetch, and §10.2's
    // named edge fields — type/confidence/evidence(quote+來源)/created_by/
    // review_status — must all come from that settled answer.
    const get = stubApi();
    renderGraph();

    fireEvent.click(await screen.findByRole("button", { name: /ada lovelace/i }));
    expect(await screen.findByText("WORKS_WITH")).toBeInTheDocument(); // edge label in the svg

    fireEvent.click(screen.getByText("WORKS_WITH"));

    expect(await screen.findByText(/ada worked with charles/i)).toBeInTheDocument(); // quote
    expect(screen.getByText("file:///corpus/ada.txt")).toBeInTheDocument(); // 來源
    expect(screen.getByText("0.87")).toBeInTheDocument(); // confidence
    expect(screen.getByText("approved")).toBeInTheDocument(); // review_status
    const detailCalls = get.mock.calls.filter((c) =>
      String((c as unknown[])[0]).includes("{relation_id}"),
    );
    expect(detailCalls.length).toBe(1);
  });

  it("shows entity fields when a node is clicked", async () => {
    stubApi();
    renderGraph();

    fireEvent.click(await screen.findByRole("button", { name: /ada lovelace/i }));

    // picking from the left selects the node too; the detail column shows the
    // entity's identity fields from the settled detail read
    expect(await screen.findByText("person:ada")).toBeInTheDocument(); // entity_key
    expect(screen.getByText("active", { selector: "dd" })).toBeInTheDocument();
  });

  it("searches server-side (SS1b): sends q to GET /entities and shows the exact match total", async () => {
    // SS1b rewired the left column from a client-side over-loaded-pages filter to
    // a REAL server-side search. DISCRIMINATING setup: the searched row (Charles)
    // is NOT in the initial (no-q) load — a client-side filter over the loaded
    // rows [Ada] by "charles" would show NOTHING, so Charles appearing PROVES the
    // term went to the server and the rows came back from it. The count shown is
    // the SERVER's exact total, not a loaded-pages caveat.
    const get = vi.spyOn(api, "GET").mockImplementation(((path: string, opts?: unknown) => {
      if (path.includes("subgraph") || path.includes("_id}"))
        return Promise.resolve({ data: { data: subgraph(), meta: META }, error: undefined });
      const q = (opts as { params?: { query?: { q?: string } } } | undefined)?.params?.query?.q;
      const rows = q
        ? [entity({ id: E2, canonical_name: "Charles Babbage", entity_key: "person:cb" })]
        : [entity()];
      return Promise.resolve({
        data: { data: rows, meta: { ...META, total: rows.length } },
        error: undefined,
      });
    }) as never);
    renderGraph();

    // the honest count is the SERVER's total over the whole active build
    expect(await screen.findByText(/active build 全部知識點:1 個/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /ada lovelace/i })).toBeInTheDocument();
    // the box caps at the server's q max_length (256): a longer paste would 400
    // and GraphBody's error return would hide the box, stranding the user (P2)
    expect(screen.getByLabelText("搜尋")).toHaveAttribute("maxlength", "256");

    fireEvent.change(screen.getByLabelText("搜尋"), { target: { value: "charles" } });

    // the debounced term reaches the SERVER as ?q=charles (not a client filter)
    await waitFor(() =>
      expect(
        get.mock.calls.some(
          (c) =>
            String(c[0]).endsWith("/entities") &&
            (c[1] as { params?: { query?: { q?: string } } } | undefined)?.params?.query?.q ===
              "charles",
        ),
      ).toBe(true),
    );
    // ...and the list renders the server's narrowed response + its exact total —
    // Charles was never in the client's loaded rows, so this is not a local filter
    expect(await screen.findByText(/符合「charles」的知識點:1 個/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /charles babbage/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /ada lovelace/i })).not.toBeInTheDocument();
  });

  it("keeps the loaded entities when a load-more fails scope-neutrally, drops them when the scope is GONE", async () => {
    // FE3's keep-rows rule applies to this list too (Codex, #75 — the guard was
    // restated here with half missing): a 503 on page 2 says nothing about the
    // build, so discarding a usable column over one flaky page is worse than the
    // failure reported; a NO_ACTIVE_BUILD on page 2 proves every loaded row's
    // build is gone. Both directions in one test so the predicate can't drift.
    const get = vi.spyOn(api, "GET");
    get.mockResolvedValueOnce({
      data: { data: [entity()], meta: { ...META, next_cursor: "c2" } },
      error: undefined,
    } as never);
    get.mockResolvedValueOnce({
      data: undefined,
      error: { error: { code: "STORE_UNAVAILABLE", message: "qdrant down" } },
    } as never);
    const view1 = renderGraph();

    fireEvent.click(await screen.findByRole("button", { name: /載入更多/ }));
    expect(await screen.findByText(/載入更多失敗/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /ada lovelace/i })).toBeInTheDocument(); // kept

    // the scope-gone direction: the WHOLE page fails closed, not just the column
    view1.unmount();
    get.mockReset();
    get.mockResolvedValueOnce({
      data: { data: [entity()], meta: { ...META, next_cursor: "c2" } },
      error: undefined,
    } as never);
    get.mockResolvedValueOnce({
      data: undefined,
      error: { error: { code: "NO_ACTIVE_BUILD", message: "no active build" } },
    } as never);
    const view2 = renderGraph();
    const col = view2.container;
    fireEvent.click(await screen.findByRole("button", { name: /載入更多/ }));
    expect(
      await screen.findByText(/could not load entities: no active build/i),
    ).toBeInTheDocument();
    expect(col.querySelectorAll(".graph__entity").length).toBe(0); // dropped
  });

  it("tears down the CACHED subgraph and detail when the entity list loses the scope", async () => {
    // The page-level half of the scope-gone rule (Codex, #75): the viz and
    // detail columns hold their own cached queries, which an entity-list error
    // does not invalidate — without the page-wide verdict the screen would say
    // "no active build" beside the OLD build's rendered graph.
    const get = stubApi();
    // entities page 1 with a cursor so load-more exists
    get.mockImplementation(((path: string) => {
      if (path.endsWith("/graph/subgraph"))
        return Promise.resolve({ data: { data: subgraph(), meta: META }, error: undefined });
      if (path.includes("{entity_id}"))
        return Promise.resolve({ data: { data: entity(), meta: META }, error: undefined });
      if (path.includes("{relation_id}"))
        return Promise.resolve({ data: { data: relationDetail(), meta: META }, error: undefined });
      return Promise.resolve({
        data: { data: [entity()], meta: { ...META, next_cursor: "c2" } },
        error: undefined,
      });
    }) as never);
    renderGraph();
    fireEvent.click(await screen.findByRole("button", { name: /ada lovelace/i }));
    expect(await screen.findByText("WORKS_WITH")).toBeInTheDocument(); // graph up

    get.mockImplementation(((path: string) => {
      if (path.endsWith("/entities"))
        return Promise.resolve({
          data: undefined,
          error: { error: { code: "NO_ACTIVE_BUILD", message: "no active build" } },
        });
      return new Promise(() => {}); // nothing else answers
    }) as never);
    fireEvent.click(screen.getByRole("button", { name: /載入更多/ }));

    expect(
      await screen.findByText(/could not load entities: no active build/i),
    ).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("WORKS_WITH")).not.toBeInTheDocument());
    expect(screen.queryByText("person:ada")).not.toBeInTheDocument(); // detail gone too
  });

  it("tears the whole page down when a load-more SUCCEEDS from a different build", async () => {
    // The splice sibling of the scope-gone teardown (Codex, #75 round 3): a page-2
    // success served by a NEW build proves the world changed under the page — the
    // cached subgraph/detail describe the old build exactly as much as a spliced
    // list would. One verdict, all three columns.
    const get = stubApi();
    get.mockImplementation(((path: string) => {
      if (path.endsWith("/graph/subgraph"))
        return Promise.resolve({ data: { data: subgraph(), meta: META }, error: undefined });
      if (path.includes("_id}"))
        return Promise.resolve({ data: { data: entity(), meta: META }, error: undefined });
      return Promise.resolve({
        data: { data: [entity()], meta: { ...META, next_cursor: "c2" } },
        error: undefined,
      });
    }) as never);
    renderGraph();
    fireEvent.click(await screen.findByRole("button", { name: /ada lovelace/i }));
    expect(await screen.findByText("WORKS_WITH")).toBeInTheDocument();

    get.mockImplementation(((path: string) => {
      if (path.endsWith("/entities"))
        return Promise.resolve({
          data: {
            data: [entity({ id: E2, canonical_name: "New Build Row" })],
            meta: { ...META, build_id: "b2", next_cursor: null },
          },
          error: undefined,
        });
      return new Promise(() => {});
    }) as never);
    fireEvent.click(screen.getByRole("button", { name: /載入更多/ }));

    expect(await screen.findByText(/would mix two builds/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("WORKS_WITH")).not.toBeInTheDocument());
  });

  it("drops a selection the new neighborhood no longer contains — detail must not outlive the view", async () => {
    // Reconciliation is a COMPARISON against the returned graph (the FE2 lesson),
    // so hops shrink, recenter and refetch are all one predicate: select the edge
    // at hops=1, then shrink the neighborhood to the bare center — the evidence
    // panel must leave with the edge, not keep describing something off-screen.
    let bare = false;
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path.endsWith("/graph/subgraph"))
        return Promise.resolve({
          data: {
            data: bare
              ? {
                  nodes: [{ id: E1, type: "PERSON", label: "Ada Lovelace", properties: {} }],
                  edges: [],
                }
              : subgraph(),
            meta: META,
          },
          error: undefined,
        });
      if (path.includes("{relation_id}"))
        return Promise.resolve({ data: { data: relationDetail(), meta: META }, error: undefined });
      if (path.includes("{entity_id}"))
        return Promise.resolve({ data: { data: entity(), meta: META }, error: undefined });
      return Promise.resolve({ data: { data: [entity()], meta: META }, error: undefined });
    }) as never);
    renderGraph();
    fireEvent.click(await screen.findByRole("button", { name: /ada lovelace/i }));
    fireEvent.click(await screen.findByText("WORKS_WITH"));
    expect(await screen.findByText(/ada worked with charles/i)).toBeInTheDocument();

    bare = true; // the next neighborhood no longer contains the selected edge
    fireEvent.change(screen.getByLabelText(/hops/i), { target: { value: "3" } });

    await waitFor(() =>
      expect(screen.queryByText(/ada worked with charles/i)).not.toBeInTheDocument(),
    );
  });

  it("tears the page down when the SUBGRAPH proves the scope is gone", async () => {
    // Clicking a build-A row after build B activates answers NO_ACTIVE_BUILD (or
    // a seed 404) from the subgraph — proof the listed rows are stale. That must
    // not render as a local viz error beside a still-clickable stale list
    // (Codex, #75 round 6); the classification is at throw time, by CODE for the
    // deliberate scope codes and by STATUS for the coarse seed 404.
    stubApi({
      subgraph: {
        data: undefined,
        error: { error: { code: "NO_ACTIVE_BUILD", message: "no active build", details: null } },
        response: { status: 409 },
      },
    });
    renderGraph();

    fireEvent.click(await screen.findByRole("button", { name: /ada lovelace/i }));

    expect(await screen.findByText(/build likely changed under it/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /ada lovelace/i })).not.toBeInTheDocument(),
    );
  });

  it("tears the page down when a DETAIL read 404s — the proof arrives one request later", async () => {
    // A node/edge click does NOT refetch the subgraph, so a build swap can first
    // surface as the detail's 404 (the detail endpoints return rows regardless of
    // lifecycle status — the only 404 is id-absent-from-build). Local rendering
    // would leave a stale, clickable list beside an honest-sounding panel note.
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path.endsWith("/graph/subgraph"))
        return Promise.resolve({ data: { data: subgraph(), meta: META }, error: undefined });
      if (path.includes("{entity_id}"))
        return Promise.resolve({
          data: undefined,
          error: { error: { code: "VALIDATION_ERROR", message: "entity not found" } },
          response: { status: 404 },
        });
      return Promise.resolve({ data: { data: [entity()], meta: META }, error: undefined });
    }) as never);
    renderGraph();

    fireEvent.click(await screen.findByRole("button", { name: /ada lovelace/i }));

    expect(await screen.findByText(/build likely changed under it/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /ada lovelace/i })).not.toBeInTheDocument(),
    );
  });

  it("tears the page down when a DETAIL read answers NO_ACTIVE_BUILD", async () => {
    // The 409 sibling of the detail-404 proof (the code-vs-status pair, both
    // directions now covered on the detail path): deactivate-to-nothing plus a
    // click before any refetch lands here first.
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path.endsWith("/graph/subgraph"))
        return Promise.resolve({ data: { data: subgraph(), meta: META }, error: undefined });
      if (path.includes("{entity_id}"))
        return Promise.resolve({
          data: undefined,
          error: { error: { code: "NO_ACTIVE_BUILD", message: "no active build" } },
          response: { status: 409 },
        });
      return Promise.resolve({ data: { data: [entity()], meta: META }, error: undefined });
    }) as never);
    renderGraph();

    fireEvent.click(await screen.findByRole("button", { name: /ada lovelace/i }));

    expect(await screen.findByText(/build likely changed under it/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /ada lovelace/i })).not.toBeInTheDocument(),
    );
  });

  it("disables non-active entities as seeds and says why", async () => {
    // The list returns EVERY status in the build (the router has no predicate)
    // but the subgraph endpoint only accepts ACTIVE seeds — a merged row that
    // looks clickable can only 404. It stays LISTED (real build content) but
    // disabled, with the status visible.
    vi.spyOn(api, "GET").mockImplementation((() =>
      Promise.resolve({
        data: {
          data: [entity(), entity({ id: E2, canonical_name: "Old Merged", status: "merged" })],
          meta: META,
        },
        error: undefined,
      })) as never);
    renderGraph();

    const merged = await screen.findByRole("button", { name: /old merged/i });
    expect(merged).toBeDisabled();
    expect(merged).toHaveTextContent("merged");
    expect(screen.getByRole("button", { name: /ada lovelace/i })).toBeEnabled();
  });

  it("hides a stale subgraph while a refetch re-verifies the active build", async () => {
    // The FE3/class-17 rule applied to the viz: the subgraph belongs to the
    // active build, a refocus refetch re-asks which build that is, and until it
    // answers the cached picture is exactly the thing being verified. A HUNG
    // refetch must not leave the old graph on display.
    const { focusManager } = await import("@tanstack/react-query");
    const { act } = await import("@testing-library/react");
    const get = stubApi();
    renderGraph();
    fireEvent.click(await screen.findByRole("button", { name: /ada lovelace/i }));
    expect(await screen.findByText("WORKS_WITH")).toBeInTheDocument();

    get.mockImplementation((() => new Promise(() => {})) as never); // everything hangs
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });

    await waitFor(() => expect(screen.queryByText("WORKS_WITH")).not.toBeInTheDocument());
    expect(screen.getAllByText(/loading/i).length).toBeGreaterThan(0);
  });

  it("hides the DETAIL when the subgraph fails scope-neutrally while the detail itself succeeds", async () => {
    // The discriminating shape of Codex's round-7 finding: a focus refetch where
    // the SUBGRAPH hits STORE_UNAVAILABLE (settled error — react-query keeps its
    // previous data, the viz shows a local error) while the DETAIL refetch
    // SUCCEEDS. If visibleSelection were derived from the cached graph, the
    // right column would keep showing evidence for an item that is no longer
    // verifiably on screen. The graph must count as ABSENT unless the subgraph
    // query is settled-successful — the detail's own gates cannot catch this
    // case (its read is green), which is why the earlier hung-everything version
    // of this pin was NOT discriminating.
    const { focusManager } = await import("@tanstack/react-query");
    const { act } = await import("@testing-library/react");
    const get = stubApi();
    renderGraph();
    fireEvent.click(await screen.findByRole("button", { name: /ada lovelace/i }));
    fireEvent.click(await screen.findByText("WORKS_WITH"));
    expect(await screen.findByText(/ada worked with charles/i)).toBeInTheDocument();

    get.mockImplementation(((path: string) => {
      if (path.endsWith("/graph/subgraph"))
        return Promise.resolve({
          data: undefined,
          error: { error: { code: "STORE_UNAVAILABLE", message: "neo4j down" } },
          response: { status: 503 },
        });
      if (path.includes("{relation_id}"))
        return Promise.resolve({ data: { data: relationDetail(), meta: META }, error: undefined });
      if (path.includes("{entity_id}"))
        return Promise.resolve({ data: { data: entity(), meta: META }, error: undefined });
      return Promise.resolve({ data: { data: [entity()], meta: META }, error: undefined });
    }) as never);
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });

    expect(await screen.findByText(/could not load the subgraph: neo4j down/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText(/ada worked with charles/i)).not.toBeInTheDocument(),
    );
    // and the page did NOT tear down — a 503 is scope-neutral, the list survives
    expect(screen.getByRole("button", { name: /ada lovelace/i })).toBeInTheDocument();
  });

  it("sends only entity_id and hops to the subgraph endpoint — never sort or filter", async () => {
    // reject_unsupported_query 400s stray params on the inspect surface; the
    // invariant lives in the fetcher's own query object (the FE3 false-green
    // lesson: assert on the recorded request, not the happy render).
    const get = stubApi();
    renderGraph();

    fireEvent.click(await screen.findByRole("button", { name: /ada lovelace/i }));
    await screen.findByText("WORKS_WITH");

    const call = get.mock.calls.find((c) => String((c as unknown[])[0]).includes("subgraph"));
    const query = (call as unknown as [string, { params: { query: Record<string, unknown> } }])[1]
      .params.query;
    expect(Object.keys(query).sort()).toEqual(["entity_id", "hops"]);
  });
});
