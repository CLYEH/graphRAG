import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CandidatesTable } from "./CandidatesTable";
import {
  mergeCandidate,
  renderWithProviders,
  stubApiError,
  stubDecision,
  stubMergeCandidates,
} from "../test-utils";

const CID = "c1111111-1111-1111-1111-111111111111";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CandidatesTable", () => {
  it("lists candidates with a status badge and score", async () => {
    stubMergeCandidates([mergeCandidate({ status: "pending", score: 0.87 })]);
    renderWithProviders(<CandidatesTable project="acme" />);

    expect(await screen.findByText("pending")).toBeInTheDocument();
    expect(screen.getByText("0.870")).toBeInTheDocument();
  });

  it("shows an empty state when there is nothing to review", async () => {
    stubMergeCandidates([]);
    renderWithProviders(<CandidatesTable project="acme" />);

    expect(await screen.findByText(/no merge candidates to review/i)).toBeInTheDocument();
  });

  it("fails loud when the queue can't be loaded", async () => {
    stubApiError();
    renderWithProviders(<CandidatesTable project="acme" />);

    expect(await screen.findByText(/could not load review queue/i)).toBeInTheDocument();
  });

  it("offers every action for a pending candidate", async () => {
    stubMergeCandidates([mergeCandidate({ status: "pending" })]);
    renderWithProviders(<CandidatesTable project="acme" />);

    expect(await screen.findByRole("button", { name: /approve/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /reject/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /defer/i })).toBeEnabled();
  });

  it("won't re-defer a deferred candidate but still allows approve/reject (§17)", async () => {
    // BA5 permits deferred → approved|rejected only; the frozen contract has no
    // illegal-transition error, so the UI is the sole guard against a doomed defer.
    stubMergeCandidates([mergeCandidate({ status: "deferred" })]);
    renderWithProviders(<CandidatesTable project="acme" />);

    expect(await screen.findByRole("button", { name: /approve/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /reject/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /defer/i })).toBeDisabled();
  });

  // both members of the TERMINAL set are asserted, so dropping either from the
  // set (a real regression) fails a test — the §17 matrix stays discriminating.
  it.each(["approved", "rejected"] as const)(
    "disables every action on a %s (terminal) candidate",
    async (status) => {
      stubMergeCandidates([mergeCandidate({ status })]);
      renderWithProviders(<CandidatesTable project="acme" />);

      expect(await screen.findByRole("button", { name: /approve/i })).toBeDisabled();
      expect(screen.getByRole("button", { name: /reject/i })).toBeDisabled();
      expect(screen.getByRole("button", { name: /defer/i })).toBeDisabled();
    },
  );

  it("posts the chosen verb to its own path with the typed reason", async () => {
    // the verb rides the URL, not the body — a mis-wired verb→path or a dropped
    // reason would merge/split the wrong way, so pin both (status vs decision trap)
    stubMergeCandidates([mergeCandidate({ id: CID, status: "pending" })]);
    const post = stubDecision(mergeCandidate({ id: CID, status: "rejected" }));
    renderWithProviders(<CandidatesTable project="acme" />);

    fireEvent.change(await screen.findByLabelText(/decision reason/i), {
      target: { value: "obvious dupes" },
    });
    fireEvent.click(screen.getByRole("button", { name: /reject/i }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/projects/{project}/merge-candidates/{candidate_id}/reject",
        {
          params: {
            path: { project: "acme", candidate_id: CID },
            header: { "Idempotency-Key": `${CID}:reject` },
          },
          body: { reason: "obvious dupes" },
        },
      ),
    );
  });

  it("sends a null reason when the field is left blank", async () => {
    stubMergeCandidates([mergeCandidate({ id: CID, status: "pending" })]);
    const post = stubDecision(mergeCandidate({ id: CID, status: "approved" }));
    renderWithProviders(<CandidatesTable project="acme" />);

    fireEvent.click(await screen.findByRole("button", { name: /approve/i }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/projects/{project}/merge-candidates/{candidate_id}/approve",
        {
          params: {
            path: { project: "acme", candidate_id: CID },
            header: { "Idempotency-Key": `${CID}:approve` },
          },
          body: { reason: null },
        },
      ),
    );
  });

  it("reuses one idempotency key across retries of the same decision (Codex #68)", async () => {
    // if the first response is lost after the server commits, the retry must carry
    // the SAME key so the endpoint replays the 200 rather than 400-ing on the
    // already-decided candidate — a random-per-click key would defeat that.
    stubMergeCandidates([mergeCandidate({ id: CID, status: "pending" })]);
    const post = stubDecision(mergeCandidate({ id: CID, status: "approved" }));
    renderWithProviders(<CandidatesTable project="acme" />);

    fireEvent.click(await screen.findByRole("button", { name: /approve/i }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole("button", { name: /approve/i })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));

    const expected = {
      params: {
        path: { project: "acme", candidate_id: CID },
        header: { "Idempotency-Key": `${CID}:approve` },
      },
      body: { reason: null },
    };
    const path = "/projects/{project}/merge-candidates/{candidate_id}/approve";
    expect(post).toHaveBeenNthCalledWith(1, path, expected);
    expect(post).toHaveBeenNthCalledWith(2, path, expected);
  });
});
