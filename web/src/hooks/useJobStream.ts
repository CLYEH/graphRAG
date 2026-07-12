import { useEffect, useState } from "react";

import { streamJobEvents } from "../api/jobStream";

import type { JobEvent } from "../api/jobStream";

export type StreamStatus = "idle" | "streaming" | "closed" | "error";

// Subscribes to a job's SSE stream for the lifetime of `jobId`, exposing the
// latest event and the connection status. The AbortController tears the stream
// down on unmount or when jobId changes, and post-abort settlements are ignored
// so no state update lands after cleanup.
export function useJobStream(jobId: string | null): {
  event: JobEvent | null;
  status: StreamStatus;
  error: string | null;
} {
  const [event, setEvent] = useState<JobEvent | null>(null);
  const [status, setStatus] = useState<StreamStatus>("idle");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setEvent(null);
    setError(null);
    if (!jobId) {
      setStatus("idle");
      return;
    }
    setStatus("streaming");
    const controller = new AbortController();
    streamJobEvents(jobId, {
      signal: controller.signal,
      onFrame: (frame) => {
        // The stream aborts on jobId change / unmount, but a chunk that already
        // resolved can still run its continuation and call onFrame afterwards.
        // That frame belongs to the old job; dropping it keeps a stale event from
        // overwriting the new job's freshly-reset state (upholding the invariant
        // above that no state update lands after cleanup).
        if (controller.signal.aborted) return;
        try {
          setEvent(JSON.parse(frame.data) as JobEvent);
        } catch {
          // a malformed frame is dropped rather than crashing the stream
        }
      },
    })
      .then(() => {
        if (!controller.signal.aborted) setStatus("closed");
      })
      .catch((e: unknown) => {
        if (!controller.signal.aborted) {
          setStatus("error");
          setError(e instanceof Error ? e.message : String(e));
        }
      });
    return () => controller.abort();
  }, [jobId]);

  return { event, status, error };
}
