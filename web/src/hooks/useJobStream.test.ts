import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as jobStream from "../api/jobStream";
import { useJobStream } from "./useJobStream";

import type { SseFrame } from "../api/jobStream";

const frameFor = (jobId: string): SseFrame => ({
  event: "job.update",
  data: `{"job_id":"${jobId}","status":"running","step":"graph","progress":0.5,"message":null,"ts":"2026-07-02T07:00:00Z"}`,
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useJobStream", () => {
  it("aborts the stream on job change and unmount, and ignores a post-abort settlement", async () => {
    // Each call parks on a promise we control, capturing its abort signal — so we
    // can assert teardown happened and force a stale settlement afterwards. Without
    // the cleanup + guards this test fails; a self-closing mock stream would not.
    const calls: { signal: AbortSignal; resolve: () => void }[] = [];
    vi.spyOn(jobStream, "streamJobEvents").mockImplementation(
      (_jobId, opts) =>
        new Promise<void>((resolve) => {
          calls.push({ signal: opts.signal, resolve });
        }),
    );

    const { result, rerender, unmount } = renderHook(({ id }) => useJobStream(id), {
      initialProps: { id: "job-1" as string | null },
    });
    expect(calls).toHaveLength(1);
    expect(calls[0].signal.aborted).toBe(false);
    expect(result.current.status).toBe("streaming");

    // switching jobs must abort the first stream and open a second
    rerender({ id: "job-2" });
    expect(calls[0].signal.aborted).toBe(true);
    expect(calls).toHaveLength(2);

    // a late settlement of the aborted first stream must not flip the live status
    await act(async () => {
      calls[0].resolve();
    });
    expect(result.current.status).toBe("streaming");

    // unmount tears the live stream down too
    unmount();
    expect(calls[1].signal.aborted).toBe(true);
  });

  it("drops a frame delivered by the old stream after switching jobs", async () => {
    // A chunk that resolved just before cleanup runs its continuation *after* the
    // switch, calling the old job's onFrame. Without the aborted-signal guard that
    // stale event would overwrite job-2's reset state, showing job-2 with job-1's
    // progress until the next real frame — the race Codex flagged (#67).
    const calls: { signal: AbortSignal; onFrame: (f: SseFrame) => void }[] = [];
    vi.spyOn(jobStream, "streamJobEvents").mockImplementation(
      (_jobId, opts) =>
        new Promise<void>(() => {
          calls.push({ signal: opts.signal, onFrame: opts.onFrame });
        }),
    );

    const { result, rerender } = renderHook(({ id }) => useJobStream(id), {
      initialProps: { id: "job-1" as string | null },
    });

    // job-1's live frame lands while it is the active stream
    act(() => calls[0].onFrame(frameFor("job-1")));
    expect(result.current.event?.job_id).toBe("job-1");

    // switch to job-2: the effect resets event to null and aborts stream 1
    rerender({ id: "job-2" });
    expect(result.current.event).toBeNull();

    // the old stream delivers one more (late) frame — it must be ignored
    act(() => calls[0].onFrame(frameFor("job-1")));
    expect(result.current.event).toBeNull();
  });
});
