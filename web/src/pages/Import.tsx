import { useEffect, useRef, useState } from "react";

import {
  useAddSource,
  useProjects,
  useSources,
  useTrigger,
  useUploadDocuments,
} from "../api/queries";
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
// pk_column in metadata; "xlsx" (SRC1) reads a file:// WORKBOOK and requires the
// column mapping (title_column + body_column; optional id_column/extra_columns/
// label) in metadata — its rows render to per-row TEXT documents, so it is
// ontology-gated like "text". All require a file:// uri — the only wired scheme
// (_local_path rejects others). The store/API accept any kind/uri string, but
// resolve_source fails the build loud otherwise, so the UI offers only these kinds,
// collects the per-kind metadata, and blocks a non-file:// uri — never letting
// the operator register a source whose build is guaranteed to fail. The contract's
// Source.kind doc lists file/directory/url/database as illustrative connector
// kinds, but those have no C2 connector yet (Codex #70).
const SOURCE_KINDS = ["text", "structured", "xlsx"] as const;
type SourceKind = (typeof SOURCE_KINDS)[number];

// Whether a uri is a canonical file:/// path — the exact form the backend reads.
// _local_path (core/builds/sources.py) reads urlparse(uri).path — and since BA9
// it RAISES on every non-canonical form below, so this gate is the pre-submit UX
// mirror of the SoR rule, not the only defense. Historically: a
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
  // No literal C0 control characters anywhere: Python's urlsplit strips tab/LF/CR
  // at ANY position (and control-or-space at the ends) before .path, so a raw
  // "file:///tmp/\t../etc" displays a "\t.." segment but READS /tmp/../etc →
  // /etc. Percent-encoded controls (other than %00, rejected below) decode to
  // literal filename bytes read verbatim — consistent with the display — so only
  // raw controls are display≠read fuel.
  if (/[\u0000-\u001f]/.test(raw)) return false;
  // Encoded separators hide the segment boundary from the display: no filesystem
  // permits "/" in a filename, so %2F can only be a disguised separator - one
  // canonical shape, separators are literal. A decoded backslash is a SEPARATOR
  // on a Windows worker (url2pathname), so it rejects below too. Both mirror the
  // SoR rule _local_path now enforces (BA9) - this gate is the pre-submit UX;
  // the backend refuses the same forms at build time for CLI/API/MCP callers.
  if (/%2f/i.test(raw)) return false;
  // Same reason for the drive separator: url2pathname detects the drive from a LITERAL
  // ":" in the still-ENCODED path, while the segment checks below run on the decoded
  // one — so "file:///C%3A/corpus" would pass them and yet read "\C:\corpus" (no
  // drive) instead of "C:\corpus". Every structural character the gate accepts must be
  // literal, or the check and the read disagree.
  if (/%3a/i.test(raw)) return false;
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
    // A malformed escape ("/data/100%") throws here but is LITERAL to Python's unquote,
    // which never raises — so the SoR refuses this shape explicitly (BA9) rather than
    // letting the two gates disagree. The canonical "%25" spelling decodes fine and
    // stays registerable on both sides.
    return false;
  }
  // An embedded NUL (file:///data/%00corpus) can't name a real file on any
  // supported OS — the connector's read is guaranteed to fail.
  if (decoded.includes("\0")) return false;
  // a decoded backslash (%5C or literal) is a path SEPARATOR on a Windows worker
  if (decoded.includes("\\")) return false;
  // ...and so is a pipe: url2pathname's first act is replace(":", "|"), so the pipe IS
  // the Windows DRIVE separator ("file:///a|/corpus" reads A:\corpus). Windows reserves
  // "|" in filenames outright, so refusing it everywhere costs nothing.
  if (decoded.includes("|")) return false;
  if (decoded.length <= 1) return false;
  // Every segment of the path the WORKER will read must be non-empty (an empty
  // segment means a "//" — UNC/root reinterpretation) and not "." / ".." (the
  // filesystem would resolve them away from the displayed path). One trailing
  // slash is allowed — the idiomatic directory form. A colon is legal only in the
  // leading DRIVE segment ("file:///C:/data", the canonical Windows form): elsewhere
  // url2pathname reads it as the drive separator and silently re-roots the path
  // ("/data/foo:bar" opens "O:bar"). WHATWG rewrites "a|" → "a:" in url.pathname, so
  // only this raw-derived path — the substring the backend's urlparse returns — sees
  // these at all. Mirrors the SoR rule _local_path enforces (BA9).
  const path = decoded.endsWith("/") ? decoded.slice(0, -1) : decoded;
  const segments = path.split("/").slice(1);
  if (segments.length === 0) return false;
  // A bare drive with no trailing slash ("file:///C:") is DRIVE-RELATIVE: url2pathname
  // yields Path("C:"), the worker's current directory on that drive, not the drive root
  // — the Windows spelling of the cwd hazard. "file:///C:/" is the root and is fine.
  if (segments.length === 1 && /^[A-Za-z]:$/.test(segments[0]) && !decoded.endsWith("/"))
    return false;
  return segments.every(
    (s, i) =>
      s !== "" &&
      s !== "." &&
      s !== ".." &&
      (!s.includes(":") || (i === 0 && /^[A-Za-z]:$/.test(s))),
  );
}

