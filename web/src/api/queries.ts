import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

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

// Requests cancellation of a job (DESIGN §22). On success the job snapshot is
// refetched so the status reflects the request even if the stream has closed.
export function useCancelJob(jobId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/jobs/{job_id}/cancel", {
        params: { path: { job_id: jobId as string } },
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

// Creates a project. `name` is the projects primary key, so it doubles as the
// Idempotency-Key: a lost 201 replays on retry instead of the name conflict
// misreporting a committed create as a failure (a genuinely-taken name 409s
// either way). Optional text fields are omitted when blank rather than sent as
// empty strings. Invalidates the project list so the switcher and root redirect
// pick the new project up.
export function useCreateProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: NewProject) => {
      const body: components["schemas"]["ProjectCreate"] = { name: input.name };
      if (input.displayName.trim() !== "") body.display_name = input.displayName.trim();
      if (input.description.trim() !== "") body.description = input.description.trim();
      const { data, error } = await api.POST("/projects", {
        params: { header: { "Idempotency-Key": input.name } },
        body,
      });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["projects"] }),
  });
}

// Registers a source under a project. No Idempotency-Key by design: `uri` is not
// unique server-side (each add mints a fresh id, duplicate uris are permitted), so
// there is no natural key, and a uri-derived one would wrongly suppress an
// intentional re-registration. The submit button is disabled while the request is
// in flight, which covers the rapid double-submit. Invalidates the source list so
// the new row appears.
export function useAddSource(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { uri: string; kind: string }) => {
      const body: components["schemas"]["SourceCreate"] = { uri: input.uri };
      if (input.kind.trim() !== "") body.kind = input.kind.trim();
      const { data, error } = await api.POST("/projects/{project}/sources", {
        params: { path: { project } },
        body,
      });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sources", project] }),
  });
}

export type TriggerKind = "ingest" | "build";

// Triggers a pipeline run (ingest = stage 1, build = the full pipeline). The body
// is sent EMPTY: IngestRequest.source_ids and BuildRequest.reason are 400-rejected
// by presence until the pipeline honors them (BA2e-1), so no field may ride along
// — the kind rides the URL, switched to keep the typed path a codegen literal. No
// Idempotency-Key: create_job_exclusive serializes on the projects row and 409s a
// second concurrent trigger (JOB_CONFLICT "overlapping job"), which is the
// server-side dedup; the buttons are disabled while pending. Returns the accepted
// job so the caller can watch it live.
export function useTrigger(project: string) {
  return useMutation({
    mutationFn: async (kind: TriggerKind) => {
      const params = { path: { project } };
      const res =
        kind === "ingest"
          ? await api.POST("/projects/{project}/ingest", { params })
          : await api.POST("/projects/{project}/build", { params });
      if (res.error) throw new Error(res.error.error.message);
      return res.data.data;
    },
  });
}
