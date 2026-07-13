import { useRef, useState } from "react";

import {
  DEFAULT_CHUNKING,
  chunkingFromConfig,
  usePreviewClean,
  useProject,
  useSaveChunking,
} from "../api/queries";
import { useDocuments } from "../api/queries";
import { isPathAddressable, useActiveProject } from "../project/projectRoute";
import "./Clean.css";

import type { CleanPreviewRequest, CleanPreviewResult } from "../api/queries";

// FE2 清洗 (DESIGN §10.2): tune chunking against real content BEFORE committing to a
// build. Two halves, deliberately on one page: PREVIEW (the v1.1 clean/preview RPC —
// pure, nothing persisted) answers "how would THESE parameters chunk THIS content?",
// and SAVE writes the parameters into project config for the next build via the
// existing PATCH (spreading the current config — the PATCH replaces the whole column,
// so a naive {config:{chunking}} would wipe ontology and every other block).
//
// The preview is a mutation, not a query — no cache, so no stale-while-revalidate
// window (class 17). But its result still goes stale the moment the INPUTS change:
// the page tracks that explicitly and labels the old result rather than showing
// chunks that silently answer parameters no longer on screen.

const MAX_TEXT = 100_000; // preview is synchronous — keep pasted text sane

function message(error: unknown): string {
  return error instanceof Error ? error.message : "unknown error";
}

export function Clean() {
  const project = useActiveProject();

  if (project === undefined) return <p className="clean__line">Unknown project.</p>;
  if (!isPathAddressable(project))
    return (
      <p className="clean__line clean__line--error">
        Project &quot;{project}&quot; isn&apos;t addressable over the API — its key contains
        &quot;/&quot; or is &quot;.&quot; / &quot;..&quot;, which a URL path segment can&apos;t
        carry.
      </p>
    );

  return <CleanBody project={project} />;
}

function CleanBody({ project }: { project: string }) {
  const proj = useProject(project);
  const preview = usePreviewClean(project);
  const save = useSaveChunking(project);

  const [source, setSource] = useState<"text" | "document">("text");
  const [text, setText] = useState("");
  const [documentId, setDocumentId] = useState("");
  const [maxCharsText, setMaxCharsText] = useState("");
  const [overlapText, setOverlapText] = useState("");
  // the result answers the INPUTS it was made with — edits after a preview make it
  // stale, and a stale preview must say so rather than impersonate the new inputs.
  // The version counter closes the in-flight window: an edit DURING a pending
  // preview must leave the settled result flagged, so success only clears the
  // flag when no input moved since the mutate captured its body.
  const [stale, setStale] = useState(false);
  const inputVersion = useRef(0);

  // Loading / failed project reads fail CLOSED: the save path spreads the loaded
  // config, so rendering the form without it risks saving a wipe (see queries.ts).
  if (proj.isFetching) return <p className="clean__line">Loading project…</p>;
  if (proj.isError)
    return (
      <p className="clean__line clean__line--error">
        Could not load the project: {message(proj.error)}
      </p>
    );
  const config = (proj.data?.config ?? {}) as Record<string, unknown>;
  const configured = chunkingFromConfig(config);

  // knobs: empty input = omit (server falls back to project config, then engine
  // defaults — the same chain a build walks). Number inputs can't produce bools,
  // and the API rejects them anyway (strict integers, v1.1).
  const maxChars = maxCharsText.trim() === "" ? undefined : Number(maxCharsText);
  const overlap = overlapText.trim() === "" ? undefined : Number(overlapText);

  // Mirror chunk_text's pair rule for the values that will actually apply —
  // typed knob, else the project's configured value, else the engine default.
  // WHY mirror at all (class-15 criterion): a bad pair SAVED into config fails
  // LATE — at the next build's config load — not here; preview alone can't
  // catch what an operator saves without previewing.
  const effectiveMax = maxChars ?? configured.max_chars;
  const effectiveOverlap = overlap ?? configured.overlap;
  const pairError =
    !Number.isInteger(effectiveMax) || effectiveMax < 1
      ? "max_chars must be a positive integer"
      : !Number.isInteger(effectiveOverlap) ||
          effectiveOverlap < 0 ||
          effectiveOverlap >= effectiveMax
        ? `overlap must satisfy 0 <= overlap < max_chars (got ${effectiveOverlap} / ${effectiveMax})`
        : null;

  const sourceReady = source === "text" ? text.length > 0 : documentId !== "";

  function knobs(): { max_chars?: number; overlap?: number } {
    return {
      ...(maxChars !== undefined ? { max_chars: maxChars } : {}),
      ...(overlap !== undefined ? { overlap } : {}),
    };
  }

  function runPreview() {
    const body: CleanPreviewRequest =
      source === "text" ? { text, ...knobs() } : { document_id: documentId, ...knobs() };
    const version = inputVersion.current;
    preview.mutate(body, {
      onSuccess: () => setStale(inputVersion.current !== version),
    });
  }

  function markStale() {
    inputVersion.current += 1;
    if (preview.data) setStale(true);
  }

  return (
    <section className="clean">
      <h1 className="clean__title">Clean</h1>
      <p className="clean__hint">
        Preview how parameters chunk real content — nothing is stored — then save them to the
        project config for the next build.
      </p>

      <div className="clean__form">
        <fieldset className="clean__source">
          <legend>Source</legend>
          <label>
            <input
              type="radio"
              name="source"
              checked={source === "text"}
              onChange={() => {
                setSource("text");
                markStale();
              }}
            />
            Paste text
          </label>
          <label>
            <input
              type="radio"
              name="source"
              checked={source === "document"}
              onChange={() => {
                setSource("document");
                markStale();
              }}
            />
            Ingested document (active build)
          </label>
        </fieldset>

        {source === "text" ? (
          <label className="clean__field">
            text
            <textarea
              value={text}
              maxLength={MAX_TEXT}
              rows={6}
              onChange={(e) => {
                setText(e.target.value);
                markStale();
              }}
            />
          </label>
        ) : (
          <DocumentPicker
            project={project}
            value={documentId}
            onChange={(id) => {
              setDocumentId(id);
              markStale();
            }}
          />
        )}

        <div className="clean__knobs">
          <label className="clean__field">
            max_chars
            <input
              type="number"
              min={1}
              placeholder={String(configured.max_chars)}
              value={maxCharsText}
              onChange={(e) => {
                setMaxCharsText(e.target.value);
                markStale();
              }}
            />
          </label>
          <label className="clean__field">
            overlap
            <input
              type="number"
              min={0}
              placeholder={String(configured.overlap)}
              value={overlapText}
              onChange={(e) => {
                setOverlapText(e.target.value);
                markStale();
              }}
            />
          </label>
          <p className="clean__muted">
            Empty = the project&apos;s configured value (shown as placeholder), falling back to the
            engine defaults ({DEFAULT_CHUNKING.max_chars}/{DEFAULT_CHUNKING.overlap}) — the same
            chain a build walks.
          </p>
        </div>

        {pairError && <p className="clean__line clean__line--error">{pairError}</p>}

        <div className="clean__actions">
          <button
            type="button"
            disabled={!sourceReady || pairError !== null || preview.isPending}
            onClick={runPreview}
          >
            {preview.isPending ? "Previewing…" : "Preview"}
          </button>
          <button
            type="button"
            disabled={pairError !== null || save.isPending}
            onClick={() => save.mutate({ max_chars: effectiveMax, overlap: effectiveOverlap })}
          >
            {save.isPending ? "Saving…" : `Save ${effectiveMax}/${effectiveOverlap} to config`}
          </button>
        </div>

        {save.isError && (
          <p className="clean__line clean__line--error">Save failed: {message(save.error)}</p>
        )}
        {save.isSuccess && !save.isPending && (
          <p className="clean__line">Saved — the next build will chunk with these values.</p>
        )}
      </div>

      {preview.isError && (
        <p className="clean__line clean__line--error">Preview failed: {message(preview.error)}</p>
      )}
      {preview.data && !preview.isPending && <PreviewResult result={preview.data} stale={stale} />}
    </section>
  );
}

