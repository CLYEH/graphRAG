import { useState } from "react";

import { useAddSource, useProjects, useSources, useTrigger } from "../api/queries";
import { isPathAddressable, useActiveProject } from "../project/projectRoute";
import { JobProgress } from "../components/JobProgress";
import { NewProjectForm } from "../components/NewProjectForm";
import "./Import.css";

import type { JobAccepted, Project, TriggerKind } from "../api/queries";

// Whether a project's config carries an ontology. The graph stage raises
// OntologyRequiredError for a build that has ANY text document and no
// config.ontology (core/builds/stages.py:177-182) — structured-only builds don't
// need one. NewProjectForm creates config-less projects, so a text build over a
// UI-created project is otherwise guaranteed to fail (Codex #70).
function hasOntology(config: Project["config"] | undefined): boolean {
  return config != null && config.ontology != null;
}

// The source kinds the ingest pipeline actually wires (core/builds/sources.py
// SUPPORTED_SOURCE_KINDS + core/ingest/connectors.py): "text" reads a file://
// DIRECTORY of .txt/.md files (read_text_documents raises NotADirectoryError on a
// single file); "structured" reads a file:// CSV FILE and requires table +
// pk_column in metadata. Both require a file:// uri — the only wired scheme
// (_local_path rejects others). The store/API accept any kind/uri string, but
// resolve_source fails the build loud otherwise, so the UI offers only these kinds,
// collects the structured metadata, and blocks a non-file:// uri — never letting
// the operator register a source whose build is guaranteed to fail. The contract's
// Source.kind doc lists file/directory/url/database as illustrative connector
// kinds, but those have no C2 connector yet (Codex #70).
const SOURCE_KINDS = ["text", "structured"] as const;
type SourceKind = (typeof SOURCE_KINDS)[number];

// Whether a uri is a canonical file:/// path — the exact form the backend reads.
// _local_path uses urlparse(uri).path only (core/builds/sources.py:50-57), so a
// host-bearing "file://nas/corpus" silently drops the host and reads /corpus, and
// an empty-path "file:" / "file://" resolves to the WORKER's cwd — both register a
// source the build then misreads (Codex #70). Requiring the triple-slash form with
// a non-empty path (parse-validated, so a bare local path is rejected too) makes
// the browser's accept set exactly what the backend will read. Dir-vs-file (text)
// stays placeholder guidance — a page can't stat the path.
function isFileUri(raw: string): boolean {
  try {
    new URL(raw); // must parse as a URL at all
  } catch {
    return false;
  }
  return /^file:\/\/\//i.test(raw) && raw.length > "file:///".length;
}

// FE1 Import (DESIGN §5/§15): register sources into the active project by URI/
// connector, then trigger ingest (stage 1) or a full build and watch the job live.
// Byte upload is deliberately out of scope — the frozen contract models a source as
// a uri reference, not an uploaded file (owner scope decision 2026-07-12). Same
// project-addressability guards as the other pages.
export function Import() {
  const project = useActiveProject();
  const projects = useProjects();

  if (project === undefined) return <p className="import__line">Unknown project.</p>;
  if (!isPathAddressable(project))
    return (
      <p className="import__line import__line--error">
        Project &quot;{project}&quot; isn&apos;t addressable over the API — its key contains
        &quot;/&quot; or is &quot;.&quot; / &quot;..&quot;, which a URL path segment can&apos;t
        carry.
      </p>
    );

  // Once the project resolves, whether it lacks an ontology (undefined while the
  // list loads → don't gate yet). RunPipeline blocks a text build in that case.
  const active = projects.data?.find((p) => p.name === project);
  const ontologyMissing = active !== undefined && !hasOntology(active.config);

  return (
    <section className="import">
      <h1 className="import__title">Import</h1>
      <p className="import__sub">
        Registering sources into <code>{project}</code>.
      </p>
      <Sources project={project} />
      <RunPipeline
        project={project}
        ontologyMissing={ontologyMissing}
        gatesLoaded={projects.data !== undefined}
      />
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
  // The only wired resolver is file://; anything else (https://, a bare path) is a
  // guaranteed build failure, so refuse it at the source rather than POST it.
  const badScheme = uri.trim() !== "" && !isFileUri(uri.trim());
  const canAdd = uri.trim() !== "" && !badScheme && metaReady && !add.isPending;

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
        Sources are read from a local <code>file://</code> path: <b>text</b> reads a directory of
        <code>.txt</code>/<code>.md</code> files; <b>structured</b> reads a CSV file.
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
            placeholder={structured ? "file:///data/rows.csv" : "file:///data/corpus/"}
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
        {badScheme && (
          <p className="npf__error">
            The uri must be a canonical <code>file:///</code> path (three slashes, no host) — the
            backend reads only the path part, so any other form is unwired or misread.
          </p>
        )}
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

// Trigger a pipeline run and watch the returned job. Both /ingest and /build
// currently enqueue the identical full six-stage build, differing only in the
// recorded job kind (api/routers/triggers.py + orchestrator run_build), so the copy
// says so rather than implying Ingest is a cheaper stage-1 run (Codex #70). Both
// buttons disable while a trigger is in flight; a second trigger while a job is
// already running comes back 409 JOB_CONFLICT (server-side one-job-per-project
// serialization), surfaced as the fail-loud error line. The accepted job id feeds
// straight into the shared live watcher so the operator sees progress inline.
function RunPipeline({
  project,
  ontologyMissing,
  gatesLoaded,
}: {
  project: string;
  ontologyMissing: boolean;
  gatesLoaded: boolean;
}) {
  const [accepted, setAccepted] = useState<JobAccepted | null>(null);
  const trigger = useTrigger(project);
  const sources = useSources(project);

  // A build with any text source and no ontology fails at the graph stage
  // (OntologyRequiredError); structured-only builds don't. Block the run rather
  // than accept a job guaranteed to fail after spending work (Codex #70). Fail
  // CLOSED while the config/source lists are still loading — unresolved data must
  // not read as "safe", or a cold load briefly enables the doomed trigger.
  const hasTextSource = (sources.data ?? []).some((s) => s.kind === "text");
  const blocked = ontologyMissing && hasTextSource;
  const ready = gatesLoaded && sources.data !== undefined;

  function run(kind: TriggerKind) {
    trigger.mutate(kind, { onSuccess: (job) => setAccepted(job) });
  }

  return (
    <section className="import__section">
      <h2>Run pipeline</h2>
      <p className="runs__muted">
        Both Ingest and Build run the full six-stage pipeline (ingest → summarize) — they differ
        only in the recorded job kind, and either way spends graph, LLM, and indexing work. One run
        at a time per project.
      </p>
      {blocked && (
        <p className="npf__error">
          This project has no ontology configured, so a build over text sources fails at the graph
          stage. Set <code>config.ontology</code> via the API/CLI (structured sources don&apos;t
          need one).
        </p>
      )}
      <div className="import__actions">
        <button
          type="button"
          onClick={() => run("ingest")}
          disabled={!ready || trigger.isPending || blocked}
        >
          {trigger.isPending ? "Triggering…" : "Ingest"}
        </button>
        <button
          type="button"
          onClick={() => run("build")}
          disabled={!ready || trigger.isPending || blocked}
        >
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
