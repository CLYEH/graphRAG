import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProjectHealth } from "./ProjectHealth";
import { healthReport, projectRoute, stubHealth, stubHealthError } from "../test-utils";

import type { HealthReport } from "../api/queries";

afterEach(() => {
  vi.restoreAllMocks();
});

// Renders the page under the real /p/:project/health route so useActiveProject
// decodes the segment exactly as production does.
function renderHealthAt(route: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/p/:project/health" element={<ProjectHealth />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ProjectHealth", () => {
  it("shows the status light, counts, and pending review for a healthy build", async () => {
    stubHealth(
      healthReport({
        status: "healthy",
        active_build_id: "b0000000-0000-0000-0000-000000000001",
        counts: { sources: 2, documents: 5, chunks: 40, entities: 12, relations: 8 },
        pending_review: 3,
      }),
    );
    renderHealthAt(projectRoute("acme"));

    expect(await screen.findByRole("status")).toHaveTextContent("Healthy");
    // counts render from the open map, so an operator sees corpus size at a glance
    expect(screen.getByText("chunks")).toBeInTheDocument();
    expect(screen.getByText("40")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument(); // pending review
  });

  // The five §19 lights must be visually distinct — a page that collapsed them
  // would hide exactly the conditions (failed build, drift, regression) it exists
  // to surface. Each enum value maps to its own human label.
  it.each([
    ["needs_review", "Needs review"],
    ["build_failed", "Build failed"],
    ["index_drift", "Index drift"],
    ["eval_regression", "Eval regression"],
  ] as [HealthReport["status"], string][])("labels the %s light as %j", async (status, label) => {
    stubHealth(healthReport({ status }));
    renderHealthAt(projectRoute("acme"));

    expect(await screen.findByRole("status")).toHaveTextContent(label);
  });

  it("shows per-store drift detail so the operator can reproject", async () => {
    stubHealth(
      healthReport({
        status: "index_drift",
        drift: { qdrant: { expected: 40, actual: 39 } },
      }),
    );
    renderHealthAt(projectRoute("acme"));

    expect(await screen.findByRole("status")).toHaveTextContent("Index drift");
    expect(screen.getByText("qdrant")).toBeInTheDocument();
    expect(screen.getByText(/"actual":39/)).toBeInTheDocument();
  });

  it("says no drift when the reconciliation is clean", async () => {
    stubHealth(healthReport({ drift: null }));
    renderHealthAt(projectRoute("acme"));

    expect(await screen.findByText(/no drift detected/i)).toBeInTheDocument();
  });

  it("surfaces store warnings from the report", async () => {
    stubHealth(
      healthReport({
        warnings: [{ code: "STORE_UNAVAILABLE", message: "qdrant unreachable" }],
      }),
    );
    renderHealthAt(projectRoute("acme"));

    expect(await screen.findByText(/qdrant unreachable/i)).toBeInTheDocument();
    expect(screen.getByText("STORE_UNAVAILABLE")).toBeInTheDocument();
  });

  it("renders an em dash when there is no active build", async () => {
    stubHealth(healthReport({ active_build_id: null }));
    renderHealthAt(projectRoute("acme"));

    await screen.findByRole("status");
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("fails loud when the health endpoint errors instead of blanking", async () => {
    // a health page that silently shows nothing hides the very outage it reports
    stubHealthError();
    renderHealthAt(projectRoute("acme"));

    expect(await screen.findByText(/could not load project health/i)).toBeInTheDocument();
  });

  it("reports an unknown project for an undecodable route segment", async () => {
    // "@@@" is not valid base64url, so the segment decodes to nothing; the page
    // must say so rather than spin forever or hit the API with undefined
    const spy = stubHealth(healthReport());
    renderHealthAt("/p/@@@/health");

    expect(await screen.findByText(/unknown project/i)).toBeInTheDocument();
    expect(spy).not.toHaveBeenCalled(); // the query stays disabled
  });

  it.each([".", ".."])("refuses the un-addressable key %j without a request", async (key) => {
    // "." / ".." open in the route but normalize to the wrong endpoint as a REST
    // path segment (Codex #66 P2); the page must say so and never fire the call
    const spy = stubHealth(healthReport());
    renderHealthAt(projectRoute(key));

    expect(await screen.findByText(/reserved url path segment/i)).toBeInTheDocument();
    expect(spy).not.toHaveBeenCalled();
  });
});
