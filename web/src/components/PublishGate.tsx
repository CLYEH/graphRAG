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

// GOV2-fe-3: a DISPLAY-ONLY publish-readiness advisory beside the activate control.
// It surfaces the non-zero §19 quality signals a curator may want to address before
// publishing, and deep-links the actionable ones — but it NEVER blocks activation:
// the §14 preflight decides that server-side (it blocks only on missing eval scores
// / drift, not on these backlogs), so gating the activate button here would be a
// fake gate. Health exposes no extraction-failure-rate count, so the panel shows
// only the signals Health actually reports.
export function PublishGate({ counts }: { counts: Record<string, number | undefined> }) {
  const active = SIGNALS.map((s) => ({ ...s, count: Number(counts[s.key] ?? 0) })).filter(
    (s) => s.count > 0,
  );
  if (active.length === 0) return null;

  return (
    <section className="publishgate">
      <p className="publishgate__title">發布前品質檢查(僅供參考,不影響上線)</p>
      <ul className="publishgate__list">
        {active.map((s) => (
          <li key={s.key} className="publishgate__row">
            <span className="publishgate__label">
              ⚠ {s.label}:{s.count}
            </span>
            {s.tab ? (
              <Link to={`../review?tab=${s.tab}`}>前往處理</Link>
            ) : (
              <span className="publishgate__note">(清單待後端 facet)</span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
