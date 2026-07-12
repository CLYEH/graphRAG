import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

import { api } from "./api/client";
import { encodeProjectSegment } from "./project/projectRoute";

import type { ReactElement } from "react";
import type { HealthReport, Project } from "./api/queries";

// Builds the encoded route for a project key, so tests exercise the real
// encode/decode path rather than hardcoding a raw `/p/<key>` segment.
export function projectRoute(key: string, section = "health") {
  return `/p/${encodeProjectSegment(key)}/${section}`;
}

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
