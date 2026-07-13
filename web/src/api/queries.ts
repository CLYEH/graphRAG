import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "./client";
import { isPathAddressable } from "../project/projectRoute";

import type { components } from "./schema";

export type Project = components["schemas"]["Project"];
export type Source = components["schemas"]["Source"];
export type HealthReport = components["schemas"]["HealthReport"];
export type Build = components["schemas"]["Build"];
export type JobAccepted = components["schemas"]["JobAccepted"];
export type Job = components["schemas"]["Job"];
export type MergeCandidate = components["schemas"]["MergeCandidate"];
export type MergeCandidateStatus = components["schemas"]["MergeCandidateStatus"];
export type Document = components["schemas"]["Document"];
export type Chunk = components["schemas"]["Chunk"];
export type QueryMode = components["schemas"]["QueryMode"];
export type QueryResult = components["schemas"]["QueryResult"];

// Lists every project for the switcher. The switcher must reach any project,
// so page through meta.next_cursor to exhaustion rather than showing only the
// first page (the API caps a page at 500). Project counts in a local console
// are small; a searchable picker is the answer if that ever stops holding.
export function useProjects() {
  return useQuery({
    queryKey: ["projects"],
    queryFn: async () => {
      const all: Project[] = [];
      let cursor: string | undefined;
      do {
        const { data, error } = await api.GET("/projects", {
          params: { query: { limit: 500, cursor } },
        });
        if (error) throw new Error(error.error.message);
        all.push(...data.data);
        cursor = data.meta.next_cursor ?? undefined;
      } while (cursor);
      return all;
    },
  });
}

// Project Health home (DESIGN §19): status light + counts + drift for the
// active build. `project` is the decoded key from the route; the query stays
// disabled until it resolves (a malformed segment never hits the API) and while
// the key is not path-addressable ("." / ".." would normalize to the wrong
// endpoint — the page reports that instead). Errors throw so the page fails loud
// (a health page that blanks hides the outage it exists to report).
export function useHealth(project: string | undefined) {
  return useQuery({
    queryKey: ["health", project],
    enabled: project !== undefined && isPathAddressable(project),
    queryFn: async () => {
      const { data, error } = await api.GET("/projects/{project}/health", {
        params: { path: { project: project as string } },
      });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
  });
}

// Build (run) history for the pipeline dashboard (DESIGN §19). Same project
// path-addressability gate as health; pages through next_cursor so old runs are
// reachable, and fails loud so a store outage shows rather than an empty table.
export function useBuilds(project: string | undefined) {
  return useQuery({
    queryKey: ["builds", project],
    enabled: project !== undefined && isPathAddressable(project),
    queryFn: async () => {
      const all: Build[] = [];
      let cursor: string | undefined;
      do {
        const { data, error } = await api.GET("/projects/{project}/builds", {
          params: { path: { project: project as string }, query: { limit: 200, cursor } },
        });
        if (error) throw new Error(error.error.message);
        all.push(...data.data);
        cursor = data.meta.next_cursor ?? undefined;
      } while (cursor);
      return all;
    },
  });
}

// Current job state (DESIGN §27.7). `jobId` is the user-pasted id; the query is
// disabled until one is entered. The live SSE stream (useJobStream) overlays the
// fast-moving fields on top of this; this fetch supplies the static ones (kind,
// build_id, timestamps, error) and the initial snapshot.
export function useJob(jobId: string | null) {
  return useQuery({
    queryKey: ["job", jobId],
    enabled: jobId !== null && jobId !== "",
    queryFn: async () => {
      const { data, error } = await api.GET("/jobs/{job_id}", {
        params: { path: { job_id: jobId as string } },
      });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
  });
}

// Requests cancellation of a job (DESIGN §22). `idempotencyKey` is a per-logical-
// attempt random key (same lost-2xx class as the triggers): a retry after a lost
// response replays the stored cancellation instead of re-posting against a job
// whose state has moved on. On success the job snapshot is refetched so the
// status reflects the request even if the stream has closed.
export function useCancelJob(jobId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (idempotencyKey: string) => {
      const { data, error } = await api.POST("/jobs/{job_id}/cancel", {
        params: {
          path: { job_id: jobId as string },
          header: { "Idempotency-Key": idempotencyKey },
        },
      });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["job", jobId] }),
  });
}

