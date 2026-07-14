import { Link } from "react-router-dom";

import { useHealth } from "../api/queries";
import { isPathAddressable, useActiveProject } from "../project/projectRoute";
import "./ProjectHealth.css";

import type { HealthReport } from "../api/queries";

// The five §19 status lights. Keying the map on HealthReport["status"] makes a
// new contract enum value a type error here rather than a silent grey badge.
const STATUS: Record<HealthReport["status"], { label: string; tone: string }> = {
  healthy: { label: "健康", tone: "ok" },
  needs_review: { label: "有待審項目", tone: "warn" },
  build_failed: { label: "建置失敗", tone: "bad" },
  index_drift: { label: "索引漂移", tone: "warn" },
  eval_regression: { label: "評測退步", tone: "bad" },
};

// §19 count keys → operator words (UXA3 translation layer). Unknown keys fall
// back to the raw name — honesty over hiding; the fallback keeps a NEW server
// count visible instead of silently dropped.
const COUNT_LABELS: Record<string, string> = {
  sources: "資料來源",
  builds_total: "建置次數",
  documents: "文件",
  chunks: "段落",
  entities: "知識點",
  relations: "關聯",
  pending_merge_candidates: "待審合併",
  pending_ontology_proposals: "待審本體提案",
  needs_review_entities: "待審知識點",
  needs_review_relations: "待審關聯",
  low_confidence_relations: "低信心關聯",
  missing_evidence_relations: "缺證據關聯",
};

export function ProjectHealth() {
  const project = useActiveProject();
  const { data, isPending, isError, error } = useHealth(project);

  // A route segment that doesn't decode is an unknown project, not a spinner.
  if (project === undefined) return <Status text="Unknown project." />;
  // Some keys open in the route but can't be a REST path segment ("/"-bearing,
  // or "." / ".."); say so rather than fire a request that 404s or normalizes to
  // the wrong endpoint.
  if (!isPathAddressable(project))
    return (
      <Status
        text={`Project "${project}" isn't addressable over the API — its key contains "/" or is "." / "..", which a URL path segment can't carry.`}
        error
      />
    );
  if (isPending) return <Status text="載入中…" />;
  if (isError) {
    const message = error instanceof Error ? error.message : "unknown error";
    return <Status text={`無法載入專案健康狀態:${message}`} error />;
  }

  return <HealthView report={data} />;
}

function Status({ text, error = false }: { text: string; error?: boolean }) {
  return (
    <section className="health">
      <p className={error ? "health__line health__line--error" : "health__line"}>{text}</p>
    </section>
  );
}

function HealthView({ report }: { report: HealthReport }) {
  const light = STATUS[report.status];
  const counts = Object.entries(report.counts ?? {});
  const drift = Object.entries(report.drift ?? {});
  const warnings = report.warnings ?? [];

  const pending = report.pending_review ?? 0;
  // pending_review AGGREGATES four §19 review types, but /review renders only
  // the merge-candidate flow — deep-linking an ontology/entity/relation
  // backlog there would land on an empty page (Codex #78). The link follows
  // the one actionable count; the grid below breaks down the rest.
  const mergePending = Number((report.counts ?? {}).pending_merge_candidates ?? 0);

  return (
    <section className="health">
      <h1 className="health__title">專案健康(診斷)</h1>
      <div className={`health__badge health__badge--${light.tone}`} role="status">
        {light.label}
      </div>

      <dl className="health__facts">
        <div>
          <dt>上線中的版本</dt>
          {/* words on the surface, uuid on hover (UXA3 translation layer) */}
          <dd title={report.active_build_id ?? undefined}>
            {report.active_build_id ? "有(游標懸停看識別碼)" : "無"}
          </dd>
        </div>
        <div>
          <dt>待審核</dt>
          <dd>
            {pending}
            {mergePending > 0 && (
              <>
                {" "}
                <Link to="../review">前往審核</Link>
              </>
            )}
          </dd>
        </div>
      </dl>

      <h2>數量統計</h2>
      {counts.length > 0 ? (
        <dl className="health__grid">
          {counts.map(([key, value]) => (
            <div key={key}>
              <dt>{COUNT_LABELS[key] ?? key}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="health__muted">還沒有統計數字(建置並上線後出現)。</p>
      )}

      <h2>投影一致性</h2>
      {drift.length > 0 ? (
        <dl className="health__grid health__grid--drift">
          {drift.map(([store, detail]) => (
            <div key={store}>
              <dt>{store}</dt>
              <dd>{JSON.stringify(detail)}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="health__muted">各儲存層與主資料一致,沒有漂移。</p>
      )}

      {warnings.length > 0 && (
        <>
          <h2>警告</h2>
          <ul className="health__warnings">
            {warnings.map((w, i) => (
              <li key={`${w.code}-${i}`}>
                <code>{w.code}</code> {w.message}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
