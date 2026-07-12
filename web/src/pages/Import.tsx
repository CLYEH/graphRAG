import { useEffect, useRef, useState } from "react";

import { useAddSource, useProjects, useSources, useTrigger } from "../api/queries";
import { isPathAddressable, useActiveProject } from "../project/projectRoute";
import { JobProgress } from "../components/JobProgress";
import { NewProjectForm } from "../components/NewProjectForm";
import "./Import.css";

import type { JobAccepted, Project, Source, TriggerKind } from "../api/queries";

// Whether a project's config carries a KNOWN-VALID ontology — presence is not
// enough. The graph stage raises OntologyRequiredError for a build with ANY text
// document and no config.ontology (core/builds/stages.py:177-182; structured-only
// builds don't need one), and a present-but-malformed block fails even earlier:
// _load_ontology (core/builds/config.py:161-179) rejects unknown keys, non-string
// type entries, and a proposal_policy outside {"review","auto"}, and TextOntology
// requires BOTH entity_types and relation_types to be non-empty with every entry
// non-blank (core/graph/ontology.py:77-85) — all raising BuildConfigError before
// the pipeline runs. This mirrors that acceptance exactly, so the gate fails
// closed on any shape the worker would refuse (Codex #70).
function hasOntology(config: Project["config"] | undefined): boolean {
  const block = config?.ontology;
  if (typeof block !== "object" || block === null || Array.isArray(block)) return false;
  const record = block as Record<string, unknown>;
  const allowed = new Set(["entity_types", "relation_types", "proposal_policy"]);
  if (Object.keys(record).some((k) => !allowed.has(k))) return false;
  if ("proposal_policy" in record) {
    const policy = record.proposal_policy;
    if (policy !== "review" && policy !== "auto") return false;
  }
  const typeList = (v: unknown): boolean =>
    Array.isArray(v) && v.length > 0 && v.every((t) => typeof t === "string" && t.trim() !== "");
  return typeList(record.entity_types) && typeList(record.relation_types);
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
// host-bearing "file://nas/corpus" silently drops the host and reads /corpus, an
// empty-path "file:" / "file://" resolves to the WORKER's cwd, and a query/hash
// suffix ("file:///data?old") is silently stripped — the worker reads a different
// path than the stored uri displays. The structural checks run on the DECODED
// path, because url2pathname percent-decodes before resolving: a four-slash
// "file:////nas/x" AND an encoded "file:///%2F…" both land as a "//"-leading path
// that gets reinterpreted as a UNC authority / server root instead of the
// displayed path, and a bare "file:///" (or encoded "%2F" alone) is the root
// (Codex #70). A malformed percent-escape rejects too (decode throws; the backend
// would read it literally — another display/read divergence). Requiring the
// triple-slash form with a non-root, single-leading-slash decoded path and no
// search/hash (parse-validated, so a bare local path is rejected too) makes the
// browser's accept set exactly what the backend will read. Dir-vs-file (text)
// stays placeholder guidance — a page can't stat.
function isFileUri(raw: string): boolean {
  let url: URL;
  try {
    url = new URL(raw); // must parse as a URL at all
  } catch {
    return false;
  }
  if (!/^file:\/\/\//i.test(raw) || url.search !== "" || url.hash !== "") return false;
  // Validate the BACKEND-derived path, not the browser-normalized url.pathname:
  // WHATWG normalizes dot segments (raw ".." AND encoded "%2e%2e") away at parse
  // time, so checks on url.pathname never see them — but the backend keeps them
  // (urlparse(raw).path), decodes, and the filesystem then resolves them to a
  // different tree than the stored uri appears to name ("file:///data/%2e%2e/etc"
  // reads /etc). Mirror the backend read: with the authority forced empty by the
  // triple-slash check, urlparse's path is the raw substring after "file://" (we
  // also cut at ?/# to match, though search/hash are already rejected above),
  // percent-decoded like url2pathname does.
  let decoded: string;
  try {
    decoded = decodeURIComponent(raw.slice("file://".length).split(/[?#]/)[0]);
  } catch {
    return false; // malformed escape: the backend would read it literally — refuse
  }
  // An embedded NUL (file:///data/%00corpus) can't name a real file on any
  // supported OS — the connector's read is guaranteed to fail.
  if (decoded.includes("\0")) return false;
  if (decoded.length <= 1) return false;
  // Every segment of the path the WORKER will read must be non-empty (an empty
  // segment means a "//" — UNC/root reinterpretation) and not "." / ".." (the
  // filesystem would resolve them away from the displayed path). One trailing
  // slash is allowed — the idiomatic directory form.
  const path = decoded.endsWith("/") ? decoded.slice(0, -1) : decoded;
  const segments = path.split("/").slice(1);
  return segments.length > 0 && segments.every((s) => s !== "" && s !== "." && s !== "..");
}

// Whether the pipeline can resolve an already-registered source to the path the
// operator registered. Two failure families, both blocking (Codex #70): (1)
// resolve_source RAISES (core/builds/sources.py:71-90) on a kind outside the wired
// set or a structured source missing non-empty table/pk_column (_required_meta) —
// a loud guaranteed failure; (2) a non-canonical file uri (host/query/hash-bearing,
// e.g. file://nas/corpus or file:///data?old) doesn't raise but is silently
// REINTERPRETED — _local_path reads only urlparse(uri).path, so the build ingests
// /corpus or /data instead of the registered target: wrong data, which this
// platform treats as strictly worse than a loud failure. So loaded sources must
// pass the same canonical file:/// validation the add form enforces. Sources
// created outside this form (CLI/API) can carry any of these; one bad source
// breaks EVERY build, regardless of ontology, so the run gate checks the whole
// loaded list.
function isResolvableSource(s: Source): boolean {
  if (s.kind !== "text" && s.kind !== "structured") return false;
  // The worker reads the STORED uri verbatim — Python's urlparse keeps a trailing
  // space in the path (verified live), while new URL()/trim() normalize it away.
  // So edge whitespace is itself a display/read divergence: check exactly as
  // stored, never a trimmed view. (The add form trims before POST, so only
  // sources registered outside the form can carry it.)
  if (s.uri !== s.uri.trim() || !isFileUri(s.uri)) return false;
  if (s.kind === "structured") {
    const table = s.metadata?.table;
    const pk = s.metadata?.pk_column;
    if (typeof table !== "string" || table.trim() === "") return false;
    if (typeof pk !== "string" || pk.trim() === "") return false;
  }
  return true;
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
        // fail closed while the config is loading, refetching, OR errored —
        // react-query keeps the previous config in data during the flight and
        // after a failed refetch, and a CLI-side ontology change must not be
        // gated on that stale snapshot
        gatesLoaded={projects.data !== undefined && !projects.isFetching && !projects.isError}
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

  // One Idempotency-Key per LOGICAL attempt: retrying the same form contents after
  // a lost 201 replays the original row instead of duplicating it, while any edit
  // (including the post-success clear) mints a fresh key so a deliberately
  // re-typed duplicate registration still goes through.
  const attemptKey = useRef(crypto.randomUUID());
  useEffect(() => {
    attemptKey.current = crypto.randomUUID();
  }, [uri, kind, table, pkColumn]);

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
        idempotencyKey: attemptKey.current,
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
  // CLOSED while the source gate is loading OR refetching — react-query keeps the
  // previous list in `data` during the post-add invalidation refetch, so a gate
  // that only checks presence decides on stale data in exactly the window where
  // the just-added text source dooms the build (a bind-time-vs-invariant TOCTOU).
  const hasTextSource = (sources.data ?? []).some((s) => s.kind === "text");
  const ontologyBlocked = ontologyMissing && hasTextSource;
  // One unresolvable source (unwired kind / non-file scheme / missing structured
  // metadata — e.g. registered via CLI/API) fails every build at ingest.
  const unresolvable = (sources.data ?? []).filter((s) => !isResolvableSource(s));
  const blocked = ontologyBlocked || unresolvable.length > 0;
  // isError matters alongside isFetching: a FAILED refetch (e.g. right after an
  // add that DID commit server-side) leaves the previous list in data with
  // isFetching false — the gate must not reopen on that stale snapshot.
  const ready =
    gatesLoaded && sources.data !== undefined && !sources.isFetching && !sources.isError;

  // One Idempotency-Key per logical trigger attempt, per kind: a retry after a
  // lost 202 replays the stored response and hands back the ORIGINAL job id
  // (create_job_exclusive only dedups while that job is non-terminal — without the
  // key a late retry double-runs the full pipeline); a trigger that SUCCEEDED
  // clears the key so the next click is a deliberate new run.
  const attemptKeys = useRef<Partial<Record<TriggerKind, string>>>({});
  function run(kind: TriggerKind) {
    const key = (attemptKeys.current[kind] ??= crypto.randomUUID());
    trigger.mutate(
      { kind, idempotencyKey: key },
      {
        onSuccess: (job) => {
          delete attemptKeys.current[kind];
          setAccepted(job);
        },
      },
    );
  }

  return (
    <section className="import__section">
      <h2>Run pipeline</h2>
      <p className="runs__muted">
        Both Ingest and Build run the full six-stage pipeline (ingest → summarize) — they differ
        only in the recorded job kind, and either way spends graph, LLM, and indexing work. One run
        at a time per project.
      </p>
      {ontologyBlocked && (
        <p className="npf__error">
          This project has no valid ontology configured (entity_types + relation_types both
          non-empty), so a build over text sources fails before the graph stage. Set{" "}
          <code>config.ontology</code> via the API/CLI (structured sources don&apos;t need one).
        </p>
      )}
      {unresolvable.length > 0 && (
        <p className="npf__error">
          {unresolvable.length} source{unresolvable.length > 1 ? "s" : ""} can&apos;t be resolved by
          the pipeline (unwired kind, non-canonical <code>file:///</code> uri, or missing structured
          table/pk_column) — a run would fail at ingest or silently read a reinterpreted path, until
          they&apos;re fixed or removed via the API/CLI.
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