// Merge candidates awaiting entity-resolution review (DESIGN §17). Active-build
// scoped like the other project reads; pages through next_cursor so the whole
// queue is reachable, and fails loud so a store outage / no-active-build surfaces
// (with the API's message) rather than an empty queue that reads as "nothing to
// review". All statuses are returned — decided rows stay visible as an audit
// trail, their actions disabled by the §17 gate below.
export function useMergeCandidates(project: string | undefined) {
  return useQuery({
    queryKey: ["merge-candidates", project],
    enabled: project !== undefined && isPathAddressable(project),
    queryFn: async () => {
      const all: MergeCandidate[] = [];
      let cursor: string | undefined;
      // The endpoint re-resolves the active build per request, so a build
      // activated mid-pagination would splice page 1 (old build) with later
      // pages (new build) — a mixed queue whose stale rows then 404 on decide.
      // Pin the first page's build_id and fail loud on a swap; a retry pulls a
      // clean single-build snapshot. (`undefined` = not yet seen; build_id itself
      // is string | null.)
      let buildId: string | null | undefined;
      do {
        const { data, error } = await api.GET("/projects/{project}/merge-candidates", {
          params: { path: { project: project as string }, query: { limit: 200, cursor } },
        });
        if (error) throw new Error(error.error.message);
        if (buildId === undefined) buildId = data.meta.build_id;
        else if (data.meta.build_id !== buildId)
          throw new Error("The active build changed while loading the review queue — retry.");
        all.push(...data.data);
        cursor = data.meta.next_cursor ?? undefined;
      } while (cursor);
      return all;
    },
  });
}

export type ReviewVerb = "approve" | "reject" | "defer";

// Records a curator decision on a merge candidate (DESIGN §17). The verb rides
// the URL (three frozen paths), not a body field, so switch on it to keep the
// typed path a literal — an accept-and-ignore body verb would defeat the codegen.
// On success the queue is invalidated so the row reflects the new status (and a
// terminal status disables further actions). `reason` is optional.
export function useDecideMergeCandidate(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      candidateId,
      verb,
      reason,
    }: {
      candidateId: string;
      verb: ReviewVerb;
      reason: string | null;
    }) => {
      // A deterministic key per (candidate, verb): if a response is lost after the
      // server commits the decision, the curator's retry replays the stored 200
      // instead of hitting an illegal-transition 400 on the now-decided candidate
      // (which would misreport a successful review as failed). The transition model
      // makes each (candidate, verb) a once-only operation, so this natural key is
      // stable across retries without extra state; the endpoints dedupe on it.
      const params = {
        path: { project, candidate_id: candidateId },
        header: { "Idempotency-Key": `${candidateId}:${verb}` },
      };
      const body = { reason };
      const res =
        verb === "approve"
          ? await api.POST("/projects/{project}/merge-candidates/{candidate_id}/approve", {
              params,
              body,
            })
          : verb === "reject"
            ? await api.POST("/projects/{project}/merge-candidates/{candidate_id}/reject", {
                params,
                body,
              })
            : await api.POST("/projects/{project}/merge-candidates/{candidate_id}/defer", {
                params,
                body,
              });
      if (res.error) throw new Error(res.error.error.message);
      return res.data.data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["merge-candidates", project] }),
  });
}

// The graph invocation carried in the contract's `options` channel (DESIGN §27.6).
// The generated schema types `options` as an open object, so the shape the runtime
// GraphOptions model enforces (api/schemas.py, extra="forbid") is declared here.
export type GraphOptions = {
  template: "neighbors" | "path" | "subgraph";
  entity: string;
  other_entity?: string | null;
  hops?: number;
};

export interface QueryForm {
  mode: QueryMode;
  query: string;
  topK: number | null;
  options: GraphOptions | null;
}

// Builds the per-mode request body. The codegen types all five query endpoints
// with one permissive QueryRequest, but the runtime enforces per-mode
// `extra="forbid"` models that reject a field by its PRESENCE (model_fields_set),
// so keys are included conditionally, never sent as null: semantic/sql/global
// take query + optional top_k; graph takes query + options (no top_k); hybrid
// takes query, optional top_k, and optional options (omitted — never null — to
// skip the graph mode). `query` is required for every mode, graph included.
export function queryBody(form: QueryForm): components["schemas"]["QueryRequest"] {
  const body: components["schemas"]["QueryRequest"] = { query: form.query };
  if (form.mode === "graph") {
    if (form.options) body.options = form.options;
    return body;
  }
  if (form.topK !== null) body.top_k = form.topK;
  if (form.mode === "hybrid" && form.options) body.options = form.options;
  return body;
}

