import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "./client";
import { isPathAddressable } from "../project/projectRoute";

import type { components } from "./schema";

export type Project = components["schemas"]["Project"];
export type HealthReport = components["schemas"]["HealthReport"];
export type Build = components["schemas"]["Build"];
export type Job = components["schemas"]["Job"];

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
