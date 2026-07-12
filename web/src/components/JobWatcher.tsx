import { useState } from "react";

import { JobProgress } from "./JobProgress";

// The Pipeline page's job watcher: an operator pastes a CLI-returned job id and
// this streams its live progress. The fetch/stream/cancel machinery lives in the
// shared JobProgress; this component only owns the paste form and the empty prompt.
export function JobWatcher() {
  const [input, setInput] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);

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
      <JobProgress jobId={jobId} />
    </section>
  );
}
