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

// the action button carries "加入本體"; the confirm button is "確定採納" — matching
// on the former isolates the arming buttons from the confirm one
const acceptButtons = () => screen.getAllByRole("button", { name: /加入本體/ });

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ProposalPool", () => {
  // WHY: accept/reject are §17-TERMINAL (a re-decide 409s) and irreversible from
  // here — reject drops the type, accept mutates the configured ontology — so a
  // lone inline misclick must not commit them. Mirrors the merge flow's confirm.
  it("arms a confirm before a terminal decision and cancels back out with no POST", async () => {
    const a = proposal({ id: "a1", type_name: "Spaceship" });
    vi.spyOn(api, "GET").mockResolvedValue({
      data: { data: [a], meta: META },
      error: undefined,
    } as never);
    const post = vi.spyOn(api, "POST");

    renderWithProviders(<ProposalPool project="acme" />);

    // first click ARMS the confirm — nothing posts yet
    fireEvent.click(await screen.findByRole("button", { name: /加入本體/ }));
    expect(await screen.findByRole("alertdialog", { name: "確認決定" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "確定採納" })).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();

    // 取消 backs out to the action buttons, still nothing posted
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(await screen.findByRole("button", { name: /加入本體/ })).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  // WHY: a single useMutation observer tracks ONE mutation; a second concurrent
  // mutate() detaches it from the first, stranding the first's cleanup and racing
  // two opposite-verb terminal transitions. Locking the whole pool while any
  // decision is pending keeps exactly one in flight (Codex #104 P2).
  it("posts only after confirm, locks the whole pool in flight, and re-enables on settle", async () => {
    const a = proposal({ id: "a1", type_name: "Spaceship" });
    const b = proposal({ id: "b2", type_name: "Station" });
    vi.spyOn(api, "GET").mockResolvedValue({
      data: { data: [a, b], meta: META },
      error: undefined,
    } as never);
    // a decision I settle on demand, so the in-flight window is observable
    let reject!: (e: unknown) => void;
    const post = vi.spyOn(api, "POST").mockReturnValue(
      new Promise((_res, rej) => {
        reject = rej;
      }) as never,
    );

    renderWithProviders(<ProposalPool project="acme" />);
    await waitFor(() => expect(acceptButtons()).toHaveLength(2));

    // arm row A's confirm, then commit it
    fireEvent.click(acceptButtons()[0]);
    fireEvent.click(await screen.findByRole("button", { name: "確定採納" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));

    // decision in flight → the ENTIRE pool locks (both rows' accept disabled), so
    // no second concurrent mutation can detach the observer
    await waitFor(() => {
      for (const btn of acceptButtons()) expect(btn).toBeDisabled();
    });

    // settle (here it fails) → every button re-enables — no stuck row — and the
    // error surfaces
    reject(new Error("boom"));
    await waitFor(() => expect(acceptButtons()[0]).toBeEnabled());
    expect(acceptButtons()[1]).toBeEnabled();
    expect(screen.getByText(/決定失敗/)).toBeInTheDocument();
  });
});
