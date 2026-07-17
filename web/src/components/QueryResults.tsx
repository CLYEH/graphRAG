import { useState } from "react";

import { useChunk, useDocument, useEntity, useRelation } from "../api/queries";
import type { QueryResult } from "../api/queries";

type WarningCode = QueryResult["warnings"][number]["code"];
type RetrievalResult = QueryResult["results"][number];
type SourceRef = RetrievalResult["source_refs"][number];

// WarningCode (DESIGN §22) → the shared badge tone. A degraded query comes back
// 200 with warnings (there is no status field), so these must render prominently.
const WARN_TONE: Record<WarningCode, string> = {
  STORE_UNAVAILABLE: "bad",
  MODE_SKIPPED: "muted",
  PARTIAL_RESULTS: "warn",
  LOW_CONFIDENCE: "warn",
  GUARDRAIL_BLOCKED: "bad",
  TRUNCATED: "warn",
};

// The deadline can fire during binding, leaving no build bound — the envelope
// then reports the nil uuid (core/mcp/server.py). Show that as degraded, not a id.
const NIL_UUID = "00000000-0000-0000-0000-000000000000";

export function QueryResults({ result, project }: { result: QueryResult; project?: string }) {
  const degraded = result.build_id === NIL_UUID;
  return (
    <div className="play__results">
      <div className="play__head">
        <span className="runs__badge runs__badge--info">{result.mode}</span>
        {/* the full build uuid rides the title attribute — visible chrome
            carries words, hover carries the identifier (UXA3 translation layer) */}
        <span className="play__meta" title={degraded ? undefined : result.build_id}>
          {degraded ? "版本:—(降級回應)" : "版本:目前上線中的知識庫"}
        </span>
        <span className="play__meta">{result.results.length} 筆結果</span>
      </div>

      {result.warnings.length > 0 && (
        <ul className="play__warnings">
          {result.warnings.map((w, i) => (
            <li key={i}>
              <span className={`runs__badge runs__badge--${WARN_TONE[w.code]}`}>{w.code}</span>{" "}
              {w.message}
            </li>
          ))}
        </ul>
      )}

      {/* a 200 with warnings and no rows is a degraded success, not "no results" */}
      {result.results.length === 0 && result.warnings.length === 0 && (
        <p className="runs__muted">沒有找到相關結果。</p>
      )}

      <ol className="play__hits">
        {result.results.map((r) => (
          <Hit key={`${r.result_type}:${r.id}`} hit={r} project={project} />
        ))}
      </ol>

      {result.graph_context && (
        <p className="play__graph">
          圖譜脈絡:{result.graph_context.nodes.length} 個節點、
          {result.graph_context.edges.length} 條關聯
          {result.graph_context.paths ? `、${result.graph_context.paths.length} 條路徑` : ""}
        </p>
      )}

      {result.debug?.routing_decision && (
        <p className="play__routing">
          <strong>routing:</strong> selected [{result.debug.routing_decision.selected.join(", ")}]
          {result.debug.routing_decision.skipped.length > 0 &&
            ` · skipped [${result.debug.routing_decision.skipped.join(", ")}]`}
          {result.debug.routing_decision.reason ? ` — ${result.debug.routing_decision.reason}` : ""}
        </p>
      )}
    </div>
  );
}

function Hit({ hit, project }: { hit: RetrievalResult; project?: string }) {
  // SS2: resolution fetches fire only once the fold is OPEN — a collapsed
  // fold must cost zero requests (a response can carry hundreds of refs).
  // The raw ref lines stay mounted regardless (UXA3: verbatim is the SoR of
  // this surface; the card is a translation layer ADDED next to it).
  const [open, setOpen] = useState(false);
  return (
    <li className="play__hit">
      <div className="play__hit-head">
        <span className="runs__badge runs__badge--muted">{hit.result_type}</span>
        {hit.title && <strong>{hit.title}</strong>}
        <span className="play__meta">相關度 {hit.score.toFixed(3)}</span>
        {hit.confidence != null && (
          <span className="play__meta">信心 {hit.confidence.toFixed(2)}</span>
        )}
      </div>
      {hit.text && <p className="play__text">{hit.text}</p>}
      {/* §16 traceability, folded (UXA3): rendering every ref as a full-width
          line built a 17,450px wall of uuids on real data — the refs (FULL ids:
          row refs are a lossless table:pk string per core row_source_ref, and
          truncation would make two row citations indistinguishable) now live
          behind a count-labelled disclosure, one click from verbatim. */}
      {hit.source_refs.length > 0 && (
        <details
          className="play__sources"
          onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
        >
          <summary>{hit.source_refs.length} 個來源引用</summary>
          <ul>
            {hit.source_refs.map((s, i) => (
              <li key={i}>
                <span className="runs__badge runs__badge--muted">{s.source_type}</span>{" "}
                <code>{s.id}</code>
                {/* source_uri is rendered as text, never an href — an untrusted
                    value in an <a href> would be a fresh injection sink (FE7) */}
                {s.source_uri ? <span className="play__uri"> · {s.source_uri}</span> : null}
                {open && project ? <SourceRefResolve s={s} project={project} /> : null}
              </li>
            ))}
          </ul>
        </details>
      )}
    </li>
  );
}

