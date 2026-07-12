import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

import { api } from "./api/client";

import type { ReactElement } from "react";
import type { Project } from "./api/queries";

export function renderWithProviders(ui: ReactElement, { route = "/" }: { route?: string } = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
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

// The typed client binds globalThis.fetch at construction, so tests mock the
// client method rather than the global fetch — this also keeps the
// query→component contract (envelope unwrapping) under test. `as never` sidesteps
// openapi-fetch's overloaded GET signature for a fixture value.
export function stubProjects(projects: Project[]) {
  return vi
    .spyOn(api, "GET")
    .mockResolvedValue({ data: { data: projects, meta: META }, error: undefined } as never);
}

export function stubProjectsError() {
  return vi.spyOn(api, "GET").mockResolvedValue({
    data: undefined,
    error: {
      error: {
        code: "STORE_UNAVAILABLE",
        message: "down",
        details: null,
        request_id: META.request_id,
      },
    },
  } as never);
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