// Runs a query against the active build (DESIGN §21/§22). A query is a read-only
// RPC — no Idempotency-Key and no cache invalidation (nothing changes server-side)
// — run on submit. The mode rides the URL, so switch to keep the path a codegen
// literal. Errors (503 unconfigured / 409 no active build / 400 rejected field /
// 404) throw so the page fails loud; §22 degradation instead comes back 200 with
// warnings, which the caller renders from the returned QueryResult.
export function useRunQuery(project: string) {
  return useMutation({
    mutationFn: async (form: QueryForm) => {
      const params = { path: { project } };
      const body = queryBody(form);
      const res =
        form.mode === "semantic"
          ? await api.POST("/projects/{project}/query/semantic", { params, body })
          : form.mode === "sql"
            ? await api.POST("/projects/{project}/query/sql", { params, body })
            : form.mode === "global"
              ? await api.POST("/projects/{project}/query/global", { params, body })
              : form.mode === "graph"
                ? await api.POST("/projects/{project}/query/graph", { params, body })
                : await api.POST("/projects/{project}/query/hybrid", { params, body });
      if (res.error) throw new Error(res.error.error.message);
      return res.data.data;
    },
  });
}

// ─── FE1 Import (DESIGN §5/§15, BA1b projects/sources + BA2e triggers) ───

// A project's registered sources, newest first. Same path-addressability gate as
// the other project reads; pages through next_cursor so every source is reachable,
// and fails loud so a store outage surfaces rather than an empty list that would
// read as "no sources yet".
export function useSources(project: string | undefined) {
  return useQuery({
    queryKey: ["sources", project],
    enabled: project !== undefined && isPathAddressable(project),
    queryFn: async () => {
      const all: Source[] = [];
      let cursor: string | undefined;
      do {
        const { data, error } = await api.GET("/projects/{project}/sources", {
          params: { path: { project: project as string }, query: { limit: 200, cursor } },
        });
        if (error) throw new Error(error.error.message);
        all.push(...data.data);
        cursor = data.meta.next_cursor ?? undefined;
      } while (cursor);
      return all;
    },
  });
}

export interface NewProject {
  name: string;
  displayName: string;
  description: string;
}

// Creates a project. No client Idempotency-Key: `name` is the projects primary
// key, so a duplicate — including a lost-201 retry — 409s "already exists" and
// fails loud (the operator sees the project in the switcher and moves on). A
// name-derived header key was rejected on purpose: `ProjectCreate.name` allows any
// non-empty string (unicode, or >255 chars), which is not a valid HTTP header
// value, so keying on it would break creating exactly the names the contract
// permits (Codex #70); and a stable name key would additionally replay a stale 201
// across a delete-then-recreate of the same name within the key TTL. Optional text
// fields are omitted when blank. Invalidates the project list so the switcher and
// root redirect pick the new project up.
export function useCreateProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: NewProject) => {
      const body: components["schemas"]["ProjectCreate"] = { name: input.name };
      if (input.displayName.trim() !== "") body.display_name = input.displayName.trim();
      if (input.description.trim() !== "") body.description = input.description.trim();
      const { data, error } = await api.POST("/projects", { body });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["projects"] }),
  });
}

// Registers a source under a project. `kind` is one of the ingest-wired kinds
// (text/structured); `metadata` carries a structured source's table + pk_column
// (read_csv_rows requires them). `idempotencyKey` is a per-logical-attempt random
// key (NOT uri-derived — `uri` isn't unique server-side and a stable natural key
// would suppress an intentional re-registration): a retry after a lost 201 replays
// the stored response instead of minting a duplicate row, while a NEW attempt (the
// form contents changed) carries a fresh key so deliberate duplicates stay
// possible. Invalidates the source list so the new row appears.
export function useAddSource(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: {
      uri: string;
      kind: string;
      metadata?: Record<string, string>;
      idempotencyKey: string;
    }) => {
      const body: components["schemas"]["SourceCreate"] = { uri: input.uri, kind: input.kind };
      if (input.metadata) body.metadata = input.metadata;
      const { data, error } = await api.POST("/projects/{project}/sources", {
        params: {
          path: { project },
          header: { "Idempotency-Key": input.idempotencyKey },
        },
        body,
      });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sources", project] }),
  });
}

