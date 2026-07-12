import { useState } from "react";

import { useAddSource, useSources, useTrigger } from "../api/queries";
import { isPathAddressable, useActiveProject } from "../project/projectRoute";
import { JobProgress } from "../components/JobProgress";
import { NewProjectForm } from "../components/NewProjectForm";
import "./Import.css";

import type { JobAccepted, TriggerKind } from "../api/queries";

// The source kinds the ingest pipeline actually wires (core/builds/sources.py
// SUPPORTED_SOURCE_KINDS): "text" reads a file:// text file/dir; "structured"
// reads a file:// CSV and requires table + pk_column in metadata. The store/API
// accept any kind string, but resolve_source fails the build loud for any other
// kind (or a blank one), so the UI offers only these two, collects the structured
// metadata, and points the uri at a file:// path (the only wired scheme) — never
// letting the operator register a source whose build is guaranteed to fail. The
// contract's Source.kind doc lists file/directory/url/database as illustrative
// connector kinds, but those have no C2 connector yet (Codex #70).
const SOURCE_KINDS = ["text", "structured"] as const;
type SourceKind = (typeof SOURCE_KINDS)[number];

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

// Register a source (file:// uri + a wired kind, plus table/pk_column for
// structured) and list what's registered. The uri and kind render as inert
// text/<code> — never an href/src — so a hostile source string can't become a live
// link (a class-14 sink); the uri is shown verbatim so the operator sees exactly
// what was stored.
function Sources({ project }: { project: string }) {
  const [uri, setUri] = useState("");
  const [kind, setKind] = useState<SourceKind>("text");
  const [table, setTable] = useState("");
  const [pkColumn, setPkColumn] = useState("");
  const sources = useSources(project);
  const add = useAddSource(project);

  // A structured source needs table + pk_column or resolve_source fails the build,
  // so gate the submit on them exactly as read_csv_rows requires.
  const structured = kind === "structured";
  const metaReady = !structured || (table.trim() !== "" && pkColumn.trim() !== "");
  const canAdd = uri.trim() !== "" && metaReady && !add.isPending;

  function submit() {
    add.mutate(
      {
        uri: uri.trim(),
        kind,
        metadata: structured ? { table: table.trim(), pk_column: pkColumn.trim() } : undefined,
      },
      {
        onSuccess: () => {
          setUri("");
          setTable("");
          setPkColumn("");
        },
      },
    );
  }

  return (
    <section className="import__section">
      <h2>Sources</h2>
      <p className="runs__muted">
        Sources are read from a local <code>file://</code> path — a text file or folder (text), or a
        CSV (structured).
      </p>
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
            placeholder="file:///data/corpus.txt"
          />
        </label>
        <label className="npf__field">
          kind
          <select value={kind} onChange={(e) => setKind(e.target.value as SourceKind)}>
            {SOURCE_KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>
        {structured && (
          <>
            <label className="npf__field">
              table
              <input
                value={table}
                onChange={(e) => setTable(e.target.value)}
                placeholder="documents"
              />
            </label>
            <label className="npf__field">
              pk_column
              <input
                value={pkColumn}
                onChange={(e) => setPkColumn(e.target.value)}
                placeholder="id"
              />
            </label>
          </>
        )}
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