// The picker reuses the FE3 documents list (same build-scoped read, opaque cursor).
function DocumentPicker({
  project,
  value,
  onChange,
}: {
  project: string;
  value: string;
  onChange: (id: string) => void;
}) {
  const docs = useDocuments(project);
  if (docs.isPending) return <p className="clean__muted">Loading documents…</p>;
  if (docs.isError)
    return (
      <p className="clean__line clean__line--error">
        Could not load documents: {message(docs.error)}
      </p>
    );
  const rows = (docs.data?.pages ?? []).flatMap((p) => p.rows);
  if (rows.length === 0)
    return <p className="clean__muted">No documents in the active build — paste text instead.</p>;
  return (
    <label className="clean__field">
      document
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">— choose a document —</option>
        {rows.map((d) => (
          <option key={d.id} value={d.id}>
            {d.source_uri}
          </option>
        ))}
      </select>
      {docs.hasNextPage && (
        <button
          type="button"
          className="clean__more"
          disabled={docs.isFetchingNextPage}
          onClick={() => docs.fetchNextPage()}
        >
          {docs.isFetchingNextPage ? "Loading…" : "Load more documents"}
        </button>
      )}
    </label>
  );
}

function PreviewResult({ result, stale }: { result: CleanPreviewResult; stale: boolean }) {
  return (
    <div className="clean__result">
      <h2 className="clean__subtitle">
        {result.chunks.length} chunk{result.chunks.length === 1 ? "" : "s"}
        {result.buildId && (
          <span className="clean__muted"> · from active build {result.buildId}</span>
        )}
      </h2>
      {stale && (
        <p className="clean__line clean__line--error">
          Parameters or source changed since this preview — run Preview again before trusting these
          chunks.
        </p>
      )}
      <table className="clean__table">
        <thead>
          <tr>
            <th>Ordinal</th>
            <th>Offsets</th>
            <th>Tokens</th>
            <th>Text</th>
          </tr>
        </thead>
        <tbody>
          {result.chunks.map((c) => (
            <tr key={c.ordinal}>
              <td>{c.ordinal}</td>
              <td>
                [{c.start_offset}, {c.end_offset})
              </td>
              <td>{c.token_count}</td>
              <td className="clean__chunk">{c.text}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
