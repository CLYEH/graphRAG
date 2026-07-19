import { useState } from "react";

import { useDecideReviewTarget, useRelationReviewQueue } from "../api/queries";

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

// GOV2-fe-2: the relation review queue (DESIGN §17). Relations the pipeline flagged
// `needs_review`, grounded by their type + confidence + evidenced quote (the src/dst
// entity ids are uuids — they ride the 原始資料 fold; a src→name→dst detail drawer
// is a follow-up, GOV2-fe-4). Same decision UX as the entity tab — the CORRECTED
// GOV2-fe-1 pattern (Codex #105): deterministic idem-key in the hook, 保留 (approve,
// non-destructive) inline, 排除 (reject, removes from the active graph with no
// in-Console undo yet) behind a confirm, whole-queue isPending lock (Codex #104 P2).
export function RelationReview({ project }: { project: string }) {
  const queue = useRelationReviewQueue(project);
  const decide = useDecideReviewTarget(project);
  const [confirmingReject, setConfirmingReject] = useState<string | null>(null);

  const onApprove = (id: string) =>
    decide.mutate({ kind: "relation", targetId: id, verb: "approve", reason: null });
  const onConfirmReject = (id: string) => {
    decide.mutate({ kind: "relation", targetId: id, verb: "reject", reason: null });
    setConfirmingReject(null);
  };

  if (queue.isPending) return <p className="review__line">載入審核佇列…</p>;
  if (queue.isError)
    return <p className="review__line review__line--error">無法載入佇列:{message(queue.error)}</p>;
  if (queue.data.length === 0) return <p className="review__line">目前沒有待審的關聯。</p>;

  return (
    <ul className="targets">
      {queue.data.map((r) => {
        const quote = r.evidence?.find((ev) => ev.quote)?.quote ?? null;
        return (
          <li key={r.id} className="targets__row">
            <div className="targets__head">
              <span className="targets__type">{r.type}</span>
              {r.confidence !== null && r.confidence !== undefined ? (
                <span className="targets__confidence">信心 {r.confidence.toFixed(2)}</span>
              ) : null}
            </div>
            <p className="targets__evidence">{quote ? `「${quote}」` : "(此關聯沒有原文引文)"}</p>
            {confirmingReject === r.id ? (
              <div className="targets__confirm" role="alertdialog" aria-label="確認排除">
                <p>排除後這個關聯會從上線的知識庫移除,目前無法從介面復原。確定嗎?</p>
                <button
                  type="button"
                  className="targets__reject"
                  disabled={decide.isPending}
                  onClick={() => onConfirmReject(r.id)}
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
                  disabled={decide.isPending}
                  onClick={() => onApprove(r.id)}
                >
                  {VERB_LABEL.approve}
                </button>
                <button
                  type="button"
                  className="targets__reject"
                  disabled={decide.isPending}
                  onClick={() => setConfirmingReject(r.id)}
                >
                  {VERB_LABEL.reject}
                </button>
              </div>
            )}
            {/* raw ids / signatures / store-vocabulary = the chrome-invariant
                <details> escape hatch (the per-decision audit trail, GOV2 §17) */}
            <details className="targets__audit">
              <summary>原始資料</summary>
              <dl>
                <div>
                  <dt>id</dt>
                  <dd>{r.id}</dd>
                </div>
                <div>
                  <dt>src_entity_id</dt>
                  <dd>{r.src_entity_id}</dd>
                </div>
                <div>
                  <dt>dst_entity_id</dt>
                  <dd>{r.dst_entity_id}</dd>
                </div>
                <div>
                  <dt>relation_signature</dt>
                  <dd>{r.relation_signature ?? "—"}</dd>
                </div>
                <div>
                  <dt>status</dt>
                  <dd>{r.status}</dd>
                </div>
                <div>
                  <dt>review_status</dt>
                  <dd>{r.review_status ?? "—"}</dd>
                </div>
                <div>
                  <dt>confidence</dt>
                  <dd>{r.confidence ?? "—"}</dd>
                </div>
                <div>
                  <dt>attributes</dt>
                  <dd>{JSON.stringify(r.attributes ?? {})}</dd>
                </div>
              </dl>
            </details>
          </li>
        );
      })}
      {decide.isError ? (
        <li className="review__line review__line--error">決定失敗:{message(decide.error)}</li>
      ) : null}
    </ul>
  );
}
