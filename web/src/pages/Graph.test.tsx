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
      },
    });
    renderGraph();

    fireEvent.click(await screen.findByRole("button", { name: /ada lovelace/i }));

    expect(await screen.findByText(/hops=9 is outside the policy ceiling/i)).toBeInTheDocument();
    expect(screen.queryByText(/PATCH \/projects\//i)).not.toBeInTheDocument();
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

  it("labels the left filter as covering LOADED entities only, and filters client-side", async () => {
    // The API has no entity search (reject_unsupported_query) — an unlabelled
    // filter box would read as a corpus search and silently lie about anything
    // beyond the loaded pages (FE3's false-affordance lesson).
    vi.spyOn(api, "GET").mockImplementation(((path: string) =>
      Promise.resolve(
        path.includes("subgraph") || path.includes("_id}")
          ? { data: { data: subgraph(), meta: META }, error: undefined }
          : {
              data: {
                data: [
                  entity(),
                  entity({ id: E2, canonical_name: "Charles Babbage", entity_key: "person:cb" }),
                ],
                meta: META,
              },
              error: undefined,
            },
      )) as never);
    renderGraph();

    expect(await screen.findByText(/filters the 2 loaded entities only/i)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/filter/i), { target: { value: "charles" } });
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

    fireEvent.click(await screen.findByRole("button", { name: /load more entities/i }));
    expect(await screen.findByText(/could not load more entities/i)).toBeInTheDocument();
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
    fireEvent.click(await screen.findByRole("button", { name: /load more entities/i }));
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
    fireEvent.click(screen.getByRole("button", { name: /load more entities/i }));

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
    fireEvent.click(screen.getByRole("button", { name: /load more entities/i }));

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
