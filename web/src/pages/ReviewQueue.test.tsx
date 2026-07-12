import { screen } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ReviewQueue } from "./ReviewQueue";
import {
  mergeCandidate,
  projectRoute,
  renderWithProviders,
  stubMergeCandidates,
} from "../test-utils";

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
  it("renders the review queue for an addressable project", async () => {
    stubMergeCandidates([mergeCandidate({ status: "pending" })]);
    renderAt("acme");

    expect(await screen.findByRole("heading", { name: /entity review/i })).toBeInTheDocument();
    expect(await screen.findByText("pending")).toBeInTheDocument();
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
