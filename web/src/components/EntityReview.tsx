import { useState } from "react";

import { useDecideReviewTarget, useEntityReviewQueue } from "../api/queries";

import type { ReviewTargetVerb } from "../api/queries";

// approve/reject → operator words, keyed on the contract verb enum so a new verb
// is a type error, not a silently-english label (the UXA3 translation layer).
const VERB_LABEL: Record<ReviewTargetVerb, string> = {
  approve: "保留",
  reject: "排除",
};

function message(error: unknown): string {
  return error instanceof Error ? error.message : "unknown error";
}

// GOV2-fe: the entity review queue (DESIGN §17). Entities the pipeline flagged
// `needs_review`, one row each with the canonical name + type. A decision removes
// the row and (like the sibling merge/proposal flows) is not re-decidable from the
// queue — a decided/audit view for correcting a committed decision is a follow-up
// (GOV2-fe-4). So 排除 (reject) — which removes the entity from the active graph
// with no in-Console undo yet — is guarded by an explicit confirm (Codex #105 P1);
// 保留 (approve, non-destructive: keeps the entity) stays inline for lightweight
// bulk review. The whole queue locks while any decision posts (decide.isPending) —
// a single useMutation observer, one mutation at a time (Codex #104 P2). Raw
// ids/keys live only inside the per-row 原始資料 <details> fold (the
// chrome-invariant escape hatch).
export function EntityReview({ project }: { project: string }) {
  const queue = useEntityReviewQueue(project);
  const decide = useDecideReviewTarget(project);
  // the entity id awaiting a reject confirm (one at a time)
  const [confirmingReject, setConfirmingReject] = useState<string | null>(null);

  // lock every decision control while a decision posts AND while the queue refreshes
  // after one: a resolved POST clears decide.isPending before the invalidated GET
  // drops the decided row, so a second decision in that window would re-decide it
  // and latest-manual-wins would silently reverse the one just confirmed (Codex #106
  // P1d — the ReviewCases queueRefreshing guard). 取消 stays usable to back out.
  const locked = decide.isPending || queue.isFetching;

  const onApprove = (id: string) =>
    decide.mutate({ kind: "entity", targetId: id, verb: "approve", reason: null });
  const onConfirmReject = (id: string) => {
    decide.mutate({ kind: "entity", targetId: id, verb: "reject", reason: null });
    setConfirmingReject(null);
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
          {confirmingReject === e.id ? (
            <div className="targets__confirm" role="alertdialog" aria-label="確認排除">
              <p>排除後這個知識點會從上線的知識庫移除,目前無法從介面復原。確定嗎?</p>
              <button
                type="button"
                className="targets__reject"
                disabled={locked}
                onClick={() => onConfirmReject(e.id)}
              >
                確定{VERB_LABEL.reject}
              </button>
              <button
                type="button"
                disabled={decide.isPending}
                onClick={() => setConfirmingReject(null)}
              >
                取消
              </button>
            </div>
          ) : (
            <div className="targets__actions">
              <button
                type="button"
                className="targets__approve"
                disabled={locked}
                onClick={() => onApprove(e.id)}
              >
                {VERB_LABEL.approve}
              </button>
              <button
                type="button"
                className="targets__reject"
                disabled={locked}
                onClick={() => setConfirmingReject(e.id)}
              >
                {VERB_LABEL.reject}
              </button>
            </div>
          )}
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
