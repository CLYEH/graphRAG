import { useState } from "react";

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
  // Staleness is a COMPARISON, not an event trail (Codex, #74): the preview
  // answers the EFFECTIVE tuple it was made with (source content + the pair the
  // server would resolve), so the page snapshots that tuple at mutate time and
  // recomputes "stale" every render. Anything that moves the current tuple —
  // typed edits, a source switch, or a useProject refetch changing the CONFIG
  // fallbacks with no input event at all — flags the old result automatically,
  // including edits that land while the request is on the wire. Same shape for
  // the save confirmation: it names the pair it saved and only stands while the
  // effective pair still matches.
  const [previewedWith, setPreviewedWith] = useState<{
    sourceKey: string;
    max: number;
    overlap: number;
  } | null>(null);
  const [savedPair, setSavedPair] = useState<{ max: number; overlap: number } | null>(null);

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

  const currentSourceKey = source === "text" ? `t:${text}` : `d:${documentId}`;
  const stale =
    preview.data !== undefined &&
    previewedWith !== null &&
    (previewedWith.sourceKey !== currentSourceKey ||
      previewedWith.max !== effectiveMax ||
      previewedWith.overlap !== effectiveOverlap);
  const savedStands =
    savedPair !== null && savedPair.max === effectiveMax && savedPair.overlap === effectiveOverlap;

  function runPreview() {
    const body: CleanPreviewRequest =
      source === "text" ? { text, ...knobs() } : { document_id: documentId, ...knobs() };
    setPreviewedWith({ sourceKey: currentSourceKey, max: effectiveMax, overlap: effectiveOverlap });
    preview.mutate(body);
  }

  function runSave() {
    // raw optional knobs — the mutation resolves omissions against the FRESH
    // config and returns the pair it actually wrote (the banner's truth)
    save.mutate(knobs(), {
      onSuccess: (r) => setSavedPair({ max: r.pair.max_chars, overlap: r.pair.overlap }),
    });
  }

  return (
    <section className="clean">
      <h1 className="clean__title">清洗(切塊預覽)</h1>
      <p className="clean__hint">
        先用真實內容預覽切塊效果(不會寫入任何資料),滿意後存進專案設定,下一次建置生效。
      </p>

      <div className="clean__form">
        <fieldset className="clean__source">
          <legend>內容來源</legend>
          <label>
            <input
              type="radio"
              name="source"
              checked={source === "text"}
              onChange={() => setSource("text")}
            />
            貼上文字
          </label>
          <label>
            <input
              type="radio"
              name="source"
              checked={source === "document"}
              onChange={() => setSource("document")}
            />
            選擇已匯入的文件
          </label>
        </fieldset>

        {source === "text" ? (
          <label className="clean__field">
            文字內容
            <textarea
              value={text}
              maxLength={MAX_TEXT}
              rows={6}
              onChange={(e) => setText(e.target.value)}
            />
          </label>
        ) : (
          <DocumentPicker
            project={project}
            value={documentId}
            onChange={(id) => setDocumentId(id)}
          />
        )}

        <div className="clean__knobs">
          <label className="clean__field">
            每塊字元上限(max_chars)
            <input
              type="number"
              min={1}
              placeholder={String(configured.max_chars)}
              value={maxCharsText}
              onChange={(e) => setMaxCharsText(e.target.value)}
            />
          </label>
          <label className="clean__field">
            重疊字元數(overlap)
            <input
              type="number"
              min={0}
              placeholder={String(configured.overlap)}
              value={overlapText}
              onChange={(e) => setOverlapText(e.target.value)}
            />
          </label>
          <p className="clean__muted">
            留空=用專案設定值(顯示為淡字),沒設定則用引擎預設(
            {DEFAULT_CHUNKING.max_chars}/{DEFAULT_CHUNKING.overlap})——與建置時的取值順序一致。
          </p>
        </div>

        {pairError && <p className="clean__line clean__line--error">{pairError}</p>}

        <div className="clean__actions">
          <button
            type="button"
            disabled={!sourceReady || pairError !== null || preview.isPending}
            onClick={runPreview}
          >
            {preview.isPending ? "預覽中…" : "預覽"}
          </button>
          <button type="button" disabled={pairError !== null || save.isPending} onClick={runSave}>
            {save.isPending ? "儲存中…" : `儲存 ${effectiveMax}/${effectiveOverlap} 到專案設定`}
          </button>
        </div>

        {save.isError && (
          <p className="clean__line clean__line--error">儲存失敗:{message(save.error)}</p>
        )}
        {save.isSuccess && !save.isPending && savedStands && (
          <p className="clean__line">
            已儲存 {savedPair?.max}/{savedPair?.overlap} — 下一次建置會用這組參數切塊。
          </p>
        )}
      </div>

      {preview.isError && (
        <p className="clean__line clean__line--error">預覽失敗:{message(preview.error)}</p>
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
          {docs.isFetchingNextPage ? "載入中…" : "載入更多"}
        </button>
      )}
    </label>
  );
}

function PreviewResult({ result, stale }: { result: CleanPreviewResult; stale: boolean }) {
  return (
    <div className="clean__result">
      <h2 className="clean__subtitle">
        {result.chunks.length} 個切塊
        {result.buildId && (
          // words on the surface, uuid on hover (UXA3): the build identity
          // matters for provenance but the identifier is not chrome
          <span className="clean__muted" title={result.buildId}>
            {" "}
            · 來自目前上線中的知識庫
          </span>
        )}
      </h2>
      {stale && (
        <p className="clean__line clean__line--error">
          參數或內容在預覽後改過了——請重新按「預覽」,別直接相信下面的結果。
        </p>
      )}
      <table className="clean__table">
        <thead>
          <tr>
            <th>序號</th>
            <th>位置(起-迄)</th>
            <th>字元數</th>
            <th>內容</th>
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
