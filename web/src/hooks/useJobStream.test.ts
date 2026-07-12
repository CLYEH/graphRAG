import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as jobStream from "../api/jobStream";
import { useJobStream } from "./useJobStream";

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
});
