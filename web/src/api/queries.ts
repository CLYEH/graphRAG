import { useQuery } from "@tanstack/react-query";

import { api } from "./client";
import type { components } from "./schema";

export type Project = components["schemas"]["Project"];

// Lists projects for the switcher. The contract paginates (meta.next_cursor),
// but the switcher shows one page; a page-through is a later concern.
export function useProjects() {
  return useQuery({
    queryKey: ["projects"],
    queryFn: async () => {
      const { data, error } = await api.GET("/projects", {});
      if (error) throw new Error(error.error.message);
      return data.data;
    },
  });
}
