import { fireEvent, screen, waitFor } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ReviewQueue } from "./ReviewQueue";
import { api } from "../api/client";
import {
  mergeCandidate,
  projectRoute,
  renderWithProviders,
  stubMergeCandidates,
  stubReviewWorld,
} from "../test-utils";

const META = { next_cursor: null, build_id: "b1", request_id: "r", elapsed_ms: 1 };

function renderAt(key: string) {
  return renderWithProviders(
    <Routes>
      <Route path="/p/:project/review" element={<ReviewQueue />} />
    </Routes>,
    { route: projectRoute(key, "review") },
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ReviewQueue", () => {
  it("renders the review flow for an addressable project", async () => {
    stubReviewWorld({
      candidates: [
        mergeCandidate({ status: "pending", left_snapshot: { name: "海祭", type: "EVENT" } }),
      ],
    });
    renderAt("acme");

    // the governance surface (治理) defaults to the 合併 (merge) tab
    expect(await screen.findByRole("heading", { name: "治理" })).toBeInTheDocument();
    expect(await screen.findByText("海祭")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "是,合併" })).toBeEnabled();
  });

  it("lists ontology proposals in the 本體提案 tab and accepts one via the accept path (GOV3)", async () => {
    const proposal = {
      id: "p1111111-1111-4111-8111-000000000001",
      project: "acme",
      kind: "entity",
      type_name: "Spaceship",
      proposal_key: "fpv2:spaceship",
      status: "proposed",
      example: "Rocinante",
      chunk_ref: "chunk:hash-x:0",
    };
    // route-aware GET: proposals for the pool, empty for the default 合併 queue —
    // a single mock would feed merge-shaped data to the pool (false green: assert
    // the pool renders from THE ontology-proposals endpoint specifically)
    vi.spyOn(api, "GET").mockImplementation(((path: string) =>
      path === "/projects/{project}/ontology-proposals"
        ? Promise.resolve({ data: { data: [proposal], meta: META }, error: undefined })
        : Promise.resolve({ data: { data: [], meta: META }, error: undefined })) as never);
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: { data: { ...proposal, status: "accepted" }, meta: META },
      error: undefined,
    } as never);
    renderAt("acme");

    // switch to the proposals tab, then the pool lists the proposed type
    fireEvent.click(await screen.findByRole("tab", { name: "本體提案" }));
    expect(await screen.findByText("Spaceship")).toBeInTheDocument();
    expect(screen.getByText(/Rocinante/)).toBeInTheDocument();

    // 採納 → POST the ACCEPT path (verb rides the URL) with the deterministic
    // Idempotency-Key; a body-verb or the reject path would fail this
    fireEvent.click(screen.getByRole("button", { name: /採納/ }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/projects/{project}/ontology-proposals/{proposal_id}/accept",
        expect.objectContaining({
          params: expect.objectContaining({
            header: { "Idempotency-Key": `${proposal.id}:accept` },
          }),
        }),
      ),
    );
  });

  it("reports an un-addressable key instead of firing a doomed request", () => {
    // "a/b" opens in the route (base64url) but can't ride the {project} path
    // segment; the page must report that and the list query must stay disabled
    const get = stubMergeCandidates([]);
    renderAt("a/b");

    expect(screen.getByText(/isn't addressable over the api/i)).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
  });
});