export type TriggerKind = "ingest" | "build";

// Triggers a pipeline run (both kinds run the full pipeline; the kind rides the
// URL, switched to keep the typed path a codegen literal). The body is sent EMPTY:
// IngestRequest.source_ids and BuildRequest.reason are 400-rejected by presence
// until the pipeline honors them (BA2e-1). `idempotencyKey` is a per-logical-
// attempt random key: create_job_exclusive only dedups while the first job is
// non-terminal, so a retry after a LOST 202 would otherwise either 409 with no job
// id to watch (job still running) or double-trigger a second full pipeline (job
// finished) — with the key, the retry replays the stored 202 and hands back the
// ORIGINAL job id. Returns the accepted job so the caller can watch it live.
export function useTrigger(project: string) {
  return useMutation({
    mutationFn: async ({ kind, idempotencyKey }: { kind: TriggerKind; idempotencyKey: string }) => {
      const params = { path: { project }, header: { "Idempotency-Key": idempotencyKey } };
      const res =
        kind === "ingest"
          ? await api.POST("/projects/{project}/ingest", { params })
          : await api.POST("/projects/{project}/build", { params });
      if (res.error) throw new Error(res.error.error.message);
      return res.data.data;
    },
  });
}

// ---- FE3 Inspect (BA3 reads) ------------------------------------------------
//
// DESIGN §10.2 names this page 檢視(文件/chunks) — the ingested documents and the
// chunks they were split into. Entity/relation detail with evidence is the spec'd
// content of a DIFFERENT page (圖譜互動探索 / FE4: "點邊顯示 type/confidence/
// evidence/來源"), so the entity/relation reads are FE4's, not this task's.
//
// Three things the frozen contract makes non-negotiable here:
//
// 1. NEVER send `sort` or `filter[...]`. The op params still expose them, but
//    `reject_unsupported_query` (api/routers/_query.py) 400s any `filter[...]` and
//    any sort other than the list's own default — and for CHUNKS, whose default
//    order is the compound (document_id, ordinal), it rejects EVERY explicit sort
//    (`sort_field=None`). Verified live: `?sort=id:desc` on chunks → HTTP 400.
// 2. Each request re-resolves the ACTIVE build, so a build activated mid-pagination
//    would splice page 1 (old build) with page 2 (new) — a silently mixed corpus.
//    Every page carries `meta.build_id`; the page pins it and fails loud on a swap.
//    (No active build at all is a 409 NO_ACTIVE_BUILD, verified live — never a 200
//    with an empty list, so an empty table really does mean an empty build.)
// 3. A missing row answers 404 with `error.code = "VALIDATION_ERROR"` — `code_for_status`
//    maps EVERY 4xx to that code, so it cannot distinguish "gone" from "bad request".
//    Branch on the HTTP STATUS.

const INSPECT_PAGE = 50;

/** One page of a build-scoped list: its rows, the build that served them, and the
 *  cursor to the next page (absent = last page). */
export type InspectPage<T> = { rows: T[]; buildId: string | null; next?: string };

/** An API failure that keeps the frozen error CODE. The list pages are only valid while
 *  the build that served them is still active, and the code is what says whether it is. */
type ErrorCode = components["schemas"]["ErrorCode"];

class ApiError extends Error {
  readonly code: ErrorCode | "";

  constructor(code: ErrorCode | "", message: string) {
    super(message);
    this.code = code;
  }
}

/** Codes that say NOTHING about the build that served the pages already on screen: the store
 *  was down, we were throttled, the server faulted, it timed out. The corpus is untouched, so
 *  a load-more that hits one of these may keep the loaded rows.
 *
 *  This is an ALLOWLIST, and the direction is the point. Both of `inspect.py::_bind`'s failure
 *  exits prove the scope that served those rows is gone — NO_ACTIVE_BUILD (409, the build was
 *  deactivated) and PROJECT_NOT_FOUND (404: `delete_project` refuses while any build exists, so
 *  the project being gone means its builds are too). Listing the REJECTS instead would close
 *  today's two spellings and leave the branch open for the next: §27.2's ErrorCode vocabulary is
 *  additive-only, and a code added later must not silently inherit the branch that keeps a
 *  possibly-vanished corpus on screen. Fail closed by default; earn the rows back explicitly. */
