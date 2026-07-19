import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

import { api } from "./api/client";
import { encodeProjectSegment } from "./project/projectRoute";

import type { ReactElement } from "react";
import type {
  Build,
  Entity,
  HealthReport,
  Job,
  MergeCandidate,
  Project,
  QueryResult,
  Relation,
  Source,
} from "./api/queries";

type RetrievalResult = QueryResult["results"][number];

// Builds the encoded route for a project key, so tests exercise the real
// encode/decode path rather than hardcoding a raw `/p/<key>` segment.
export function projectRoute(key: string, section = "health") {
  return `/p/${encodeProjectSegment(key)}/${section}`;
}

export function renderWithProviders(ui: ReactElement, { route = "/" }: { route?: string } = {}) {
  // retry: false covers hooks without a per-query retry; the scope-aware hooks
  // (useSubgraph/useRelation/useEntity) OVERRIDE it with their own retry fn
  // (Codex #76 R6), so retryDelay: 0 keeps their neutral-error retries instant
  // in tests instead of walking real backoff.
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, retryDelay: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const META = {
  request_id: "00000000-0000-0000-0000-000000000000",
  build_id: null,
  elapsed_ms: 1,
  next_cursor: null,
};

const HEALTH_PATH = "/projects/{project}/health";

// The typed client binds globalThis.fetch at construction, so tests mock the
// client method rather than the global fetch — this also keeps the
// query→component contract (envelope unwrapping) under test. `as never` sidesteps
// openapi-fetch's overloaded GET signature. The mock routes by path so a view
// that lands on the health route (which fetches /health) gets a valid report
// instead of the projects envelope; pass `health` to override the default.
export function stubProjects(projects: Project[], health: HealthReport = healthReport()) {
  return vi
    .spyOn(api, "GET")
    .mockImplementation(((path: string) =>
      Promise.resolve(
        path === HEALTH_PATH
          ? { data: { data: health, meta: META }, error: undefined }
          : { data: { data: projects, meta: META }, error: undefined },
      )) as never);
}

// Feeds the /projects query one call per page, chaining next_cursor across pages
// (null on the last) — so tests can prove the switcher pages through, not just
// page 1. The interleaved /health call is answered separately and does not
// advance the page cursor.
export function stubProjectsPages(pages: Project[][], health: HealthReport = healthReport()) {
  let call = 0;
  return vi.spyOn(api, "GET").mockImplementation(((path: string) => {
    if (path === HEALTH_PATH)
      return Promise.resolve({ data: { data: health, meta: META }, error: undefined });
    const i = call++;
    const next = i < pages.length - 1 ? `cursor-${i + 1}` : null;
    return Promise.resolve({
      data: { data: pages[i] ?? [], meta: { ...META, next_cursor: next } },
      error: undefined,
    });
  }) as never);
}

const errorEnvelope = {
  data: undefined,
  error: {
    error: {
      code: "STORE_UNAVAILABLE",
      message: "down",
      details: null,
      request_id: META.request_id,
    },
  },
};

export function stubProjectsError() {
  return vi.spyOn(api, "GET").mockResolvedValue(errorEnvelope as never);
}

// Generic GET-error stub (any endpoint) for the FE8 fail-loud tests.
export function stubApiError() {
  return vi.spyOn(api, "GET").mockResolvedValue(errorEnvelope as never);
}

export function healthReport(overrides: Partial<HealthReport> = {}): HealthReport {
  return {
    status: "healthy",
    active_build_id: null,
    counts: {},
    pending_review: 0,
    drift: null,
    warnings: [],
    ...overrides,
  };
}

export function stubHealth(report: HealthReport) {
  return vi
    .spyOn(api, "GET")
    .mockResolvedValue({ data: { data: report, meta: META }, error: undefined } as never);
}

export function stubHealthError() {
  return vi.spyOn(api, "GET").mockResolvedValue(errorEnvelope as never);
}

export function project(name: string, displayName?: string): Project {
  return {
    name,
    display_name: displayName ?? null,
    description: null,
    config: {},
    created_at: "2026-07-01T00:00:00Z",
  };
}

export function build(overrides: Partial<Build> = {}): Build {
  return {
    id: "b0000000-0000-0000-0000-000000000000",
    project: "acme",
    status: "ready",
    config_hash: null,
    source_hash: null,
    started_at: "2026-07-01T00:00:00Z",
    finished_at: null,
    activated_at: null,
    metrics: null,
    eval: null,
    ...overrides,
  };
}

export function job(overrides: Partial<Job> = {}): Job {
  return {
    job_id: "0c9f7a3e-2f65-4f0a-8a2b-7d1e9c4b5a6f",
    status: "running",
    kind: "build",
    project: "acme",
    build_id: null,
    step: null,
    progress: 0,
    message: null,
    error: null,
    created_at: "2026-07-01T00:00:00Z",
    finished_at: null,
    ...overrides,
  };
}

// Single-purpose GET stubs for the FE8 component tests (each component fetches
// exactly one resource in isolation, so a flat mockResolvedValue is enough).
export function stubBuilds(builds: Build[]) {
  return vi
    .spyOn(api, "GET")
    .mockResolvedValue({ data: { data: builds, meta: META }, error: undefined } as never);
}

export function stubJob(j: Job) {
  return vi
    .spyOn(api, "GET")
    .mockResolvedValue({ data: { data: j, meta: META }, error: undefined } as never);
}

export function mergeCandidate(overrides: Partial<MergeCandidate> = {}): MergeCandidate {
  return {
    id: "c0000000-0000-0000-0000-000000000000",
    project: "acme",
    build_id: "b0000000-0000-0000-0000-000000000000",
    left_entity_id: "e1000000-0000-0000-0000-000000000000",
    right_entity_id: "e2000000-0000-0000-0000-000000000000",
    score: 0.9,
    status: "pending",
    decision: null,
    decided_by: null,
    decided_at: null,
    reason: null,
    impact: null,
    left_snapshot: null,
    right_snapshot: null,
    ...overrides,
  };
}

export function stubMergeCandidates(candidates: MergeCandidate[]) {
  return vi
    .spyOn(api, "GET")
    .mockResolvedValue({ data: { data: candidates, meta: META }, error: undefined } as never);
}

// GOV2-fe: a needs_review entity for the review-queue tests.
export function entity(overrides: Partial<Entity> = {}): Entity {
  return {
    id: "e1000000-0000-0000-0000-000000000000",
    project: "acme",
    build_id: "b0000000-0000-0000-0000-000000000000",
    type: "EVENT",
    canonical_name: "海祭",
    entity_key: "fpv1:deadbeef",
    attributes: {},
    status: "needs_review",
    review_status: "unreviewed",
    created_by: "llm",
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    ...overrides,
  };
}

// GOV2-fe-2: a needs_review relation for the relation-review tests.
export function relation(overrides: Partial<Relation> = {}): Relation {
  return {
    id: "r1000000-0000-0000-0000-000000000000",
    project: "acme",
    build_id: "b0000000-0000-0000-0000-000000000000",
    src_entity_id: "e1000000-0000-0000-0000-000000000000",
    dst_entity_id: "e2000000-0000-0000-0000-000000000000",
    type: "PRACTICED_BY",
    attributes: {},
    relation_signature: "fpv1:rel-deadbeef",
    status: "needs_review",
    review_status: "unreviewed",
    created_by: "llm",
    confidence: 0.9,
    evidence: [],
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    ...overrides,
  };
}

export type SubgraphStub = {
  nodes: { id: string; label?: string | null }[];
  edges: { id: string; src: string; dst: string; type: string }[];
};

// Route-aware GET stub for the UXA1 review flow: the case card fans out to
// three endpoints (queue, subgraph-per-entity, relation detail), so a single
// mockResolvedValue would feed queue-shaped data to the context fetches — the
// contract marks GraphContext.edges required, and the component rightly
// trusts that instead of null-guarding an impossible shape.
export function stubReviewWorld({
  candidates,
  subgraph = { nodes: [], edges: [] },
  relation,
  failSubgraph = false,
  subgraphBuildId = null,
  failRelation = false,
}: {
  candidates: MergeCandidate[];
  subgraph?: SubgraphStub;
  relation?: { id: string; evidence?: { id: string; evidence_type: string; quote?: string }[] };
  /** false = succeed; "neutral" = 503 store outage (scope-neutral);
   *  "scope" = 404 (SubgraphScopeError: build swap / seed gone). */
  failSubgraph?: false | "neutral" | "scope";
  /** meta.build_id stamped on the subgraph response; null = unnamed (no proof). */
  subgraphBuildId?: string | null;
  /** false = succeed; "neutral" = 503 outage; "scope" = 404 (DetailScopeGoneError). */
  failRelation?: false | "neutral" | "scope";
}) {
  return vi.spyOn(api, "GET").mockImplementation(((path: string) => {
    if (path === "/projects/{project}/merge-candidates")
      return Promise.resolve({ data: { data: candidates, meta: META }, error: undefined });
    if (path === "/projects/{project}/graph/subgraph") {
      if (failSubgraph === "neutral")
        return Promise.resolve({
          data: undefined,
          error: { error: { code: "STORE_UNAVAILABLE", message: "graph store down" } },
          response: { status: 503 },
        });
      if (failSubgraph === "scope")
        return Promise.resolve({
          data: undefined,
          error: { error: { code: "VALIDATION_ERROR", message: "entity not in active build" } },
          response: { status: 404 },
        });
      return Promise.resolve({
        data: { data: subgraph, meta: { ...META, build_id: subgraphBuildId } },
        error: undefined,
      });
    }
    if (path === "/projects/{project}/relations/{relation_id}") {
      if (failRelation === "neutral")
        return Promise.resolve({
          data: undefined,
          error: { error: { code: "STORE_UNAVAILABLE", message: "graph store down" } },
          response: { status: 503 },
        });
      if (failRelation === "scope")
        return Promise.resolve({
          data: undefined,
          error: { error: { code: "VALIDATION_ERROR", message: "relation not found" } },
          response: { status: 404 },
        });
      return Promise.resolve({ data: { data: relation, meta: META }, error: undefined });
    }
    throw new Error(`unstubbed GET ${path}`);
  }) as never);
}

// POST stub for the decision endpoints — resolves with the updated candidate.
export function stubDecision(updated: MergeCandidate) {
  return vi
    .spyOn(api, "POST")
    .mockResolvedValue({ data: { data: updated, meta: META }, error: undefined } as never);
}

export function retrievalResult(overrides: Partial<RetrievalResult> = {}): RetrievalResult {
  return {
    result_type: "chunk",
    id: "d0000000-0000-0000-0000-000000000000",
    title: null,
    text: null,
    score: 0.5,
    confidence: null,
    source_refs: [{ source_type: "chunk", id: "50000000-0000-0000-0000-000000000000" }],
    ...overrides,
  };
}

export function queryResult(overrides: Partial<QueryResult> = {}): QueryResult {
  return {
    mode: "hybrid",
    build_id: "b0000000-0000-0000-0000-000000000000",
    results: [],
    graph_context: null,
    warnings: [],
    debug: null,
    ...overrides,
  };
}

// POST stub for the query endpoints — resolves with the given QueryResult.
export function stubQuery(result: QueryResult) {
  return vi
    .spyOn(api, "POST")
    .mockResolvedValue({ data: { data: result, meta: META }, error: undefined } as never);
}

export function source(overrides: Partial<Source> = {}): Source {
  return {
    id: "50000000-0000-0000-0000-000000000000",
    kind: "file",
    uri: "file:///data/corpus",
    metadata: {},
    added_at: "2026-07-01T00:00:00Z",
    ...overrides,
  };
}

export function stubSources(sources: Source[]) {
  return vi
    .spyOn(api, "GET")
    .mockResolvedValue({ data: { data: sources, meta: META }, error: undefined } as never);
}

// Generic POST-success stub (create project / add source / trigger) — wraps the
// given payload in the response envelope. Tests read the returned spy to assert
// the path, body, and idempotency header the call sent.
export function stubPost(payload: unknown) {
  return vi
    .spyOn(api, "POST")
    .mockResolvedValue({ data: { data: payload, meta: META }, error: undefined } as never);
}

// POST-error stub for the fail-loud paths (create 409 name-taken, trigger 409
// JOB_CONFLICT, add-source outage). The message surfaces in the UI verbatim.
export function stubPostError(code: string, message: string) {
  return vi.spyOn(api, "POST").mockResolvedValue({
    data: undefined,
    error: { error: { code, message, details: null, request_id: META.request_id } },
  } as never);
}

// A streaming fetch Response carrying the given SSE text chunks — mock global
// fetch with this to drive the job event stream in tests.
export function sseResponse(chunks: string[], init: ResponseInit = {}): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const encoder = new TextEncoder();
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, ...init });
}
