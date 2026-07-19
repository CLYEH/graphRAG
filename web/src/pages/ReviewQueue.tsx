import { useSearchParams } from "react-router-dom";

import { ProposalPool } from "../components/ProposalPool";
import { ReviewCases } from "../components/ReviewCases";
import { isPathAddressable, useActiveProject } from "../project/projectRoute";
import "./ReviewQueue.css";

// The governance surface (DESIGN §17 names four review kinds). GOV3-fe adds the
// ontology-proposal pool beside the existing merge-candidate queue as tabs; GOV2-fe
// will add the entity/relation review + quality lists here too. The route stays
// `review` (deep-links + e2e depend on it); the tab lives in `?tab=` so Health
// signals can deep-link a specific tab.
const TABS = [
  { key: "merge", label: "合併" },
  { key: "proposals", label: "本體提案" },
] as const;
type TabKey = (typeof TABS)[number]["key"];

export function ReviewQueue() {
  const project = useActiveProject();
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  const tab: TabKey = TABS.some((t) => t.key === raw) ? (raw as TabKey) : "merge";

  if (project === undefined) return <p className="review__line">Unknown project.</p>;
  if (!isPathAddressable(project))
    return (
      <p className="review__line review__line--error">
        Project &quot;{project}&quot; isn&apos;t addressable over the API — its key contains
        &quot;/&quot; or is &quot;.&quot; / &quot;..&quot;, which a URL path segment can&apos;t
        carry.
      </p>
    );

  return (
    <section className="review">
      <h1 className="review__title">治理</h1>
      <nav className="review__tabs" role="tablist" aria-label="治理面向">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            className={`review__tab${tab === t.key ? " review__tab--on" : ""}`}
            onClick={() =>
              setParams(
                (prev) => {
                  const next = new URLSearchParams(prev);
                  next.set("tab", t.key);
                  return next;
                },
                { replace: true },
              )
            }
          >
            {t.label}
          </button>
        ))}
      </nav>

      {tab === "merge" ? (
        <>
          <p className="review__intro">
            建置時系統發現這些名字<strong>可能指同一個東西</strong>,但不敢自行決定。
            請逐案確認:同一個就合併,不同的就分開;拿不準先跳過。
          </p>
          <ReviewCases project={project} />
        </>
      ) : (
        <>
          <p className="review__intro">
            LLM 於抽取時觀察到、但不在<strong>已設定本體</strong>裡的型別。
            採納即加入本體(下次建置生效),拒絕則排除。
          </p>
          <ProposalPool project={project} />
        </>
      )}
    </section>
  );
}
