import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProposalPool } from "./ProposalPool";
import { api } from "../api/client";
import { renderWithProviders } from "../test-utils";

import type { OntologyProposal } from "../api/queries";

const META = { next_cursor: null, build_id: "b1", request_id: "r", elapsed_ms: 1 };

function proposal(over: Partial<OntologyProposal>): OntologyProposal {
  return {
    id: "p0",
    project: "acme",
    kind: "entity",
    type_name: "T",
    proposal_key: "k",
    fingerprint_version: 2,
    example: null,
    chunk_ref: null,
    status: "proposed",
    decided_by: null,
    decided_at: null,
    reason: null,
    created_at: "2026-07-01T00:00:00Z",
    ...over,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ProposalPool", () => {
  // WHY: with a single `deciding` id, deciding row B overwrote A's value and
  // re-enabled A's buttons while A's POST was still open — a second, opposite-verb
  // decision on A could then race two terminal transitions (idem-keys A:accept vs
  // A:reject differ, so the server 409s the loser and reports it as a failure).
  // The pin: each in-flight row disables INDEPENDENTLY (Codex #104 P2).
  it("keeps a row disabled while its own decision is in flight even after another row is decided", async () => {
    const a = proposal({ id: "a1", type_name: "Spaceship" });
    const b = proposal({ id: "b2", type_name: "Station" });
    vi.spyOn(api, "GET").mockResolvedValue({
      data: { data: [a, b], meta: META },
      error: undefined,
    } as never);
    // decisions never settle, so both rows stay in flight for the whole test —
    // onSettled never runs, so nothing is removed from the in-flight set
    vi.spyOn(api, "POST").mockReturnValue(new Promise(() => {}) as never);

    renderWithProviders(<ProposalPool project="acme" />);

    // re-query fresh each step so a re-render can't hand back a stale node;
    // order follows the [a, b] response — [0] is row A, [1] is row B
    const accepts = () => screen.getAllByRole("button", { name: /採納/ });
    await waitFor(() => expect(accepts()).toHaveLength(2));

    // decide A → A's own buttons disable while its POST is open
    fireEvent.click(accepts()[0]);
    await waitFor(() => expect(accepts()[0]).toBeDisabled());
    // B is untouched, still actionable
    expect(accepts()[1]).toBeEnabled();

    // decide B while A is STILL posting → B disables AND A stays disabled.
    // (the single-`deciding` bug re-enabled A here → this assertion would fail.)
    fireEvent.click(accepts()[1]);
    await waitFor(() => expect(accepts()[1]).toBeDisabled());
    expect(accepts()[0]).toBeDisabled();
  });
});
