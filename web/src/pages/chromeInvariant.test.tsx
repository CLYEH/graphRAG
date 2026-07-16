import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Overview } from "./Overview";
import { ProjectHealth } from "./ProjectHealth";
import { JobsDashboard } from "./JobsDashboard";
import { Quality } from "./Quality";
import { ReviewQueue } from "./ReviewQueue";
import { Inspect } from "./Inspect";
import { Clean } from "./Clean";
import { Graph } from "./Graph";
import { Import } from "./Import";
import { Settings } from "./Settings";
import { QueryResults } from "../components/QueryResults";
import { api } from "../api/client";
import {
  build,
  healthReport,
  mergeCandidate,
  projectRoute,
  queryResult,
  retrievalResult,
} from "../test-utils";

import type { ReactElement } from "react";

// UXA3's page-wide invariant (the translation layer's contract): the rendered
// CHROME of every page carries words — never a bare uuid, never a snake_case
// field name. Raw identifiers are allowed exactly two homes: hover titles
// (attributes, not text) and <details> folds (the 進階 escape hatch). The
// fixtures are deliberately uuid-laden so a single regression — one field
// rendered raw — fails the sweep.

const UUID_TEXT = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
// the leak list: §19 count keys and entity/relation field names the audit
// found rendered verbatim (raw store vocabulary, not operator words)
const SNAKE_LABELS =
  /\b(pending_merge_candidates|pending_ontology_proposals|needs_review_entities|needs_review_relations|low_confidence_relations|missing_evidence_relations|builds_total|canonical_name|entity_key|created_by|review_status|source_uri|active_build_id|pending_review)\b/;

function assertChromeClean(root: HTMLElement) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const offenders: string[] = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.textContent ?? "";
    if (!UUID_TEXT.test(text) && !SNAKE_LABELS.test(text)) continue;
    // inside a <details> fold = the sanctioned home for raw values
    let el: HTMLElement | null = node.parentElement;
    let folded = false;
    while (el) {
      if (el.tagName === "DETAILS") {
        folded = true;
        break;
      }
      el = el.parentElement;
    }
    if (!folded) offenders.push(text.trim().slice(0, 80));
  }
  expect(offenders).toEqual([]);
}

const META = {
  request_id: "00000000-0000-0000-0000-000000000000",
  build_id: null,
  elapsed_ms: 1,
  next_cursor: null,
};

const B1 = "b1111111-aaaa-4aaa-8aaa-000000000001";
const E1 = "e1111111-aaaa-4aaa-8aaa-000000000001";

function ok(data: unknown) {
  return Promise.resolve({ data: { data, meta: META }, error: undefined });
}

