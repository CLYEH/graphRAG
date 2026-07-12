import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { JobWatcher } from "./JobWatcher";
import { api } from "../api/client";
import { job, renderWithProviders, sseResponse, stubApiError, stubJob } from "../test-utils";

const JOB_ID = "0c9f7a3e-2f65-4f0a-8a2b-7d1e9c4b5a6f";

function enter(id: string) {
  fireEvent.change(screen.getByLabelText(/job id/i), { target: { value: id } });
  fireEvent.click(screen.getByRole("button", { name: /watch/i }));
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("JobWatcher", () => {
  it("prompts for a job id before one is entered", () => {
    renderWithProviders(<JobWatcher />);
    expect(screen.getByText(/paste a job id/i)).toBeInTheDocument();
  });

  it("overlays the live SSE event on the fetched job snapshot", async () => {
    stubJob(job({ status: "running", kind: "build", step: null, progress: 0.1 }));
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          sseResponse([
            `event: job.update\ndata: {"job_id":"${JOB_ID}","status":"running","step":"graph","progress":0.6,"message":"embedding","ts":"2026-07-02T07:00:00Z"}\n\n`,
          ]),
        ),
    );
    renderWithProviders(<JobWatcher />);
    enter(JOB_ID);

    // the fetched snapshot supplies kind; the live event supplies step + progress
    expect(await screen.findByText("graph")).toBeInTheDocument();
    expect(await screen.findByText("60%")).toBeInTheDocument();
  });

  it("requests cancellation of a running job", async () => {
    stubJob(job({ status: "running" }));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([])));
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: { data: { job_id: JOB_ID, status: "cancelled" }, meta: {} },
      error: undefined,
    } as never);

    renderWithProviders(<JobWatcher />);
    enter(JOB_ID);

    fireEvent.click(await screen.findByRole("button", { name: /^cancel$/i }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
  });

  it("disables cancel for a job that already finished", async () => {
    stubJob(job({ status: "done", progress: 1 }));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([])));
    renderWithProviders(<JobWatcher />);
    enter(JOB_ID);

    expect(await screen.findByRole("button", { name: /cancel/i })).toBeDisabled();
  });

  it("prefers a terminal snapshot over a stale running event", async () => {
    // after a successful cancel the refetched snapshot is terminal; a retained
    // "running" SSE event must not mask it (else Cancel stays wrongly enabled)
    stubJob(job({ status: "cancelled" }));
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          sseResponse([
            `event: job.update\ndata: {"job_id":"${JOB_ID}","status":"running","step":"graph","progress":0.6,"message":null,"ts":"2026-07-02T07:00:00Z"}\n\n`,
          ]),
        ),
    );
    renderWithProviders(<JobWatcher />);
    enter(JOB_ID);

    await screen.findByText("graph"); // the running event has been applied
    expect(screen.getByRole("status")).toHaveTextContent("cancelled");
    expect(screen.getByRole("button", { name: /cancel/i })).toBeDisabled();
  });

  it("clears a field the live event reports as null instead of showing the snapshot", async () => {
    stubJob(job({ status: "running", step: "loading", progress: 0.1 }));
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          sseResponse([
            `event: job.update\ndata: {"job_id":"${JOB_ID}","status":"running","step":null,"progress":0.5,"message":null,"ts":"2026-07-02T07:00:00Z"}\n\n`,
          ]),
        ),
    );
    renderWithProviders(<JobWatcher />);
    enter(JOB_ID);

    await screen.findByText("50%"); // the live event landed
    expect(screen.queryByText("loading")).not.toBeInTheDocument();
  });

  it("fails loud when the job can't be loaded", async () => {
    stubApiError();
    renderWithProviders(<JobWatcher />);
    enter("nope");

    expect(await screen.findByText(/could not load job/i)).toBeInTheDocument();
  });
});