// ---- SS2 reference cards ----------------------------------------------------
//
// Each ref resolves to human words via the EXISTING detail reads (documents/
// chunks/entities/relations by id) — batched by react-query's per-id cache
// (a response citing one document 40 times fetches it once) and lazy (mounted
// only while the fold is open, above). Resolution strictly ADDS a line under
// the verbatim ref: while loading or after any error the raw id/uri stands
// alone, so a ref from an older build (detail 404 after activation) degrades
// to exactly the pre-SS2 rendering plus an honest miss note — never a blank.

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// the rebuild-stable chunk ref (core.graph.documents.chunk_source_ref):
// `chunk:<content_hash>:<ordinal>` — cited by entity/graph hits. It is not a
// row id, so it has no detail read; its parts are still worth words.
const STABLE_CHUNK_RE = /^chunk:([0-9a-f]+):(\d+)$/i;

function SourceRefResolve({ s, project }: { s: SourceRef; project: string }) {
  if (s.source_type === "row") {
    // the producer splits the lossless ref into metadata {table, pk}
    // (contract SourceRef.metadata) — render words when present, else stay raw
    const table = typeof s.metadata?.table === "string" ? s.metadata.table : null;
    const pk = typeof s.metadata?.pk === "string" ? s.metadata.pk : null;
    return table && pk ? (
      <span className="play__resolve">
        資料表 {table} · 主鍵 {pk}
      </span>
    ) : null;
  }
  if (s.source_type === "chunk") {
    const stable = STABLE_CHUNK_RE.exec(s.id);
    if (stable) {
      return (
        <span className="play__resolve">
          段落 #{stable[2]} · 內容雜湊 {stable[1].slice(0, 12)}…
        </span>
      );
    }
    return UUID_RE.test(s.id) ? <ChunkCard project={project} id={s.id} /> : null;
  }
  if (!UUID_RE.test(s.id)) return null;
  if (s.source_type === "document") return <DocumentCard project={project} id={s.id} />;
  if (s.source_type === "entity") return <EntityCard project={project} id={s.id} />;
  if (s.source_type === "relation") return <RelationCard project={project} id={s.id} />;
  return null;
}

const MISS = (
  <span className="play__resolve play__resolve--miss">無法解析(不在目前版本或已移除)</span>
);

/** The DR-010 envelope's display name when the document carries one. */
function documentTitle(metadata: unknown): string | null {
  if (typeof metadata !== "object" || metadata === null) return null;
  const context = (metadata as Record<string, unknown>).context;
  if (typeof context !== "object" || context === null) return null;
  const title = (context as Record<string, unknown>).title;
  return typeof title === "string" && title.trim() !== "" ? title : null;
}

function DocumentCard({ project, id }: { project: string; id: string }) {
  const doc = useDocument(project, id);
  if (doc.isError) return MISS;
  if (!doc.data) return null;
  return (
    <span className="play__resolve">
      文件:{documentTitle(doc.data.metadata) ?? doc.data.source_uri}
    </span>
  );
}

function ChunkCard({ project, id }: { project: string; id: string }) {
  const chunk = useChunk(project, id);
  // chunk → its document's title: the second hop shares the per-id cache, so
  // sibling refs citing the same document resolve it once
  const doc = useDocument(project, chunk.data?.document_id);
  if (chunk.isError) return MISS;
  if (!chunk.data) return null;
  const docName = doc.data ? (documentTitle(doc.data.metadata) ?? doc.data.source_uri) : null;
  const snippet =
    chunk.data.text.length > 120 ? `${chunk.data.text.slice(0, 120)}…` : chunk.data.text;
  return (
    <span className="play__resolve">
      {docName ? `${docName} · ` : ""}段落 #{chunk.data.ordinal}:「{snippet}」
    </span>
  );
}

function EntityCard({ project, id }: { project: string; id: string }) {
  const entity = useEntity(project, id);
  if (entity.isError) return MISS;
  if (!entity.data) return null;
  return (
    <span className="play__resolve">
      {entity.data.type} · {entity.data.canonical_name}
    </span>
  );
}

function RelationCard({ project, id }: { project: string; id: string }) {
  const relation = useRelation(project, id);
  // endpoint names via the same cached entity reads; ids stand in until they
  // land (the verbatim line above keeps the full identifiers regardless)
  const src = useEntity(project, relation.data?.src_entity_id);
  const dst = useEntity(project, relation.data?.dst_entity_id);
  if (relation.isError) return MISS;
  if (!relation.data) return null;
  const name = (side: { data?: { canonical_name: string } | undefined }, id_: string) =>
    side.data?.canonical_name ?? `${id_.slice(0, 8)}…`;
  return (
    <span className="play__resolve">
      {name(src, relation.data.src_entity_id)} —{relation.data.type}→{" "}
      {name(dst, relation.data.dst_entity_id)}
    </span>
  );
}
