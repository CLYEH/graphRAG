import { useSearchParams } from "react-router-dom";

import { EntityReview } from "../components/EntityReview";
import { ProposalPool } from "../components/ProposalPool";
import { RelationReview } from "../components/RelationReview";
import { ReviewCases } from "../components/ReviewCases";
import { isPathAddressable, useActiveProject } from "../project/projectRoute";
import "./ReviewQueue.css";

import type { KeyboardEvent } from "react";

// The governance surface (DESIGN §17 names four review kinds). Tabs: merge
// candidates (UXA1), entity review (GOV2-fe-1), relation review (GOV2-fe-2),
// ontology proposals (GOV3-fe). The route stays `review` (deep-links + e2e depend
// on it); the active tab lives in `?tab=` so Health signals can deep-link a tab.
const TABS = [
  { key: "merge", label: "合併" },
  { key: "entity", label: "知識點" },
  { key: "relation", label: "關聯" },
  { key: "proposals", label: "本體提案" },
] as const;
type TabKey = (typeof TABS)[number]["key"];

const tabId = (k: TabKey) => `review-tab-${k}`;
// one panel whose content swaps — a stable id so every tab's aria-controls
// resolves to a present element (no dangling refs), the panel's aria-labelledby
// tracks the active tab. (Rendering all three panels would fire all three queues.)
const PANEL_ID = "review-panel";

export function ReviewQueue() {
  const project = useActiveProject();
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  const tab: TabKey = TABS.some((t) => t.key === raw) ? (raw as TabKey) : "merge";

  const selectTab = (key: TabKey) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set("tab", key);
        return next;
      },
      { replace: true },
    );

  // roving arrow-key nav across the tablist (WAI-ARIA tabs pattern): ←/→ wrap,
  // Home/End jump to the ends; the moved-to tab is both selected and focused.
  const onTabKeyDown = (e: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const last = TABS.length - 1;
    let target: number | null = null;
    if (e.key === "ArrowRight") target = index === last ? 0 : index + 1;
    else if (e.key === "ArrowLeft") target = index === 0 ? last : index - 1;
    else if (e.key === "Home") target = 0;
    else if (e.key === "End") target = last;
    if (target === null) return;
    e.preventDefault();
    const key = TABS[target].key;
    selectTab(key);
    document.getElementById(tabId(key))?.focus();
  };

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
        {TABS.map((t, i) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            id={tabId(t.key)}
            aria-selected={tab === t.key}
            aria-controls={PANEL_ID}
            tabIndex={tab === t.key ? 0 : -1}
            className={`review__tab${tab === t.key ? " review__tab--on" : ""}`}
            onClick={() => selectTab(t.key)}
            onKeyDown={(e) => onTabKeyDown(e, i)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      <div role="tabpanel" id={PANEL_ID} aria-labelledby={tabId(tab)} tabIndex={0}>
        {tab === "merge" && (
          <>
            <p className="review__intro">
              建置時系統發現這些名字<strong>可能指同一個東西</strong>,但不敢自行決定。
              請逐案確認:同一個就合併,不同的就分開;拿不準先跳過。
            </p>
            <ReviewCases project={project} />
          </>
        )}
        {tab === "entity" && (
          <>
            <p className="review__intro">
              系統不確定這些<strong>知識點</strong>是否正確,先擱著等你確認。
              對的就保留;錯的就排除——排除會把它從上線的知識庫移除,目前無法從介面復原,請確認後再排除。
            </p>
            <EntityReview project={project} />
          </>
        )}
        {tab === "relation" && (
          <>
            <p className="review__intro">
              系統不確定這些<strong>關聯</strong>是否正確,可展開原文引文輔助判斷。
              對的就保留;錯的就排除——排除會把它從上線的知識庫移除,目前無法從介面復原,請確認後再排除。
            </p>
            <RelationReview project={project} />
          </>
        )}
        {tab === "proposals" && (
          <>
            <p className="review__intro">
              LLM 於抽取時觀察到、但不在<strong>已設定本體</strong>裡的型別。
              採納即加入本體(下次建置生效),拒絕則排除。
            </p>
            <ProposalPool project={project} />
          </>
        )}
      </div>
    </section>
  );
}
