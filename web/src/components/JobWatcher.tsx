import { useState } from "react";

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

export function JobWatcher() {
  const [input, setInput] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const { data: job, isPending, isError, error } = useJob(jobId);
  const stream = useJobStream(jobId);
  const cancel = useCancelJob(jobId);

  return (
    <section className="watch">
      <form
        className="watch__form"
        onSubmit={(e) => {
          e.preventDefault();
          setJobId(input.trim() === "" ? null : input.trim());
        }}
      >
        <input
          className="watch__input"
          aria-label="Job id"
          placeholder="job id from graphrag build / ingest"
          value={input}
          onChange={(e) => setInput(e.target.value)}
        />
        <button type="submit">Watch</button>
      </form>

      {jobId === null && (
        <p className="runs__muted">
          Paste a job id (returned by the CLI) to watch its progress live.
        </p>
      )}
      {jobId !== null && isPending && <p className="runs__muted">Loading job…</p>}
      {jobId !== null && isError && (
        <p className="runs__muted runs__muted--error">
          Could not load job: {error instanceof Error ? error.message : "unknown error"}
        </p>
      )}
      {jobId !== null && job && (
        <JobView
          job={job}
          event={stream.event}
          streamStatus={stream.status}
          streamError={stream.error}
          onCancel={() => cancel.mutate()}
          cancelPending={cancel.isPending}
          cancelError={cancel.error instanceof Error ? cancel.error.message : null}
        />
      )}
    </section>
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
