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
        <span className="play__meta">
          build: {degraded ? "— (degraded)" : result.build_id.slice(0, 8)}
        </span>
        <span className="play__meta">
          {result.results.length} result{result.results.length === 1 ? "" : "s"}
        </span>
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
        <p className="runs__muted">No results.</p>
      )}

      <ol className="play__hits">
        {result.results.map((r) => (
          <Hit key={`${r.result_type}:${r.id}`} hit={r} />
        ))}
      </ol>

      {result.graph_context && (
        <p className="play__graph">
          graph context: {result.graph_context.nodes.length} nodes,{" "}
          {result.graph_context.edges.length} edges
          {result.graph_context.paths ? `, ${result.graph_context.paths.length} paths` : ""}
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
        <span className="play__meta">score {hit.score.toFixed(3)}</span>
        {hit.confidence != null && (
          <span className="play__meta">conf {hit.confidence.toFixed(2)}</span>
        )}
      </div>
      {hit.text && <p className="play__text">{hit.text}</p>}
      <ul className="play__sources">
        {hit.source_refs.map((s, i) => (
          <li key={i}>
            <span className="runs__badge runs__badge--muted">{s.source_type}</span>{" "}
            {/* the full id, not a slice — row refs are a lossless table:pk string
                (core row_source_ref), so truncating hides the pk and makes two row
                citations indistinguishable, breaking §16 traceability */}
            <code>{s.id}</code>
            {/* source_uri is rendered as text, never an href — an untrusted value
                in an <a href> would be a fresh injection sink (the FE7 lesson) */}
            {s.source_uri ? <span className="play__uri"> · {s.source_uri}</span> : null}
          </li>
        ))}
      </ul>
    </li>
  );
}