// The Console's half of the canonical-uri contract, as one named rule: the uri EXACTLY
// as stored (never a trimmed view — Python's urlparse keeps a trailing space in the
// path, while new URL()/trim() normalize it away, so edge whitespace is itself a
// display≠read divergence) must be a canonical file:/// uri. This must accept exactly
// the set core.builds.sources._local_path accepts; tests/fixtures/canonical_file_uri.json
// is the shared corpus that enforces that parity from both suites (BA9).
export function isCanonicalFileUri(uri: string): boolean {
  return uri === uri.trim() && isFileUri(uri);
}

// Whether the pipeline can resolve an already-registered source to the path the
// operator registered. Two failure families, both blocking (Codex #70): (1)
// resolve_source RAISES (core/builds/sources.py) on a kind outside the wired
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
  if (s.kind !== "text" && s.kind !== "structured" && s.kind !== "xlsx") return false;
  // (The add form trims before POST, so only sources registered outside the form can
  // carry edge whitespace — but they can, so the stored uri is checked as stored.)
  if (!isCanonicalFileUri(s.uri)) return false;
  if (s.kind === "structured") {
    const table = s.metadata?.table;
    const pk = s.metadata?.pk_column;
    if (typeof table !== "string" || table.trim() === "") return false;
    if (typeof pk !== "string" || pk.trim() === "") return false;
  }
  if (s.kind === "xlsx") {
    // mirror of _xlsx_required (core/builds/sources.py): the mapping's two
    // required columns. The OPTIONAL keys are deliberately not mirrored: a
    // present-but-malformed id_column/extra_columns/label (only reachable via
    // CLI/API registration) still fails the build loud server-side — this
    // gate mirrors what the FORM can produce, and over-mirroring every
    // optional shape would fork the validator (class-5 tax) for a path the
    // SoR already refuses honestly.
    const title = s.metadata?.title_column;
    const body = s.metadata?.body_column;
    if (typeof title !== "string" || title.trim() === "") return false;
    if (typeof body !== "string" || body.trim() === "") return false;
  }
  return true;
}

