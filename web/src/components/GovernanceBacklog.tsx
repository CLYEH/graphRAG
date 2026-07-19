import { Link } from "react-router-dom";

// The §19 quality signals this panel surfaces. The three review backlogs deep-link
// to their governance tab; the two relation-quality counts have NO list endpoint
// yet (the deferred /relations confidence/evidence facet task), so they show as
// information only — honest, not a link to a page that can't render them.
// pending_merge_candidates is deliberately absent: it has its own prominent action
// card on the Overview already (Codex #78).
const SIGNALS: { key: string; label: string; tab?: string }[] = [
  { key: "needs_review_entities", label: "待審知識點", tab: "entity" },
  { key: "needs_review_relations", label: "待審關聯", tab: "relation" },
  { key: "pending_ontology_proposals", label: "待審本體提案", tab: "proposals" },
  { key: "low_confidence_relations", label: "低信心關聯" },
  { key: "missing_evidence_relations", label: "缺證據關聯" },
];

// GOV2-fe-3: a DISPLAY-ONLY governance-backlog summary on the Overview. It lists
// the non-zero §19 quality signals and deep-links the actionable ones; it NEVER
// blocks activation (the §14 preflight decides that server-side).
//
// Honest scope (Codex #107 P2): these counts follow §19 Health semantics — the
// entity/relation review + confidence/evidence counts describe the CURRENTLY
// ACTIVE build (zeros when none exists); only the proposal pool is project-wide.
// So this is the LIVE knowledge base's governance backlog, NOT a publish-readiness
// check of a candidate build — Health has no per-build facet, and a candidate-
// scoped preflight would need a contract addition (deferred follow-up). Working
// the backlog still pays forward: §17 decisions ride the DR-011 ledger into
// future builds.
export function GovernanceBacklog({ counts }: { counts: Record<string, number | undefined> }) {
  const active = SIGNALS.map((s) => ({ ...s, count: Number(counts[s.key] ?? 0) })).filter(
    (s) => s.count > 0,
  );
  if (active.length === 0) return null;

  return (
    <section className="govbacklog">
      <p className="govbacklog__title">品質治理待辦(上線中知識庫;僅供參考,不影響上線)</p>
      <ul className="govbacklog__list">
        {active.map((s) => (
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
    </section>
  );
}
