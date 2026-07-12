import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { JobsDashboard } from "./JobsDashboard";
import { build, projectRoute, stubBuilds } from "../test-utils";

afterEach(() => {
  vi.restoreAllMocks();
});

function renderAt(route: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/p/:project/jobs" element={<JobsDashboard />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("JobsDashboard", () => {
  it("renders the runs table and the job watcher for an addressable project", async () => {
    stubBuilds([build({ status: "active" })]);
    renderAt(projectRoute("acme", "jobs"));

    expect(await screen.findByRole("heading", { name: /pipeline/i })).toBeInTheDocument();
    expect(screen.getByText(/watch a job/i)).toBeInTheDocument();
    expect(await screen.findByText("active")).toBeInTheDocument(); // a run row badge
  });

  it("reports an un-addressable project without fetching runs", async () => {
    const spy = stubBuilds([]);
    renderAt(projectRoute("a/b", "jobs"));

    expect(await screen.findByText(/isn't addressable over the api/i)).toBeInTheDocument();
    expect(spy).not.toHaveBeenCalled();
  });
});
