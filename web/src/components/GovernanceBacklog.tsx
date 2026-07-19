import { Link } from "react-router-dom";

// The §19 quality signals this panel surfaces, grouped by their TRUE scope
// (Codex #107 R2): the entity/relation review + confidence/evidence counts are
// scoped to the ACTIVE build (health.py), but the ontology-proposal pool is
// project-wide — so a project with proposals and NO active build must not be told
// the tasks belong to a live knowledge base. Each scope group renders only when it
// has a non-zero signal, under its own honest label.
// The three review backlogs deep-link to their governance tab; the two
// relation-quality counts have NO list endpoint yet (the deferred /relations
// confidence/evidence facet task), so they show as information only.
// pending_merge_candidates is deliberately absent: it has its own prominent action
// card on the Overview already (Codex #78).
type Scope = "build" | "project";

const SCOPE_LABEL: Record<Scope, string> = {
  build: "上線中知識庫",
  project: "全專案",
};

const SIGNALS: { key: string; label: string; scope: Scope; tab?: string }[] = [
  { key: "needs_review_entities", label: "待審知識點", scope: "build", tab: "entity" },
  { key: "needs_review_relations", label: "待審關聯", scope: "build", tab: "relation" },
  { key: "low_confidence_relations", label: "低信心關聯", scope: "build" },
  { key: "missing_evidence_relations", label: "缺證據關聯", scope: "build" },
  { key: "pending_ontology_proposals", label: "待審本體提案", scope: "project", tab: "proposals" },
];

// GOV2-fe-3: a DISPLAY-ONLY governance-backlog summary on the Overview. It lists
// the non-zero §19 quality signals and deep-links the actionable ones; it NEVER
// blocks activation (the §14 preflight decides that server-side). This is NOT a
// publish-readiness check of a candidate build — Health has no per-build facet,
// so a candidate-scoped preflight would need a contract addition (deferred).
// Working the backlog still pays forward: §17 decisions ride the DR-011 ledger
// into future builds.
export function GovernanceBacklog({ counts }: { counts: Record<string, number | undefined> }) {
  const active = SIGNALS.map((s) => ({ ...s, count: Number(counts[s.key] ?? 0) })).filter(
    (s) => s.count > 0,
  );
  if (active.length === 0) return null;

  const groups = (["build", "project"] as const)
    .map((scope) => ({ scope, rows: active.filter((s) => s.scope === scope) }))
    .filter((g) => g.rows.length > 0);

  return (
    <section className="govbacklog">
      <p className="govbacklog__title">品質治理待辦(僅供參考,不影響上線)</p>
      {groups.map((g) => (
        <div key={g.scope}>
          <p className="govbacklog__scope">{SCOPE_LABEL[g.scope]}</p>
          <ul className="govbacklog__list">
            {g.rows.map((s) => (
              <li key={s.key} className="govbacklog__row">
                <span className="govbacklog__label">
                  ⚠ {s.label}:{s.count}
                </span>
                {s.tab ? (
                  <Link to={`../review?tab=${s.tab}`}>前往處理</Link>
                ) : (
                  <span className="govbacklog__note">(清單待後端 facet)</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </section>
  );
}
