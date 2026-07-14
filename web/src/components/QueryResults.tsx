import type { QueryResult } from "../api/queries";

type WarningCode = QueryResult["warnings"][number]["code"];
type RetrievalResult = QueryResult["results"][number];

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

export function QueryResults({ result }: { result: QueryResult }) {
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
          <Hit key={`${r.result_type}:${r.id}`} hit={r} />
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

function Hit({ hit }: { hit: RetrievalResult }) {
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
        <details className="play__sources">
          <summary>{hit.source_refs.length} 個來源引用</summary>
          <ul>
            {hit.source_refs.map((s, i) => (
              <li key={i}>
                <span className="runs__badge runs__badge--muted">{s.source_type}</span>{" "}
                <code>{s.id}</code>
                {/* source_uri is rendered as text, never an href — an untrusted
                    value in an <a href> would be a fresh injection sink (FE7) */}
                {s.source_uri ? <span className="play__uri"> · {s.source_uri}</span> : null}
              </li>
            ))}
          </ul>
        </details>
      )}
    </li>
  );
}
