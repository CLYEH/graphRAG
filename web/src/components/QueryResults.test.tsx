import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { QueryResults } from "./QueryResults";
import { queryResult, renderWithProviders, retrievalResult } from "../test-utils";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("QueryResults", () => {
  it("renders a source uri as text, never a clickable link", () => {
    // an untrusted source_uri in an <a href> would be a fresh injection sink
    // (the FE7 class-14 lesson) — it must render as inert text
    renderWithProviders(
      <QueryResults
        result={queryResult({
          results: [
            retrievalResult({
              source_refs: [
                {
                  source_type: "document",
                  id: "aaaaaaaa-1111-2222-3333-444444444444",
                  source_uri: "javascript:alert(1)",
                },
              ],
            }),
          ],
        })}
      />,
    );

    expect(screen.getByText(/javascript:alert\(1\)/)).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows the routing trace when the debug block is present", () => {
    renderWithProviders(
      <QueryResults
        result={queryResult({
          debug: {
            stores_used: [],
            retrieval_plan: [],
            routing_decision: {
              selected: ["semantic", "graph"],
              skipped: ["sql"],
              reason: "hybrid fan-out",
              confidence: null,
            },
            latency_ms: 12,
          },
        })}
      />,
    );

    expect(screen.getByText(/routing:/i).closest("p")).toHaveTextContent(
      "selected [semantic, graph] · skipped [sql] — hybrid fan-out",
    );
  });

  it("marks a nil-uuid build as degraded rather than showing an id", () => {
    renderWithProviders(
      <QueryResults result={queryResult({ build_id: "00000000-0000-0000-0000-000000000000" })} />,
    );

    expect(screen.getByText(/degraded/i)).toBeInTheDocument();
  });
});
