import { useQuery } from "@tanstack/react-query";

import { api } from "./client";
import type { components } from "./schema";

export type Project = components["schemas"]["Project"];

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