const SCOPE_NEUTRAL = new Set<ErrorCode>([
  "STORE_UNAVAILABLE",
  "RATE_LIMITED",
  "INTERNAL",
  "QUERY_TIMEOUT",
]);

/** Builds the thrown error from the API's error envelope. The `??`s are not defensive noise:
 *  a body that is NOT our envelope (a proxy's HTML 502, say) has no `error.code` to read, and
 *  the empty code deliberately falls OUTSIDE the allowlist below — an unparseable failure tells
 *  us nothing about the binding, and "nothing" fails closed here. Reading it unguarded would
 *  instead throw a TypeError and show the user "Cannot read properties of undefined". */
function apiError(body: { error?: { code?: ErrorCode; message?: string } }): ApiError {
  return new ApiError(body.error?.code ?? "", body.error?.message ?? "the request failed");
}

/** True when the failure CANNOT have invalidated the build that served the loaded pages —
 *  either the server never answered at all (a transport error, so it said nothing about the
 *  binding) or it answered with a scope-neutral code above.
 *
 *  Note this keys on the CODE while the detail read below keys on the STATUS. The asymmetry is
 *  real: these codes are raised deliberately by the API, whereas a detail 404's code is the
 *  COARSE fallback `code_for_status` stamps on every framework-raised 4xx (VALIDATION_ERROR),
 *  which identifies nothing. And status alone would be a spelling here — 409 is shared by
 *  BUILD_NOT_READY and JOB_CONFLICT; the code names the condition itself. */
export function isScopeNeutral(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true; // the server never answered — it said nothing
  return error.code !== "" && SCOPE_NEUTRAL.has(error.code); // unparseable body ⇒ fail closed
}

function useInspectList<T>(
  key: string,
  project: string | undefined,
  fetchPage: (project: string, cursor?: string) => Promise<InspectPage<T>>,
) {
  return useInfiniteQuery({
    queryKey: [key, project],
    enabled: project !== undefined && isPathAddressable(project),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => fetchPage(project as string, pageParam),
    getNextPageParam: (last: InspectPage<T>) => last.next,
  });
}

async function fetchDocuments(project: string, cursor?: string): Promise<InspectPage<Document>> {
  const { data, error } = await api.GET("/projects/{project}/documents", {
    params: { path: { project }, query: { limit: INSPECT_PAGE, cursor } },
  });
  if (error) throw apiError(error);
  return { rows: data.data, buildId: data.meta.build_id, next: data.meta.next_cursor ?? undefined };
}

async function fetchChunks(project: string, cursor?: string): Promise<InspectPage<Chunk>> {
  const { data, error } = await api.GET("/projects/{project}/chunks", {
    params: { path: { project }, query: { limit: INSPECT_PAGE, cursor } },
  });
  if (error) throw apiError(error);
  return { rows: data.data, buildId: data.meta.build_id, next: data.meta.next_cursor ?? undefined };
}

export const useDocuments = (project: string | undefined) =>
  useInspectList("documents", project, fetchDocuments);
export const useChunks = (project: string | undefined) =>
  useInspectList("chunks", project, fetchChunks);

// A 404 here means "no such row IN THE ACTIVE BUILD" — ids are minted per build, so a
// row id from a superseded build cannot resolve in the current one. Say that, rather
// than echoing the generic VALIDATION_ERROR message every 4xx carries.
function detailError(status: number, message: string): Error {
  return new Error(
    status === 404
      ? "Not found in the active build — it may belong to an older build, or the active build changed. Reload the list."
      : message,
  );
}

// Detail reads. `Document.raw` is returned ONLY here — the list omits the key entirely
// (verified against a real build), which is what a row click is for.
export function useDocument(project: string | undefined, id: string | undefined) {
  return useQuery({
    queryKey: ["document", project, id],
    enabled: project !== undefined && isPathAddressable(project) && id !== undefined,
    queryFn: async () => {
      const { data, error, response } = await api.GET(
        "/projects/{project}/documents/{document_id}",
        { params: { path: { project: project as string, document_id: id as string } } },
      );
      if (error) throw detailError(response.status, error.error.message);
      return data.data;
    },
  });
}

