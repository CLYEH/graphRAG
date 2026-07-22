import { useDecideReviewTarget, useRelationGapList } from "../api/queries";
import { RelationRow } from "./RelationReview";

import type { RelationGapFacet } from "../api/queries";

function message(error: unknown): string {
  return error instanceof Error ? error.message : "unknown error";
}

// GOV2-fe-5: the two quality gap lists (低信心 / 缺證據) — ACTIVE relations a
// facet flags for triage, deep-linked from Health's non-zero gauges. Rows are
// the SAME RelationRow as the review queue (see-the-pair gate, lazy evidence,
// shared lock/restore hooks) in queue mode: 排除 removes the relation from
// the live graph; 保留 records a curator confirmation in the ledger but the
// row LEGITIMATELY stays listed — the facet keys on confidence/evidence,
// which a confirmation does not change (the tab intro says so; pretending
// otherwise would be a false affordance).
export function RelationGapList({ project, facet }: { project: string; facet: RelationGapFacet }) {
  const list = useRelationGapList(project, facet);
  const decide = useDecideReviewTarget(project);
  const rows = list.data?.pages.flatMap((p) => p.rows) ?? [];

  return (
    <div>
      {list.isPending ? (
        <p className="review__line">載入中…</p>
      ) : list.isError && !list.data ? (
        <p className="review__line review__line--error">無法載入清單:{message(list.error)}</p>
      ) : rows.length === 0 ? (
        <p className="review__line">
          {facet === "confidence" ? "目前沒有低信心的關聯。" : "目前沒有缺證據的關聯。"}
        </p>
      ) : (
        <ul className="targets">
          {rows.map((r) => (
            <RelationRow
              key={r.id}
              project={project}
              r={r}
              decide={decide}
              // the query itself, not a derived boolean — the lock's list axis
              // (incl. #108 P1's isError-never-unlocks) lives in the shared hook
              list={list}
              mode="queue"
            />
          ))}
        </ul>
      )}

      {/* any list failure with rows on screen: rows stay (#102), retry via FULL
          refetch — recomputes params from fresh page 1, recovering transient
          failures AND the build-swap pin trip (Codex #108 P2) */}
      {list.isError && list.data ? (
        <p className="review__line review__line--error">
          載入失敗:{message(list.error)}
          <button
            type="button"
            className="targets__evidence-toggle"
            onClick={() => void list.refetch()}
          >
            重新載入
          </button>
        </p>
      ) : null}
      {list.hasNextPage && !list.isError ? (
        <button
          type="button"
          className="targets__more"
          disabled={list.isFetchingNextPage}
          onClick={() => void list.fetchNextPage()}
        >
          {list.isFetchingNextPage ? "載入中…" : "載入更多"}
        </button>
      ) : null}
      {decide.isError ? (
        <p className="review__line review__line--error">決定失敗:{message(decide.error)}</p>
      ) : null}
    </div>
  );
}