// One route-aware stub feeding every read a uuid-laden answer.
function stubWorld() {
  vi.spyOn(api, "GET").mockImplementation(((path: string) => {
    switch (path) {
      case "/projects/{project}/health":
        return ok(
          healthReport({
            status: "needs_review",
            active_build_id: B1,
            pending_review: 5,
            counts: {
              documents: 410,
              entities: 1409,
              relations: 1158,
              pending_merge_candidates: 5,
              low_confidence_relations: 149,
            },
          }),
        );
      case "/projects/{project}/sources":
        return ok([
          {
            id: "s1111111-aaaa-4aaa-8aaa-000000000001",
            project: "acme",
            kind: "text",
            uri: "file:///data/corpus",
            metadata: {},
            created_at: "2026-07-01T00:00:00Z",
          },
        ]);
      case "/projects/{project}/builds":
        // the eval block is deliberately uuid/hex-laden (build_id, fingerprint):
        // the 品質 page must confine the raw block to its 進階 fold and render
        // the verdicts as words
        return ok([
          build({
            id: B1,
            status: "active",
            eval: {
              build_id: B1,
              score: 1,
              passed: 1,
              failed: 0,
              fingerprint: "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
              metrics: {},
              cases: [{ question: "海祭是哪一族的祭儀?", mode: "hybrid", score: 1, passed: true }],
            },
            activated_at: "2026-07-13T16:35:46Z",
          }),
        ]);
      case "/jobs/{job_id}":
        // the accepted eval job's snapshot (the 品質 sweep clicks 開始評測):
        // uuid-laden ids must stay in attributes, status renders as words
        return ok({
          job_id: "0c9f7a3e-2f65-4f0a-8a2b-7d1e9c4b5a6f",
          status: "running",
          kind: "eval",
          project: "acme",
          build_id: B1,
          step: null,
          progress: 0.5,
          message: null,
          error: null,
          created_at: "2026-07-01T00:00:00Z",
          finished_at: null,
        });
      case "/projects/{project}/merge-candidates":
        // build_id + entity ids ALIGN with the subgraph stub: a mismatched
        // build would (correctly) trip the review page's scope freeze, and
        // unmatched entity ids would skip the evidenced-relation path — both
        // would hide exactly the uuid-laden renders this sweep must inspect
        return ok([
          mergeCandidate({
            build_id: B1,
            left_entity_id: E1,
            right_entity_id: "e2222222-aaaa-4aaa-8aaa-000000000002",
            left_snapshot: { name: "海祭", type: "EVENT" },
            right_snapshot: { name: "海祭儀式", type: "EVENT" },
          }),
        ]);
      case "/projects/{project}/graph/subgraph":
        // the subgraph meta NAMES the build (that's the provenance line under
        // test on the Graph page)
        return Promise.resolve({
          error: undefined,
          data: {
            meta: { ...META, build_id: B1 },
            data: {
              nodes: [
                { id: E1, label: "海祭", type: "EVENT" },
                {
                  id: "e2222222-aaaa-4aaa-8aaa-000000000002",
                  label: "阿美族",
                  type: "ETHNIC_GROUP",
                },
              ],
              edges: [
                {
                  id: "r1111111-aaaa-4aaa-8aaa-000000000001",
                  src: E1,
                  dst: "e2222222-aaaa-4aaa-8aaa-000000000002",
                  type: "PRACTICED_BY",
                },
              ],
            },
          },
        });
      case "/projects/{project}/entities":
        return ok([
          {
            id: E1,
            project: "acme",
            build_id: B1,
            type: "EVENT",
            canonical_name: "海祭",
            entity_key: "fpv1:deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            attributes: {},
            status: "active",
            review_status: "unreviewed",
            created_at: "2026-07-01T00:00:00Z",
            updated_at: "2026-07-01T00:00:00Z",
            created_by: "llm",
          },
        ]);
      case "/projects/{project}/relations/{relation_id}":
        return ok({
          id: "r1111111-aaaa-4aaa-8aaa-000000000001",
          project: "acme",
          build_id: B1,
          src_entity_id: E1,
          dst_entity_id: "e2222222-aaaa-4aaa-8aaa-000000000002",
          type: "PRACTICED_BY",
          attributes: {},
          status: "active",
          review_status: "unreviewed",
          confidence: 0.9,
          evidence: [
            {
              id: "ev111111-aaaa-4aaa-8aaa-000000000001",
              evidence_type: "chunk",
              quote: "每年5月初由頭目率領族人舉行",
              source_uri: "file:///data/corpus/guide-main_001.txt",
            },
          ],
          created_at: "2026-07-01T00:00:00Z",
          updated_at: "2026-07-01T00:00:00Z",
        });
      case "/projects":
        return ok([
          {
            name: "acme",
            display_name: null,
            description: null,
            config: {
              ontology: { entity_types: ["EVENT"], relation_types: ["PRACTICED_BY"] },
              chunking: { max_chars: 400, overlap: 60 },
            },
            created_at: "2026-07-01T00:00:00Z",
          },
        ]);
      case "/projects/{project}":
        // uuid-laden where the settings page must NOT render them bare:
        // structured_mappings never renders at all, and the sql guardrail
        // block (with a uuid-suffixed table name) is confined to a fold
        return ok({
          name: "acme",
          display_name: null,
          description: null,
          config: {
            ontology: {
              entity_types: ["EVENT"],
              relation_types: ["PRACTICED_BY"],
              proposal_policy: "review",
            },
            chunking: { max_chars: 400, overlap: 60 },
            query_policy: {
              schema_version: "1.0",
              default_mode: "hybrid",
              max_top_k: 10,
              max_graph_hops: 2,
              max_sql_rows: 100,
              max_latency_ms: 15000,
              require_sources: true,
              expose_debug: false,
              text_to_sql: {
                enabled: false,
                readonly: true,
                allowed_tables: ["t_1a2b3c4d-aaaa-4aaa-8aaa-000000000001"],
                blocked_keywords: ["insert", "update", "delete", "drop", "alter", "truncate"],
                max_rows: 100,
                timeout_ms: 10000,
              },
              text_to_cypher: {
                enabled: false,
                readonly: true,
                allowed_clauses: ["MATCH", "WHERE", "RETURN", "LIMIT"],
                blocked: ["CREATE", "MERGE", "DELETE", "SET", "REMOVE", "CALL"],
                max_rows: 100,
                timeout_ms: 10000,
              },
            },
            structured_mappings: {
              exhibits: {
                entities: {
                  ex: {
                    entity_type: "EXHIBIT",
                    name_column: "name",
                    disambiguator_column: "c1111111-aaaa-4aaa-8aaa-000000000001",
                  },
                },
                relations: [],
              },
            },
          },
          created_at: "2026-07-01T00:00:00Z",
        });
      case "/projects/{project}/documents":
        return ok([
          {
            id: "d1111111-aaaa-4aaa-8aaa-000000000001",
            project: "acme",
            build_id: B1,
            source_uri: "file:///data/corpus/guide-main_002.txt",
            mime: "text/plain",
            status: "ingested",
            ingested_at: "2026-07-13T15:35:58Z",
          },
        ]);
      default:
        return ok([]);
    }
  }) as never);
}