export function useChunk(project: string | undefined, id: string | undefined) {
  return useQuery({
    queryKey: ["chunk", project, id],
    enabled: project !== undefined && isPathAddressable(project) && id !== undefined,
    queryFn: async () => {
      const { data, error, response } = await api.GET("/projects/{project}/chunks/{chunk_id}", {
        params: { path: { project: project as string, chunk_id: id as string } },
      });
      if (error) throw detailError(response.status, error.error.message);
      return data.data;
    },
  });
}

// ---- FE2 清洗 (DESIGN §10.2, contract v1.1 DR-009) --------------------------------
//
// Two facts drive the shapes here (read from api/routers/projects.py and
// core/registry/store.py, not assumed):
// 1. PATCH /projects/{project} REPLACES the whole config column — there is no
//    server-side deep merge. Writing chunking params must therefore spread the
//    project's CURRENT config and override only the chunking block; PATCHing
//    {config: {chunking}} alone would silently WIPE ontology and every other
//    block, and the wreck would surface only at the next build.
// 2. The preview is a pure RPC (nothing persisted, no Idempotency-Key), so it
//    is a mutation, not a query: no cache entry means no stale-while-revalidate
//    window for class 17 to bite — the page owns "these chunks answer THESE
//    parameters" staleness explicitly instead.

export type CleanPreviewRequest = components["schemas"]["CleanPreviewRequest"];
export type CleanPreviewChunk = components["schemas"]["CleanPreviewChunk"];
export type CleanPreviewResult = { chunks: CleanPreviewChunk[]; buildId: string | null };

/** Engine chunking defaults — DISPLAY mirror of core/clean/chunking.py's
 *  DEFAULT_MAX_CHARS/DEFAULT_OVERLAP. Only placeholders and the save button's
 *  label read these; every REAL fallback happens server-side (the preview and
 *  the build walk the same config→default chain), so a drift here mislabels a
 *  placeholder but cannot change what runs. */
export const DEFAULT_CHUNKING = { max_chars: 1200, overlap: 200 } as const;

/** The project's configured chunking values with engine-default fallback —
 *  same per-field, bool-excluded read the preview endpoint does server-side
 *  (bool is an int subtype in Python AND accepted by JS typeof checks nowhere,
 *  but a config written by hand can hold anything). */
export function chunkingFromConfig(config: Record<string, unknown>): {
  max_chars: number;
  overlap: number;
} {
  const block = config["chunking"];
  const pick = (key: string, fallback: number): number => {
    if (block && typeof block === "object" && !Array.isArray(block)) {
      const v = (block as Record<string, unknown>)[key];
      if (typeof v === "number" && Number.isInteger(v)) return v;
    }
    return fallback;
  };
  return {
    max_chars: pick("max_chars", DEFAULT_CHUNKING.max_chars),
    overlap: pick("overlap", DEFAULT_CHUNKING.overlap),
  };
}

/** The single project — FE2 needs the full config object to spread on save. */
export function useProject(project: string | undefined) {
  return useQuery({
    queryKey: ["project", project],
    enabled: project !== undefined && isPathAddressable(project),
    queryFn: async () => {
      const { data, error } = await api.GET("/projects/{project}", {
        params: { path: { project: project as string } },
      });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
  });
}

/** Preview chunking (POST clean/preview). meta.build_id names the active build
 *  the document was read from; null for the text source. */
export function usePreviewClean(project: string) {
  return useMutation({
    mutationFn: async (body: CleanPreviewRequest): Promise<CleanPreviewResult> => {
      const { data, error } = await api.POST("/projects/{project}/clean/preview", {
        params: { path: { project } },
        body,
      });
      if (error) throw new Error(error.error.message);
      return { chunks: data.data.chunks, buildId: data.meta.build_id };
    },
  });
}

/** Write the chunking block into project config — spreading the CURRENT config
 *  (fact 1 above). The caller passes the config it read; sending a stale spread
 *  would resurrect deleted blocks, so the page keeps the project query fresh and
 *  invalidates it on success. */
export function useSaveChunking(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (args: {
      config: Record<string, unknown>;
      max_chars: number;
      overlap: number;
    }) => {
      const { data, error } = await api.PATCH("/projects/{project}", {
        params: { path: { project } },
        body: {
          config: {
            ...args.config,
            chunking: { max_chars: args.max_chars, overlap: args.overlap },
          },
        },
      });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["project", project] }),
  });
}
