import { useState } from "react";

import { useAddSource, useSources, useTrigger } from "../api/queries";
import { isPathAddressable, useActiveProject } from "../project/projectRoute";
import { JobProgress } from "../components/JobProgress";
import { NewProjectForm } from "../components/NewProjectForm";
import "./Import.css";

import type { JobAccepted, TriggerKind } from "../api/queries";

// Connector kinds the frozen contract's Source.kind documents (file/directory/url/
// database); blank lets the backend infer. Not an enum in the contract, so this is
// a convenience list, not a validation gate.
const SOURCE_KINDS = ["file", "directory", "url", "database"];

// FE1 Import (DESIGN §5/§15): register sources into the active project by URI/
// connector, then trigger ingest (stage 1) or a full build and watch the job live.
// Byte upload is deliberately out of scope — the frozen contract models a source as
// a uri reference, not an uploaded file (owner scope decision 2026-07-12). Same
// project-addressability guards as the other pages.
export function Import() {
  const project = useActiveProject();

  if (project === undefined) return <p className="import__line">Unknown project.</p>;
  if (!isPathAddressable(project))
    return (
      <p className="import__line import__line--error">
        Project &quot;{project}&quot; isn&apos;t addressable over the API — its key contains
        &quot;/&quot; or is &quot;.&quot; / &quot;..&quot;, which a URL path segment can&apos;t
        carry.
      </p>
    );

  return (
    <section className="import">
      <h1 className="import__title">Import</h1>
      <p className="import__sub">
        Registering sources into <code>{project}</code>.
      </p>
      <Sources project={project} />
      <RunPipeline project={project} />
      <section className="import__section">
        <h2>New project</h2>
        <p className="runs__muted">Create a different project and switch to it.</p>
        <NewProjectForm />
      </section>
    </section>
  );
}

// Register a source (uri + optional kind) and list what's registered. The uri and
// kind render as inert text/<code> — never an href/src — so a hostile source
// string can't become a live link (a class-14 sink); the uri is shown verbatim so
// the operator sees exactly what was stored.
function Sources({ project }: { project: string }) {
  const [uri, setUri] = useState("");
  const [kind, setKind] = useState("");
  const sources = useSources(project);
  const add = useAddSource(project);

  const canAdd = uri.trim() !== "" && !add.isPending;

  function submit() {
    add.mutate(
      { uri: uri.trim(), kind },
      {
        onSuccess: () => {
          setUri("");
          setKind("");
        },
      },
    );
  }

  return (
    <section className="import__section">
      <h2>Sources</h2>
      <form
        className="npf__form"
        onSubmit={(e) => {
          e.preventDefault();
          if (canAdd) submit();
        }}
      >
        <label className="npf__field npf__field--wide">
          uri
          <input
            value={uri}
            onChange={(e) => setUri(e.target.value)}
            placeholder="file:///data/corpus · https://… · postgres://…"
          />
        </label>
        <label className="npf__field">
          kind
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">(infer)</option>
            {SOURCE_KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" disabled={!canAdd}>
          {add.isPending ? "Adding…" : "Add source"}
        </button>
        {add.isError && (
          <p className="npf__error">
            Add failed: {add.error instanceof Error ? add.error.message : "unknown error"}
          </p>
        )}
      </form>

      {sources.isPending && <p className="runs__muted">Loading sources…</p>}
      {sources.isError && (
        <p className="runs__muted runs__muted--error">
          Could not load sources:{" "}
          {sources.error instanceof Error ? sources.error.message : "unknown error"}
        </p>
      )}
      {sources.data && sources.data.length === 0 && (
        <p className="runs__muted">No sources registered yet.</p>
      )}
      {sources.data && sources.data.length > 0 && (
        <ul className="import__sources">
          {sources.data.map((s) => (
            <li key={s.id}>
              <code className="import__uri">{s.uri}</code>
              {s.kind ? <span className="import__kind">{s.kind}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// Trigger ingest/build and watch the returned job. Both buttons disable while a
// trigger is in flight; a second trigger while a job is already running comes back
// 409 JOB_CONFLICT (server-side one-job-per-project serialization), surfaced as the
// fail-loud error line. The accepted job id feeds straight into the shared live
// watcher so the operator sees progress without pasting an id elsewhere.
function RunPipeline({ project }: { project: string }) {
  const [accepted, setAccepted] = useState<JobAccepted | null>(null);
  const trigger = useTrigger(project);

  function run(kind: TriggerKind) {
    trigger.mutate(kind, { onSuccess: (job) => setAccepted(job) });
  }

  return (
    <section className="import__section">
      <h2>Run pipeline</h2>
      <p className="runs__muted">
        Ingest runs stage 1; build runs the full pipeline. One run at a time per project.
      </p>
      <div className="import__actions">
        <button type="button" onClick={() => run("ingest")} disabled={trigger.isPending}>
          {trigger.isPending ? "Triggering…" : "Ingest"}
        </button>
        <button type="button" onClick={() => run("build")} disabled={trigger.isPending}>
          {trigger.isPending ? "Triggering…" : "Build"}
        </button>
      </div>
      {trigger.isError && (
        <p className="npf__error">
          Trigger failed: {trigger.error instanceof Error ? trigger.error.message : "unknown error"}
        </p>
      )}
      {accepted && (
        <p className="runs__muted">
          Accepted job <code>{accepted.job_id}</code> ({accepted.status}).
        </p>
      )}
      <JobProgress jobId={accepted?.job_id ?? null} />
    </section>
  );
}