// FE1 Import (DESIGN §5/§15): register sources into the active project by URI/
// connector, upload document files into the managed corpus (UXC2b — contract
// v1.2's upload endpoint superseded the 2026-07-12 "no byte upload" scope
// decision; owner approved the upload track 2026-07-14), then trigger ingest
// (stage 1) or a full build and watch the job live. Same project-addressability
// guards as the other pages.
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

  // Three ontology states once the project resolves (undefined while the list
  // loads → don't gate yet): ABSENT is fine for structured-only builds (gated
  // only when text sources exist); PRESENT-BUT-INVALID fails _load_ontology in
  // the worker preflight for EVERY run regardless of source kinds ("ontology" in
  // config is enough to enter the validation branch — even `ontology: null`), so
  // RunPipeline blocks all runs on it; PRESENT-VALID gates nothing.
  const active = projects.data?.find((p) => p.name === project);
  const ontologyMissing = active !== undefined && !hasOntology(active.config);
  const ontologyInvalid =
    active !== undefined &&
    active.config != null &&
    "ontology" in active.config &&
    !hasOntology(active.config);

  return (
    <section className="import">
      <h1 className="import__title">匯入資料</h1>
      <p className="import__sub">
        目前專案:<code>{project}</code>
      </p>
      <Sources project={project} />
      <UploadSection
        project={project}
        requiredAttrs={requiredAttrs(active?.config)}
        // the same fail-closed predicate RunPipeline gates on: while the
        // config is loading/refetching/errored, "no required attrs" is
        // indistinguishable from "unknown" — submitting then would recreate
        // the configured-project dead end this section closes (Codex #83)
        configLoaded={projects.data !== undefined && !projects.isFetching && !projects.isError}
      />
      <RunPipeline
        project={project}
        ontologyMissing={ontologyMissing}
        ontologyInvalid={ontologyInvalid}
        // fail closed while the config is loading, refetching, OR errored —
        // react-query keeps the previous config in data during the flight and
        // after a failed refetch, and a CLI-side ontology change must not be
        // gated on that stale snapshot
        gatesLoaded={projects.data !== undefined && !projects.isFetching && !projects.isError}
      />
      <section className="import__section">
        <h2>建立新專案</h2>
        <p className="runs__muted">建立另一個專案並切換過去。</p>
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
  const [titleColumn, setTitleColumn] = useState("");
  const [bodyColumn, setBodyColumn] = useState("");
  const [idColumn, setIdColumn] = useState("");
  const [extraColumns, setExtraColumns] = useState("");
  const [rowLabel, setRowLabel] = useState("");
  const sources = useSources(project);
  const add = useAddSource(project);

  // One Idempotency-Key per LOGICAL attempt: retrying the same form contents after
  // a lost 201 replays the original row instead of duplicating it, while any edit
  // (including the post-success clear) mints a fresh key so a deliberately
  // re-typed duplicate registration still goes through.
  const attemptKey = useRef(crypto.randomUUID());
  useEffect(() => {
    attemptKey.current = crypto.randomUUID();
  }, [uri, kind, table, pkColumn, titleColumn, bodyColumn, idColumn, extraColumns, rowLabel]);

  // A structured source needs table + pk_column, an xlsx source needs its two
  // required mapping columns — or resolve_source fails the build, so gate the
  // submit exactly as the connector requires.
  const structured = kind === "structured";
  const xlsx = kind === "xlsx";
  const metaReady = structured
    ? table.trim() !== "" && pkColumn.trim() !== ""
    : xlsx
      ? titleColumn.trim() !== "" && bodyColumn.trim() !== ""
      : true;
  // The only wired resolver is file://; anything else (https://, a bare path) is a
  // guaranteed build failure, so refuse it at the source rather than POST it.
  const badScheme = uri.trim() !== "" && !isFileUri(uri.trim());
  const canAdd = uri.trim() !== "" && !badScheme && metaReady && !add.isPending;

  function xlsxMetadata(): Record<string, unknown> {
    const out: Record<string, unknown> = {
      title_column: titleColumn.trim(),
      body_column: bodyColumn.trim(),
    };
    if (idColumn.trim()) out.id_column = idColumn.trim();
    const extras = extraColumns
      .split(",")
      .map((c) => c.trim())
      .filter((c) => c !== "");
    if (extras.length > 0) out.extra_columns = extras;
    if (rowLabel.trim()) out.label = rowLabel.trim();
    return out;
  }

  function submit() {
    add.mutate(
      {
        uri: uri.trim(),
        kind,
        metadata: structured
          ? { table: table.trim(), pk_column: pkColumn.trim() }
          : xlsx
            ? xlsxMetadata()
            : undefined,
        idempotencyKey: attemptKey.current,
      },
      {
        onSuccess: () => {
          setUri("");
          setTable("");
          setPkColumn("");
          setTitleColumn("");
          setBodyColumn("");
          setIdColumn("");
          setExtraColumns("");
          setRowLabel("");
        },
      },
    );
  }

  return (
    <section className="import__section">
      <h2>資料來源</h2>
      <p className="runs__muted">
        來源是伺服器本機的 <code>file:///</code> 路徑(例:
        <code>file:///C:/data/corpus</code>):<b>text</b> 讀整個資料夾的
        <code>.txt</code>/<code>.md</code>;<b>structured</b> 讀單一 CSV 檔;<b>xlsx</b>{" "}
        讀單一試算表——每列渲染成一份文字文件,欄位對應(哪一欄是標題/內文)填在下方。
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
        {xlsx && (
          <>
            <label className="npf__field">
              title_column(必填)
              <input
                value={titleColumn}
                onChange={(e) => setTitleColumn(e.target.value)}
                placeholder="標題"
              />
            </label>
            <label className="npf__field">
              body_column(必填)
              <input
                value={bodyColumn}
                onChange={(e) => setBodyColumn(e.target.value)}
                placeholder="內容詳情"
              />
            </label>
            <label className="npf__field">
              id_column
              <input
                value={idColumn}
                onChange={(e) => setIdColumn(e.target.value)}
                placeholder="編號"
              />
            </label>
            <label className="npf__field">
              extra_columns(逗號分隔)
              <input
                value={extraColumns}
                onChange={(e) => setExtraColumns(e.target.value)}
                placeholder="位置, 分類"
              />
            </label>
            <label className="npf__field">
              label
              <input
                value={rowLabel}
                onChange={(e) => setRowLabel(e.target.value)}
                placeholder="導覽"
              />
            </label>
          </>
        )}
        <button type="submit" disabled={!canAdd}>
          {add.isPending ? "登記中…" : "登記來源"}
        </button>
        {badScheme && (
          <p className="npf__error">
            The uri must be a canonical <code>file:///</code> path (three slashes, no host) — the
            backend reads only the path part, so any other form is unwired or misread.
          </p>
        )}
        {add.isError && (
          <p className="npf__error">
            登記失敗:{add.error instanceof Error ? add.error.message : "unknown error"}
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

// The project's REQUIRED context attributes (config.metadata_schema.attributes
// entries with required: true) — the fields the upload endpoint refuses a file
// WITHOUT (core/metadata/schema.py validates even an absent context against
// the declared schema). This mirror exists only to GENERATE the per-file
// inputs; the verdict stays the server's (a lenient parse of a malformed
// block yields no fields, and the endpoint's own config load fails the
// request loudly). Optional attributes and the core title/document_type stay
// API/CLI territory — the dead end this closes is the REQUIRED ones, without
// which a configured project can never accept any upload (Codex #83).
type RequiredAttr = { name: string; type: "string" | "number" | "boolean" };

function requiredAttrs(config: Project["config"] | undefined): RequiredAttr[] {
  const block = (config as Record<string, unknown> | undefined)?.metadata_schema;
  if (typeof block !== "object" || block === null || Array.isArray(block)) return [];
  const attrs = (block as Record<string, unknown>).attributes;
  if (typeof attrs !== "object" || attrs === null || Array.isArray(attrs)) return [];
  const out: RequiredAttr[] = [];
  for (const [name, defn] of Object.entries(attrs as Record<string, unknown>)) {
    if (typeof defn !== "object" || defn === null) continue;
    const rec = defn as Record<string, unknown>;
    if (rec.required !== true) continue;
    const t = rec.type;
    if (t === "string" || t === "number" || t === "boolean") out.push({ name, type: t });
  }
  return out;
}

// 上傳檔案 (UXC2b): drag-drop or file-pick → multipart upload → the server's
// per-file accepted/rejected manifest rendered honestly.
//
// State machine (class 20/26):
// - selection: File[] local state; empty → 上傳 disabled. Picking/dropping
//   REPLACES the selection (one batch at a time) and RESETS the mutation so a
//   previous batch's verdicts never sit beside a new selection as if they
//   were about it.
// - Idempotency-Key: one per (selection, logical attempt) — minted when the
//   selection changes (the register-form discipline above), REUSED on a retry
//   after a whole-request failure (a lost 201 replays the stored manifest
//   instead of re-writing the corpus), and the post-success selection clear
//   mints a fresh key for the next batch.
// - The manifest is the mutation RESULT (server truth), keyed by
//   original_filename (the row identity a human recognizes): accepted rows
//   carry the stored corpus uri on hover ONLY (the stored filename is a hex
//   token and source ids are uuids — chrome shows words, not identifiers);
//   rejected rows show the server's reason verbatim (a refused extension is a
//   STATED refusal, never a silent drop).
// - No client-side pre-rejection: the accept attr is an affordance, the
//   server's manifest is the verdict — a client allowlist would fork from
//   core/ingest/connectors.py TEXT_SUFFIXES (checker/consumer split) and the
//   endpoint already refuses per-file loudly.
// - Required metadata: when the project declares required context attributes,
//   each picked file gets inputs for exactly those fields (string/number →
//   typed inputs, boolean → checkbox). A BLANK field is OMITTED from the
//   payload — the server then refuses that file with its own "required
//   attribute missing" reason (an empty string is server-legal, so a client
//   non-blank gate would over-block; omission keeps the verdict honest and
//   server-owned). Values reset with the selection (they belong to the batch).
// - Fail closed on UNKNOWN config: while the projects read is loading,
//   refetching, or errored, requiredAttrs=[] means "unknown", not "none" —
//   submitting then would silently skip the metadata form and recreate the
//   configured-project dead end. The submit stays locked (with an honest
//   line, not a silent disabled button) until the config is current — the
//   UXB1 form discipline, same predicate as RunPipeline's gatesLoaded.
function UploadSection({
  project,
  requiredAttrs,
  configLoaded,
}: {
  project: string;
  requiredAttrs: RequiredAttr[];
  configLoaded: boolean;
}) {
  const upload = useUploadDocuments(project);
  const [picked, setPicked] = useState<File[]>([]);
  const [attrValues, setAttrValues] = useState<Record<string, string | boolean>>({});
  const attemptKey = useRef(crypto.randomUUID());

  // ANY edit mints a fresh key (the source form's field-change discipline):
  // the server hashes the metadata content into the idempotency fingerprint,
  // so retrying an edited batch under the OLD key would 409
  // IDEMPOTENCY_CONFLICT instead of submitting the correction — only an
  // UNCHANGED retry may replay (Codex #83 triage 3). Extra mints (mount, the
  // pick() reset) are harmless: freshness only matters at submit.
  useEffect(() => {
    attemptKey.current = crypto.randomUUID();
  }, [attrValues]);

  function pick(files: FileList | null) {
    if (!files || files.length === 0) return;
    setPicked(Array.from(files));
    setAttrValues({});
    attemptKey.current = crypto.randomUUID();
    upload.reset();
  }

  // one flat key per (file position, attribute) — file NAME alone would
  // collide for a batch carrying duplicate names (the server rejects those
  // whole-request, but the inputs must not alias before that verdict)
  function valueKey(fileIndex: number, attr: string): string {
    return `${fileIndex} ${attr}`;
  }

  function metadataPayload(): Record<string, unknown> | undefined {
    if (requiredAttrs.length === 0) return undefined;
    const out: Record<string, unknown> = {};
    picked.forEach((f, i) => {
      const attributes: Record<string, unknown> = {};
      for (const a of requiredAttrs) {
        const v = attrValues[valueKey(i, a.name)];
        if (a.type === "boolean") {
          attributes[a.name] = v === true; // a checkbox always answers
        } else if (typeof v === "string" && v.trim() !== "") {
          const parsed = a.type === "number" ? Number(v) : v;
          if (a.type === "number" && Number.isNaN(parsed as number)) continue;
          attributes[a.name] = parsed;
        }
      }
      if (Object.keys(attributes).length > 0) out[f.name] = { context: { attributes } };
    });
    return Object.keys(out).length > 0 ? out : undefined;
  }

  function submit() {
    if (picked.length === 0 || upload.isPending || !configLoaded) return;
    upload.mutate(
      { files: picked, metadata: metadataPayload(), idempotencyKey: attemptKey.current },
      { onSuccess: () => setPicked([]) },
    );
  }

  const manifest = upload.data?.files ?? null;
  const accepted = manifest?.filter((f) => f.status === "accepted") ?? [];
  const rejected = manifest?.filter((f) => f.status === "rejected") ?? [];

  return (
    <section className="import__section">
      <h2>上傳檔案</h2>
      <p className="runs__muted">
        直接把 <code>.txt</code>/<code>.md</code> 檔上傳到伺服器的專案語料夾——上傳成功後,
        對應的來源會自動出現在上面的清單裡。
      </p>
      <div
        className="import__dropzone"
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          pick(e.dataTransfer.files);
        }}
      >
        <span>把檔案拖進來,或</span>
        <label className="import__filepick">
          選擇檔案
          <input
            type="file"
            multiple
            accept=".txt,.md"
            onChange={(e) => {
              pick(e.target.files);
              e.target.value = "";
            }}
          />
        </label>
      </div>
      {picked.length > 0 && requiredAttrs.length === 0 && (
        <ul className="import__picked">
          {picked.map((f, i) => (
            <li key={`${i}:${f.name}`}>{f.name}</li>
          ))}
        </ul>
      )}
      {picked.length > 0 && requiredAttrs.length > 0 && (
        <div className="import__filemetas">
          <p className="runs__muted">這個專案要求每個檔案填寫下列欄位(留空會被伺服器退回):</p>
          {picked.map((f, i) => (
            <fieldset key={`${i}:${f.name}`} className="import__filemeta">
              <legend>{f.name}</legend>
              {requiredAttrs.map((a) =>
                a.type === "boolean" ? (
                  <label key={a.name} className="import__metafield">
                    {a.name}
                    <input
                      type="checkbox"
                      checked={attrValues[valueKey(i, a.name)] === true}
                      onChange={(e) =>
                        setAttrValues((prev) => ({
                          ...prev,
                          [valueKey(i, a.name)]: e.target.checked,
                        }))
                      }
                    />
                  </label>
                ) : (
                  <label key={a.name} className="import__metafield">
                    {a.name}(必填)
                    <input
                      type={a.type === "number" ? "number" : "text"}
                      value={
                        typeof attrValues[valueKey(i, a.name)] === "string"
                          ? (attrValues[valueKey(i, a.name)] as string)
                          : ""
                      }
                      onChange={(e) =>
                        setAttrValues((prev) => ({
                          ...prev,
                          [valueKey(i, a.name)]: e.target.value,
                        }))
                      }
                    />
                  </label>
                ),
              )}
            </fieldset>
          ))}
        </div>
      )}
      <button
        type="button"
        onClick={submit}
        disabled={picked.length === 0 || upload.isPending || !configLoaded}
      >
        {upload.isPending ? "上傳中…" : "上傳"}
      </button>
      {picked.length > 0 && !configLoaded && (
        <p className="runs__muted">正在確認專案設定(必填欄位)——設定讀取完成前暫停上傳。</p>
      )}
      {upload.isError && (
        <p className="npf__error">
          上傳失敗:{upload.error instanceof Error ? upload.error.message : "unknown error"}
        </p>
      )}
      {manifest && (
        <div className="import__manifest">
          <p className="import__manifestsummary">
            接受 {accepted.length} 檔 · 退回 {rejected.length} 檔
          </p>
          <ul className="import__manifestrows">
            {manifest.map((f, i) =>
              f.status === "accepted" ? (
                <li key={`${i}:${f.original_filename}`}>
                  <span className="runs__badge runs__badge--ok">已接受</span>{" "}
                  <span title={f.document_uri}>{f.original_filename}</span>
                </li>
              ) : (
                <li key={`${i}:${f.original_filename}`}>
                  <span className="runs__badge runs__badge--bad">已退回</span>{" "}
                  <span>{f.original_filename}</span>
                  <span className="import__reason">{f.reason}</span>
                </li>
              ),
            )}
          </ul>
        </div>
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
  ontologyInvalid,
  gatesLoaded,
}: {
  project: string;
  ontologyMissing: boolean;
  ontologyInvalid: boolean;
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
  // xlsx rows render to per-row TEXT documents (SRC1), so they hit the same
  // OntologyRequiredError as a text directory — both kinds arm the gate
  // SRC2: a disabled source is excluded from the build (`_load_sources`
  // enabled_only=True), so it must not arm ANY build-eligibility gate —
  // otherwise disabling a broken/text source can't recover the build, defeating
  // the whole point of soft-disable. Gate over the enabled subset only; the
  // list rendering below still shows disabled rows so an operator can re-enable.
  // A missing `enabled` (pre-SRC2 server) counts as enabled.
  const buildableSources = (sources.data ?? []).filter((s) => s.enabled !== false);
  const hasTextSource = buildableSources.some((s) => s.kind === "text" || s.kind === "xlsx");
  const ontologyBlocked = ontologyMissing && hasTextSource;
  // One unresolvable source (unwired kind / non-file scheme / missing structured
  // metadata — e.g. registered via CLI/API) fails every build at ingest. A
  // present-but-invalid ontology (ontologyInvalid) fails the worker preflight for
  // EVERY run — even structured-only — so it blocks unconditionally.
  const unresolvable = buildableSources.filter((s) => !isResolvableSource(s));
  const blocked = ontologyBlocked || ontologyInvalid || unresolvable.length > 0;
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
      <h2>建置</h2>
      <p className="runs__muted">
        把登記好的資料變成可查詢的知識庫(讀取 → 清洗 → 圖譜抽取 → 索引 → 摘要,會呼叫
        LLM,需要幾分鐘)。一個專案一次跑一個。
      </p>
      {ontologyInvalid && (
        <p className="npf__error">
          <code>config.ontology</code> is present but invalid (needs entity_types + relation_types,
          both non-empty string arrays) — every run fails at config load. Fix or remove it via the
          API/CLI.
        </p>
      )}
      {ontologyBlocked && !ontologyInvalid && (
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
      {/* one verb (UXA3): ingest and build run the SAME six-stage pipeline
          and differ only in the recorded job kind (Codex #70's finding) — two
          buttons plus an engineering apology was the API leaking into the UI.
          The Console always records the run as a build. */}
      <div className="import__actions">
        <button
          type="button"
          onClick={() => run("build")}
          disabled={!ready || trigger.isPending || blocked}
        >
          {trigger.isPending ? "啟動中…" : "開始建置"}
        </button>
      </div>
      {trigger.isError && (
        <p className="npf__error">
          建置啟動失敗:{trigger.error instanceof Error ? trigger.error.message : "unknown error"}
        </p>
      )}
      {accepted && (
        // words visible, id on hover (UXA3) — the live progress below is the
        // thing the operator actually watches
        <p className="runs__muted" title={accepted.job_id}>
          建置已排入佇列,進度如下。
        </p>
      )}
      <JobProgress jobId={accepted?.job_id ?? null} />
    </section>
  );
}
