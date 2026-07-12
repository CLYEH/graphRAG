import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "./client";
import { isPathAddressable } from "../project/projectRoute";

import type { components } from "./schema";

export type Project = components["schemas"]["Project"];
export type HealthReport = components["schemas"]["HealthReport"];
export type Build = components["schemas"]["Build"];
export type Job = components["schemas"]["Job"];
export type MergeCandidate = components["schemas"]["MergeCandidate"];
export type MergeCandidateStatus = components["schemas"]["MergeCandidateStatus"];

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
