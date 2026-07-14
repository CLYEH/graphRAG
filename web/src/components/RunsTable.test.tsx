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

    // words on the surface: the start time names the version; the uuid rides
    // the hover title only (UXA3) — its bare prefix must NOT be visible text
    expect((await screen.findAllByText(/版$/)).length).toBeGreaterThan(0);
    expect(screen.queryByText("b1111111")).not.toBeInTheDocument();
    expect(screen.getByText("上線中")).toBeInTheDocument();
    expect(screen.getByText("失敗")).toBeInTheDocument();
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

    fireEvent.click((await screen.findAllByText(/版$/))[0]);
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
