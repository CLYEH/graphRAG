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
          aria-label="工作識別碼"
          placeholder="貼上 job id"
          value={input}
          onChange={(e) => setInput(e.target.value)}
        />
        <button type="submit">追蹤</button>
      </form>

      {jobId === null && (
        <p className="runs__muted">
          通常不需要用到:Import 頁觸發建置時會直接顯示即時進度。這裡可貼上工作識別碼(job
          id)追蹤任何一個工作。
        </p>
      )}
      <JobProgress jobId={jobId} />
    </section>
  );
}
