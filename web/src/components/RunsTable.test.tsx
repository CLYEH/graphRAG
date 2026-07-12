import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RunsTable } from "./RunsTable";
import { build, renderWithProviders, stubApiError, stubBuilds } from "../test-utils";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("RunsTable", () => {
  it("lists builds with a status badge per run", async () => {
    stubBuilds([
      build({ id: "b1111111-aaaa-4aaa-8aaa-000000000001", status: "active" }),
      build({ id: "b2222222-bbbb-4bbb-8bbb-000000000002", status: "failed" }),
    ]);
    renderWithProviders(<RunsTable project="acme" />);

    expect(await screen.findByText("b1111111")).toBeInTheDocument(); // short id
    expect(screen.getByText("active")).toBeInTheDocument();
    expect(screen.getByText("failed")).toBeInTheDocument();
  });

  it("expands a run to drill into hashes and metrics", async () => {
    stubBuilds([
      build({
        id: "b1111111-aaaa-4aaa-8aaa-000000000001",
        status: "failed",
        config_hash: "cfg-abc",
        metrics: { groundedness: 0.91 },
      }),
    ]);
    renderWithProviders(<RunsTable project="acme" />);

    fireEvent.click(await screen.findByText("b1111111"));
    // the drill-down is what makes a failed run diagnosable from the dashboard
    expect(await screen.findByText("cfg-abc")).toBeInTheDocument();
    expect(screen.getByText(/"groundedness":0\.91/)).toBeInTheDocument();
  });

  it("shows an empty state when there are no builds", async () => {
    stubBuilds([]);
    renderWithProviders(<RunsTable project="acme" />);

    expect(await screen.findByText(/no builds yet/i)).toBeInTheDocument();
  });

  it("fails loud instead of showing an empty table when builds can't load", async () => {
    stubApiError();
    renderWithProviders(<RunsTable project="acme" />);

    expect(await screen.findByText(/could not load runs/i)).toBeInTheDocument();
  });
});
