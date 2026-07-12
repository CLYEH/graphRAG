import { useQuery } from "@tanstack/react-query";

import { api } from "./client";
import type { components } from "./schema";

export type Project = components["schemas"]["Project"];
export type HealthReport = components["schemas"]["HealthReport"];

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
// disabled until it resolves so a malformed segment never hits the API. Errors
// throw so the page fails loud (a health page that blanks hides the outage it
// exists to report) rather than rendering an empty shell.
export function useHealth(project: string | undefined) {
  return useQuery({
    queryKey: ["health", project],
    enabled: project !== undefined,
    queryFn: async () => {
      const { data, error } = await api.GET("/projects/{project}/health", {
        params: { path: { project: project as string } },
      });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
  });
}
