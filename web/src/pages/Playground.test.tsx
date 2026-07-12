import { fireEvent, screen, waitFor } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Playground } from "./Playground";
import { api } from "../api/client";
import {
  projectRoute,
  queryResult,
  renderWithProviders,
  retrievalResult,
  stubQuery,
} from "../test-utils";

function renderAt(key: string) {
  return renderWithProviders(
    <Routes>
      <Route path="/p/:project/playground" element={<Playground />} />
    </Routes>,
    { route: projectRoute(key, "playground") },
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Playground", () => {
  it("reports an un-addressable project key", () => {
    renderAt("a/b");
    expect(screen.getByText(/isn't addressable over the api/i)).toBeInTheDocument();
  });

  it("runs the default (hybrid) query and shows results with citations", async () => {
    const post = stubQuery(
      queryResult({
        mode: "hybrid",
        results: [
          retrievalResult({
            result_type: "chunk",
            text: "the answer",
            score: 0.9,
            source_refs: [{ source_type: "document", id: "aaaaaaaa-1111-2222-3333-444444444444" }],
          }),
        ],
      }),
    );
    renderAt("acme");

    fireEvent.change(screen.getByLabelText("query"), { target: { value: "what?" } });
    fireEvent.click(screen.getByRole("button", { name: /run query/i }));

    expect(await screen.findByText("the answer")).toBeInTheDocument();
    expect(screen.getByText("document")).toBeInTheDocument(); // the source_ref badge
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/projects/{project}/query/hybrid", {
        params: { path: { project: "acme" } },
        body: { query: "what?" },
      }),
    );
  });

  it("hides top_k and shows the graph options for the graph mode", async () => {
    stubQuery(queryResult());
    renderAt("acme");

    fireEvent.change(screen.getByLabelText("mode"), { target: { value: "graph" } });

    expect(screen.queryByLabelText("top_k")).not.toBeInTheDocument();
    expect(screen.getByLabelText("entity")).toBeInTheDocument();
  });

  it("submits the graph mode with options and no top_k", async () => {
    const post = stubQuery(queryResult({ mode: "graph" }));
    renderAt("acme");

    fireEvent.change(screen.getByLabelText("mode"), { target: { value: "graph" } });
    fireEvent.change(screen.getByLabelText("query"), { target: { value: "who?" } });
    fireEvent.change(screen.getByLabelText("entity"), { target: { value: "Ada" } });
    fireEvent.click(screen.getByRole("button", { name: /run query/i }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/projects/{project}/query/graph", {
        params: { path: { project: "acme" } },
        body: { query: "who?", options: { template: "neighbors", entity: "Ada", hops: 1 } },
      }),
    );
  });

  it("keeps Run disabled for the graph mode until an entity is given", async () => {
    // graph's options (with entity) are REQUIRED — an empty entity would 400, so
    // the form must gate submit on it; this is what guarantees the body is never
    // sent with incomplete graph options.
    stubQuery(queryResult({ mode: "graph" }));
    renderAt("acme");

    fireEvent.change(screen.getByLabelText("mode"), { target: { value: "graph" } });
    fireEvent.change(screen.getByLabelText("query"), { target: { value: "who?" } });
    expect(screen.getByRole("button", { name: /run query/i })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("entity"), { target: { value: "Ada" } });
    expect(screen.getByRole("button", { name: /run query/i })).toBeEnabled();
  });

  it("renders a degraded result (warnings, no rows) as warnings, not an empty state", async () => {
    stubQuery(
      queryResult({
        results: [],
        warnings: [{ code: "PARTIAL_RESULTS", message: "exceeded the 5000ms deadline (§21)" }],
      }),
    );
    renderAt("acme");

    fireEvent.change(screen.getByLabelText("query"), { target: { value: "slow" } });
    fireEvent.click(screen.getByRole("button", { name: /run query/i }));

    expect(await screen.findByText("PARTIAL_RESULTS")).toBeInTheDocument();
    expect(screen.getByText(/exceeded the 5000ms deadline/i)).toBeInTheDocument();
    expect(screen.queryByText(/^no results\.$/i)).not.toBeInTheDocument();
  });

  it("fails loud when the query errors (e.g. model unconfigured)", async () => {
    vi.spyOn(api, "POST").mockResolvedValue({
      data: undefined,
      error: {
        error: {
          code: "STORE_UNAVAILABLE",
          message: "embedding model is not configured",
          details: null,
          request_id: "0",
        },
      },
    } as never);
    renderAt("acme");

    fireEvent.change(screen.getByLabelText("query"), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: /run query/i }));

    expect(
      await screen.findByText(/query failed: embedding model is not configured/i),
    ).toBeInTheDocument();
  });
});
