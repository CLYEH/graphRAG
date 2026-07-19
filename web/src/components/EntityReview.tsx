import { useDecideReviewTarget, useEntityReviewQueue } from "../api/queries";

import type { ReviewTargetVerb } from "../api/queries";

// approve/reject → operator words, keyed on the contract verb enum so a new verb
// is a type error, not a silently-english label (the UXA3 translation layer).
// These §17 decisions are REVERSIBLE (review.py appends + latest-manual-wins
// resolves), so there is deliberately NO confirm step — a misclick is recoverable
// by re-deciding — unlike the merge/proposal terminal flows (GOV3-fe #104).
const VERB_LABEL: Record<ReviewTargetVerb, string> = {
  approve: "保留",
  reject: "排除",
};

function message(error: unknown): string {
  return error instanceof Error ? error.message : "unknown error";
}

// GOV2-fe: the entity review queue (DESIGN §17). Entities the pipeline flagged
// `needs_review`, one row each with the canonical name + type and inline
// keep/exclude. The whole queue locks while any decision posts (decide.isPending)
// — a single useMutation observer, so exactly one mutation in flight keeps
// react-query's lifecycle reliable (Codex #104 P2). Raw ids/keys/store-vocabulary
// live only inside the per-row 原始資料 <details> fold (the chrome-invariant
// escape hatch), never as bare chrome.
export function EntityReview({ project }: { project: string }) {
  const queue = useEntityReviewQueue(project);
  const decide = useDecideReviewTarget(project);

  const onDecide = (targetId: string, verb: ReviewTargetVerb) => {
    // per-attempt RANDOM idem-key: a reversible re-decision must NOT replay an
    // earlier decision's stored response (the activate/trigger discipline).
    decide.mutate({
      kind: "entity",
      targetId,
      verb,
      reason: null,
      idempotencyKey: crypto.randomUUID(),
    });
  };

  if (queue.isPending) return <p className="review__line">載入審核佇列…</p>;
  if (queue.isError)
    return <p className="review__line review__line--error">無法載入佇列:{message(queue.error)}</p>;
  if (queue.data.length === 0) return <p className="review__line">目前沒有待審的知識點。</p>;

  return (
    <ul className="targets">
      {queue.data.map((e) => (
        <li key={e.id} className="targets__row">
          <div className="targets__head">
            <span className="targets__name">{e.canonical_name}</span>
            <span className="targets__type">{e.type}</span>
          </div>
          <div className="targets__actions">
            <button
              type="button"
              className="targets__approve"
              disabled={decide.isPending}
              onClick={() => onDecide(e.id, "approve")}
            >
              {VERB_LABEL.approve}
            </button>
            <button
              type="button"
              className="targets__reject"
              disabled={decide.isPending}
              onClick={() => onDecide(e.id, "reject")}
            >
              {VERB_LABEL.reject}
            </button>
          </div>
          {/* raw ids / entity_key / store-vocabulary = the chrome-invariant
              <details> escape hatch (the per-decision audit trail, GOV2 §17) */}
          <details className="targets__audit">
            <summary>原始資料</summary>
            <dl>
              <div>
                <dt>id</dt>
                <dd>{e.id}</dd>
              </div>
              <div>
                <dt>entity_key</dt>
                <dd>{e.entity_key}</dd>
              </div>
              <div>
                <dt>status</dt>
                <dd>{e.status}</dd>
              </div>
              <div>
                <dt>review_status</dt>
                <dd>{e.review_status ?? "—"}</dd>
              </div>
              <div>
                <dt>created_by</dt>
                <dd>{e.created_by ?? "—"}</dd>
              </div>
              <div>
                <dt>attributes</dt>
                <dd>{JSON.stringify(e.attributes ?? {})}</dd>
              </div>
            </dl>
          </details>
        </li>
      ))}
      {decide.isError ? (
        <li className="review__line review__line--error">決定失敗:{message(decide.error)}</li>
      ) : null}
    </ul>
  );
}