function renderPage(ui: ReactElement, section: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, retryDelay: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[projectRoute("acme", section)]}>
        <Routes>
          <Route path={`/p/:project/${section}`} element={ui} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

// The job SSE rides raw fetch (jobStream.ts), which jsdom cannot serve — an
// unstubbed stream failure would surface an environment-specific error line
// (with the raw URL) that no real browser shows. Keep the stream silent.
vi.mock("../api/jobStream", async (importOriginal) => {
  const real = await importOriginal<typeof import("../api/jobStream")>();
  return {
    ...real,
    // a stream that never yields and never errors — the page under sweep is
    // the chrome, not the live progress
    streamJobEvents: () => new Promise<void>(() => {}),
  };
});

describe("chrome invariant — no raw ids or store vocabulary outside folds", () => {
  it("總覽 (Overview)", async () => {
    stubWorld();
    const { container } = renderPage(<Overview />, "overview");
    await screen.findByText(/服務中/);
    assertChromeClean(container);
  });

  it("診斷 (Health)", async () => {
    stubWorld();
    const { container } = renderPage(<ProjectHealth />, "health");
    await screen.findByRole("status");
    assertChromeClean(container);
  });

  it("建置 (Jobs)", async () => {
    stubWorld();
    const { container } = renderPage(<JobsDashboard />, "jobs");
    await screen.findByText("上線中");
    assertChromeClean(container);
  });

  it("審核 (Review)", async () => {
    stubWorld();
    const { container } = renderPage(<ReviewQueue />, "review");
    await screen.findByText("海祭儀式");
    // the shared stub now serves an evidenced edge — wait for BOTH panels'
    // context (the uuid-laden relation payload) to settle before sweeping
    await waitFor(() =>
      expect(screen.getAllByText(/每年5月初由頭目率領族人舉行/).length).toBeGreaterThan(0),
    );
    assertChromeClean(container);
  });

  it("檢視 (Inspect documents)", async () => {
    stubWorld();
    const { container } = renderPage(<Inspect />, "inspect");
    await screen.findByText("guide-main_002.txt");
    assertChromeClean(container);
  });

  it("清洗 (Clean) — including a build-stamped preview", async () => {
    stubWorld();
    vi.spyOn(api, "POST").mockResolvedValue({
      data: {
        data: {
          chunks: [{ ordinal: 0, start: 0, end: 12, token_count: 12, text: "海祭是阿美族的祭儀" }],
          pair: { max_chars: 400, overlap: 60 },
          buildId: B1,
        },
        meta: { ...META, build_id: B1 },
      },
      error: undefined,
    } as never);
    const { container } = renderPage(<Clean />, "clean");
    fireEvent.change(await screen.findByLabelText("文字內容"), {
      target: { value: "海祭是阿美族的祭儀" },
    });
    fireEvent.click(screen.getByRole("button", { name: "預覽" }));
    await screen.findByText(/個切塊/);
    assertChromeClean(container);
  });

  it("圖譜 (Graph) — including a rendered subgraph", async () => {
    stubWorld();
    const { container } = renderPage(<Graph />, "graph");
    fireEvent.click(await screen.findByRole("button", { name: /海祭/ }));
    await screen.findByText(/顯示目前上線中的知識庫/);
    assertChromeClean(container);
  });

  it("匯入 (Import) — including an accepted build job", async () => {
    stubWorld();
    vi.spyOn(api, "POST").mockResolvedValue({
      data: {
        data: { job_id: "0c9f7a3e-2f65-4f0a-8a2b-7d1e9c4b5a6f", status: "queued" },
        meta: META,
      },
      error: undefined,
    } as never);
    const { container } = renderPage(<Import />, "import");
    // the run gate opens only after config+sources settle — clicking a
    // disabled button is a silent no-op (the clickWhenEnabled lesson)
    const runBtn = await screen.findByRole("button", { name: "開始建置" });
    await waitFor(() => expect(runBtn).toBeEnabled());
    fireEvent.click(runBtn);
    await screen.findByText(/建置已排入佇列/);
    assertChromeClean(container);
  });

  it("品質 (Quality) — verdicts as words, raw eval block confined to its fold, incl. a live job", async () => {
    stubWorld();
    vi.spyOn(api, "POST").mockResolvedValue({
      data: {
        data: { job_id: "0c9f7a3e-2f65-4f0a-8a2b-7d1e9c4b5a6f", status: "queued" },
        meta: META,
      },
      error: undefined,
    } as never);
    const { container } = renderPage(<Quality />, "quality");
    // the per-case table (from the uuid-laden eval block) renders as words
    await screen.findByText("海祭是哪一族的祭儀?");
    // run an eval so the accepted-job progress chrome is under the sweep too
    const runBtn = screen.getByRole("button", { name: "開始評測" });
    await waitFor(() => expect(runBtn).toBeEnabled());
    fireEvent.click(runBtn);
    await screen.findByText("評測中");
    assertChromeClean(container);
  });

  it("設定 (Settings) — the uuid-laden guardrail blocks stay inside their folds", async () => {
    stubWorld();
    const { container } = renderPage(<Settings />, "settings");
    await screen.findByText("知識類型");
    assertChromeClean(container);
  });

  it("檢索結果 (QueryResults)", () => {
    const { container } = render(
      <QueryResults
        result={queryResult({
          build_id: B1,
          results: [
            retrievalResult({
              id: E1,
              result_type: "chunk",
              text: "海祭是阿美族的祭儀",
              source_refs: [
                { source_type: "chunk", id: "c1111111-aaaa-4aaa-8aaa-000000000001" },
                { source_type: "entity", id: E1 },
              ],
            }),
          ],
        })}
      />,
    );
    assertChromeClean(container);
  });
});
