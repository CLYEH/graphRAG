import { useEffect, useRef } from "react";

import { useCancelJob, useJob } from "../api/queries";
import { useJobStream } from "../hooks/useJobStream";

import type { Job } from "../api/queries";
import type { JobEvent } from "../api/jobStream";
import type { StreamStatus } from "../hooks/useJobStream";

// JobStatus (DESIGN §27.7) → badge tone, shared with the build status badges.
const TONE: Record<Job["status"], string> = {
  queued: "info",
  running: "warn",
  done: "ok",
  failed: "bad",
  cancelled: "muted",
};

const TERMINAL: ReadonlySet<Job["status"]> = new Set(["done", "failed", "cancelled"]);

// Live progress for a known job (DESIGN §27.7): the fetched snapshot supplies the
// static fields, the SSE stream overlays the fast-moving ones, and the stream
// closing refetches to surface the terminal-only fields (build_id, error). Shared
// by the Pipeline watcher (id pasted by the operator) and the Import page (id
// returned by an ingest/build trigger). Renders nothing until a job id is set.
export function JobProgress({ jobId }: { jobId: string | null }) {
  const { data: job, isPending, isError, error, refetch } = useJob(jobId);
  const stream = useJobStream(jobId);
  const cancel = useCancelJob(jobId);

  // One Idempotency-Key per cancel intent, per job: minted on the first click and
  // REUSED on retries (a lost response replays the stored cancellation instead of
  // re-posting against a job whose state moved on); a different job gets a fresh
  // key. Same lost-2xx class as the trigger keys.
  const cancelKey = useRef<{ id: string; key: string } | null>(null);
  function onCancel() {
    if (jobId === null) return;
    if (cancelKey.current === null || cancelKey.current.id !== jobId)
      cancelKey.current = { id: jobId, key: crypto.randomUUID() };
    cancel.mutate(cancelKey.current.key);
  }

  // JobEvent carries only the fast-moving fields, so when the stream ends (the
  // job reached a terminal state) refetch the snapshot to pull the terminal-only
  // fields — build_id, error, finished_at — instead of leaving the stale
  // watch-time values on screen until a manual re-watch.
  useEffect(() => {
    if (stream.status === "closed") void refetch();
  }, [stream.status, refetch]);

  if (jobId === null) return null;
  if (isPending) return <p className="runs__muted">Loading job…</p>;
  // only surface the error when there is no snapshot to show — a failed post-close
  // refetch must not blank (or stack over) an already-loaded job
  if (isError && !job)
    return (
      <p className="runs__muted runs__muted--error">
        Could not load job: {error instanceof Error ? error.message : "unknown error"}
      </p>
    );
  if (!job) return null;

  return (
    <JobView
      job={job}
      event={stream.event}
      streamStatus={stream.status}
      streamError={stream.error}
      onCancel={onCancel}
      cancelPending={cancel.isPending}
      cancelError={cancel.error instanceof Error ? cancel.error.message : null}
    />
  );
}

function JobView({
  job,
  event,
  streamStatus,
  streamError,
  onCancel,
  cancelPending,
  cancelError,
}: {
  job: Job;
  event: JobEvent | null;
  streamStatus: StreamStatus;
  streamError: string | null;
  onCancel: () => void;
  cancelPending: boolean;
  cancelError: string | null;
}) {
  // The live event overlays the fast-moving fields; the fetched job supplies the
  // static ones. When an event is present it wins wholesale (a null step/message
  // clears rather than showing a stale snapshot value); with no event the
  // snapshot renders, so a job that already finished still shows its final state.
  // Exception: a terminal snapshot (e.g. the post-cancel refetch) is authoritative
  // and must not be masked by a retained non-terminal event.
  const status = TERMINAL.has(job.status) ? job.status : (event?.status ?? job.status);
  const step = event ? event.step : (job.step ?? null);
  const progress = event ? event.progress : (job.progress ?? 0);
  const message = event ? event.message : (job.message ?? null);
  const cancellable = status === "queued" || status === "running";

  return (
    <div className="watch__job">
      <div className="watch__head">
        <span className={`runs__badge runs__badge--${TONE[status]}`} role="status">
          {status}
        </span>
        <code className="watch__kind">{job.kind ?? "job"}</code>
        <button type="button" onClick={onCancel} disabled={!cancellable || cancelPending}>
          {cancelPending ? "Cancelling…" : "Cancel"}
        </button>
      </div>

      <progress className="watch__bar" max={1} value={progress} />
      <span className="watch__pct">{Math.round(progress * 100)}%</span>

      <dl className="watch__facts">
        <div>
          <dt>step</dt>
          <dd>{step ?? "—"}</dd>
        </div>
        <div>
          <dt>message</dt>
          <dd>{message ?? "—"}</dd>
        </div>
        <div>
          <dt>build</dt>
          <dd>{job.build_id ?? "—"}</dd>
        </div>
      </dl>

      {job.error && <p className="runs__muted runs__muted--error">{job.error.message}</p>}
      {cancelError && (
        <p className="runs__muted runs__muted--error">Cancel failed: {cancelError}</p>
      )}
      <p className="watch__stream">
        stream: {streamStatus}
        {streamError ? ` — ${streamError}` : ""}
      </p>
    </div>
  );
}
