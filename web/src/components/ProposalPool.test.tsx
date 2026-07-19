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
  // WHY: a single useMutation observer tracks ONE mutation; a second concurrent
  // mutate() detaches it from the first, so the first's onSettled never fires and
  // its row would stay stuck disabled — and two opposite-verb decisions on one row
  // (idem-keys A:accept vs A:reject differ, so no dedupe) would race two terminal
  // transitions, the loser 409ing into a spurious failure. The fix locks the whole
  // pool while any decision is pending, so exactly one is ever in flight and
  // react-query owns the pending lifecycle (Codex #104 P2, both rounds).
  it("locks the whole pool while a decision is in flight and re-enables on settle", async () => {
    const a = proposal({ id: "a1", type_name: "Spaceship" });
    const b = proposal({ id: "b2", type_name: "Station" });
    vi.spyOn(api, "GET").mockResolvedValue({
      data: { data: [a, b], meta: META },
      error: undefined,
    } as never);
    // a decision I settle on demand, so the in-flight window is observable
    let reject!: (e: unknown) => void;
    vi.spyOn(api, "POST").mockReturnValue(
      new Promise((_res, rej) => {
        reject = rej;
      }) as never,
    );

    renderWithProviders(<ProposalPool project="acme" />);

    // re-query fresh each step; order follows [a, b] — [0] is row A, [1] is row B
    const accepts = () => screen.getAllByRole("button", { name: /採納/ });
    await waitFor(() => expect(accepts()).toHaveLength(2));
    expect(accepts()[0]).toBeEnabled();
    expect(accepts()[1]).toBeEnabled();

    // decide row A → the ENTIRE pool locks, so no second concurrent decision can
    // start (which is what would detach the observer and strand a row's cleanup)
    fireEvent.click(accepts()[0]);
    await waitFor(() => expect(accepts()[0]).toBeDisabled());
    expect(accepts()[1]).toBeDisabled();

    // the decision settles (here it fails) → the single observer flips isPending
    // off, so every button re-enables — no row is left stuck — and the error shows
    reject(new Error("boom"));
    await waitFor(() => expect(accepts()[0]).toBeEnabled());
    expect(accepts()[1]).toBeEnabled();
    expect(screen.getByText(/決定失敗/)).toBeInTheDocument();
  });
});
