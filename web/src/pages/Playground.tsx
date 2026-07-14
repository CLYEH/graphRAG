import { useState } from "react";

import { useRunQuery } from "../api/queries";
import { isPathAddressable, useActiveProject } from "../project/projectRoute";
import { QueryResults } from "../components/QueryResults";
import "./Playground.css";

import type { GraphOptions, QueryMode } from "../api/queries";

const MODES: QueryMode[] = ["hybrid", "semantic", "sql", "global", "graph"];
const TEMPLATES: GraphOptions["template"][] = ["neighbors", "path", "subgraph"];

// FE6 Query playground (DESIGN §13/§21/§22): run any of the five modes against the
// active build and show results + citations + warnings + routing trace. Graph viz
// is v2 (FE4), so graph_context renders as a summary line.
export function Playground() {
  const project = useActiveProject();

  if (project === undefined) return <p className="play__line">Unknown project.</p>;
  if (!isPathAddressable(project))
    return (
      <p className="play__line play__line--error">
        Project &quot;{project}&quot; isn&apos;t addressable over the API — its key contains
        &quot;/&quot; or is &quot;.&quot; / &quot;..&quot;, which a URL path segment can&apos;t
        carry.
      </p>
    );

  return (
    <section className="play">
      <h1 className="play__title">問答測試</h1>
      <QueryConsole project={project} />
    </section>
  );
}

function QueryConsole({ project }: { project: string }) {
  const [mode, setMode] = useState<QueryMode>("hybrid");
  const [query, setQuery] = useState("");
  const [topKText, setTopKText] = useState("");
  const [template, setTemplate] = useState<GraphOptions["template"]>("neighbors");
  const [entity, setEntity] = useState("");
  const [otherEntity, setOtherEntity] = useState("");
  const [hopsText, setHopsText] = useState("1");
  const [includeGraph, setIncludeGraph] = useState(false);

  const run = useRunQuery(project);

  // graph accepts no top_k; hybrid can carry the graph options behind a toggle,
  // graph always does. Each mode's allowed fields are enforced in queryBody.
  const showTopK = mode !== "graph";
  const showGraph = mode === "graph" || (mode === "hybrid" && includeGraph);

  const graphIncomplete =
    showGraph && (entity.trim() === "" || (template === "path" && otherEntity.trim() === ""));
  const canRun = query.trim() !== "" && !graphIncomplete && !run.isPending;

  function submit() {
    const n = Number(topKText);
    const topK = showTopK && topKText.trim() !== "" && Number.isInteger(n) && n >= 1 ? n : null;
    const options: GraphOptions | null = showGraph
      ? {
          template,
          entity: entity.trim(),
          ...(template === "path" ? { other_entity: otherEntity.trim() } : {}),
          // guard emptiness first, like top_k — a blank field is Number("")===0,
          // which would send hops: 0 (a GUARDRAIL_BLOCKED) instead of the default
          hops: hopsText.trim() !== "" && Number.isInteger(Number(hopsText)) ? Number(hopsText) : 1,
        }
      : null;
    run.mutate({ mode, query: query.trim(), topK, options });
  }

  return (
    <div className="play__console">
      <form
        className="play__form"
        onSubmit={(e) => {
          e.preventDefault();
          if (canRun) submit();
        }}
      >
        <label className="play__field">
          查詢模式
          <select value={mode} onChange={(e) => setMode(e.target.value as QueryMode)}>
            {MODES.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>

        <label className="play__field play__field--wide">
          問題
          <textarea
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="輸入你的問題"
            rows={2}
          />
        </label>

        {showTopK && (
          <label className="play__field">
            結果數上限
            <input
              type="number"
              min={1}
              step={1}
              value={topKText}
              onChange={(e) => setTopKText(e.target.value)}
              placeholder="政策預設"
            />
          </label>
        )}

        {mode === "hybrid" && (
          <label className="play__field play__field--check">
            <input
              type="checkbox"
              checked={includeGraph}
              onChange={(e) => setIncludeGraph(e.target.checked)}
            />
            加入圖譜模式
          </label>
        )}

        {showGraph && (
          <fieldset className="play__graph-opts">
            <legend>圖譜選項</legend>
            <label className="play__field">
              查法(template)
              <select
                value={template}
                onChange={(e) => setTemplate(e.target.value as GraphOptions["template"])}
              >
                {TEMPLATES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
            <label className="play__field">
              起點實體(名稱)
              <input value={entity} onChange={(e) => setEntity(e.target.value)} />
            </label>
            {template === "path" && (
              <label className="play__field">
                other entity
                <input value={otherEntity} onChange={(e) => setOtherEntity(e.target.value)} />
              </label>
            )}
            <label className="play__field">
              hops
              <input
                type="number"
                min={1}
                step={1}
                value={hopsText}
                onChange={(e) => setHopsText(e.target.value)}
              />
            </label>
          </fieldset>
        )}

        <button type="submit" disabled={!canRun}>
          {run.isPending ? "Running…" : "Run query"}
        </button>
      </form>

      {run.isError && (
        <p className="runs__muted runs__muted--error">
          Query failed: {run.error instanceof Error ? run.error.message : "unknown error"}
        </p>
      )}
      {run.data && <QueryResults result={run.data} />}
    </div>
  );
}
