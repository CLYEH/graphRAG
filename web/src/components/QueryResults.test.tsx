import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { QueryResults } from "./QueryResults";
import { api } from "../api/client";
import { queryResult, renderWithProviders, retrievalResult } from "../test-utils";

afterEach(() => {
  vi.restoreAllMocks();
});

// jsdom does not toggle <details> from a summary click — set the property and
// dispatch the toggle event React listens for
function openFold() {
  const details = document.querySelector("details");
  if (!details) throw new Error("no fold rendered");
  details.open = true;
  fireEvent(details, new Event("toggle"));
}

const META = {
  request_id: "00000000-0000-0000-0000-000000000000",
  build_id: null,
  elapsed_ms: 1,
  next_cursor: null,
};

const DOC_ID = "aaaaaaaa-1111-2222-3333-444444444444";

describe("QueryResults", () => {
  it("renders a source uri as text, never a clickable link", () => {
    // an untrusted source_uri in an <a href> would be a fresh injection sink
    // (the FE7 class-14 lesson) — it must render as inert text
    renderWithProviders(
      <QueryResults
        result={queryResult({
          results: [
            retrievalResult({
              source_refs: [
                {
                  source_type: "document",
                  id: "aaaaaaaa-1111-2222-3333-444444444444",
                  source_uri: "javascript:alert(1)",
                },
              ],
            }),
          ],
        })}
      />,
    );

    expect(screen.getByText(/javascript:alert\(1\)/)).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows a row citation's full lossless id, not a truncated slice", () => {
    // SQL/row source refs are a lossless table:pk string (core row_source_ref),
    // not a uuid — slicing to 8 chars would hide the pk and make two row
    // citations indistinguishable (Codex #69), breaking §16 traceability
    renderWithProviders(
      <QueryResults
        result={queryResult({
          results: [
            retrievalResult({
              result_type: "row",
              source_refs: [
                {
                  source_type: "row",
                  id: "9:customers:12345",
                  metadata: { table: "customers", pk: "12345" },
                },
              ],
            }),
          ],
        })}
      />,
    );

    expect(screen.getByText("9:customers:12345")).toBeInTheDocument();
  });

  it("shows the routing trace when the debug block is present", () => {
    renderWithProviders(
      <QueryResults
        result={queryResult({
          debug: {
            stores_used: [],
            retrieval_plan: [],
            routing_decision: {
              selected: ["semantic", "graph"],
              skipped: ["sql"],
              reason: "hybrid fan-out",
              confidence: null,
            },
            latency_ms: 12,
          },
        })}
      />,
    );

    expect(screen.getByText(/routing:/i).closest("p")).toHaveTextContent(
      "selected [semantic, graph] · skipped [sql] — hybrid fan-out",
    );
  });

  it("marks a nil-uuid build as degraded rather than showing an id", () => {
    renderWithProviders(
      <QueryResults result={queryResult({ build_id: "00000000-0000-0000-0000-000000000000" })} />,
    );

    expect(screen.getByText(/降級回應/)).toBeInTheDocument();
  });

  // ---- SS2 reference cards --------------------------------------------------

  const docRefResult = () =>
    queryResult({
      results: [
        retrievalResult({
          source_refs: [{ source_type: "document", id: DOC_ID, source_uri: "file:///a.md" }],
        }),
      ],
    });

  it("fires zero resolution requests while the fold is closed", () => {
    // a response can carry hundreds of refs; rendering it must not fan out
    // detail reads the user never asked for — resolution is open-gated
    const spy = vi.spyOn(api, "GET");
    renderWithProviders(<QueryResults result={docRefResult()} project="demo" />);

    expect(screen.getByText(DOC_ID)).toBeInTheDocument(); // verbatim ref still listed
    expect(spy).not.toHaveBeenCalled();
  });

  it("resolves a document ref to its envelope title on open, keeping the verbatim id", async () => {
    // SS2's point: refs become words (DR-010 context.title) — but the raw id
    // stays adjacent, because the card is a translation layer, not a REPLACEMENT
    // of §16's lossless citation (UXA3 rule)
    vi.spyOn(api, "GET").mockImplementation(((path: string) =>
      Promise.resolve(
        path === "/projects/{project}/documents/{document_id}"
          ? {
              data: {
                data: {
                  id: DOC_ID,
                  build_id: "b",
                  source_uri: "file:///a.md",
                  metadata: { context: { title: "海科館導覽手冊" } },
                },
                meta: META,
              },
              error: undefined,
            }
          : { data: { data: [], meta: META }, error: undefined },
      )) as never);
    renderWithProviders(<QueryResults result={docRefResult()} project="demo" />);
    openFold();

    expect(await screen.findByText(/文件:海科館導覽手冊/)).toBeInTheDocument();
    expect(screen.getByText(DOC_ID)).toBeInTheDocument();
  });

  it("degrades an unresolvable ref to the raw line plus an honest miss note", async () => {
    // a ref minted by an older build 404s after activation — the card must
    // say so and the verbatim id must survive (never a blank enrichment)
    vi.spyOn(api, "GET").mockResolvedValue({
      data: undefined,
      error: {
        error: { code: "VALIDATION_ERROR", message: "not found", details: null, request_id: "r" },
      },
      // a real 404 → detailError → DetailScopeGoneError → the hook's retry fn
      // stops immediately (no retry storm for a ref from an older build)
      response: { status: 404 },
    } as never);
    renderWithProviders(<QueryResults result={docRefResult()} project="demo" />);
    openFold();

    expect(await screen.findByText(/無法解析/)).toBeInTheDocument();
    expect(screen.getByText(DOC_ID)).toBeInTheDocument();
  });

  it("resolves a relation ref to endpoint names via the cached entity reads", async () => {
    // the relation card chains three reads (relation → src/dst entities); the
    // names must land as words while the verbatim relation id stays adjacent
    const REL_ID = "bbbbbbbb-1111-2222-3333-444444444444";
    const SRC_ID = "cccccccc-1111-2222-3333-444444444444";
    const DST_ID = "dddddddd-1111-2222-3333-444444444444";
    const entities: Record<string, { id: string; canonical_name: string; type: string }> = {
      [SRC_ID]: { id: SRC_ID, canonical_name: "Alice", type: "Person" },
      [DST_ID]: { id: DST_ID, canonical_name: "Acme", type: "Company" },
    };
    vi.spyOn(api, "GET").mockImplementation(((path: string, opts: never) => {
      const params = (opts as { params: { path: Record<string, string> } }).params.path;
      if (path === "/projects/{project}/relations/{relation_id}")
        return Promise.resolve({
          data: {
            data: {
              id: REL_ID,
              build_id: "b",
              src_entity_id: SRC_ID,
              dst_entity_id: DST_ID,
              type: "WORKS_AT",
              status: "active",
            },
            meta: META,
          },
          error: undefined,
        });
      if (path === "/projects/{project}/entities/{entity_id}")
        return Promise.resolve({
          data: { data: entities[params.entity_id], meta: META },
          error: undefined,
        });
      return Promise.resolve({ data: { data: [], meta: META }, error: undefined });
    }) as never);
    renderWithProviders(
      <QueryResults
        result={queryResult({
          results: [retrievalResult({ source_refs: [{ source_type: "relation", id: REL_ID }] })],
        })}
        project="demo"
      />,
    );
    openFold();

    expect(await screen.findByText(/Alice —WORKS_AT→ Acme/)).toBeInTheDocument();
    expect(screen.getByText(REL_ID)).toBeInTheDocument();
  });

  it("parses stable chunk refs and row metadata into words without any fetch", () => {
    // chunk:<hash>:<ordinal> is rebuild-stable, not detail-addressable; row
    // refs already carry {table, pk} metadata — both resolve client-side only
    const spy = vi.spyOn(api, "GET");
    renderWithProviders(
      <QueryResults
        result={queryResult({
          results: [
            retrievalResult({
              source_refs: [
                { source_type: "chunk", id: "chunk:abc123def456:4" },
                {
                  source_type: "row",
                  id: "9:customers:12345",
                  metadata: { table: "customers", pk: "12345" },
                },
              ],
            }),
          ],
        })}
        project="demo"
      />,
    );
    openFold();

    expect(screen.getByText(/段落 #4 · 內容雜湊 abc123def456/)).toBeInTheDocument();
    expect(screen.getByText(/資料表 customers · 主鍵 12345/)).toBeInTheDocument();
    expect(screen.getByText("9:customers:12345")).toBeInTheDocument(); // lossless id intact
    expect(spy).not.toHaveBeenCalled();
  });
});
