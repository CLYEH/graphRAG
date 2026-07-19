import { useState } from "react";

import { useDecideReviewTarget, useRelation, useRelationReviewQueue } from "../api/queries";

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

// The relation LIST endpoint deliberately OMITS evidence (detail-only, to avoid N
// sub-resource fetches per row — api/schemas.py relation_dto). So the evidenced
// quote — the reviewer's grounding — is fetched LAZILY from GET /relations/{id}
// (useRelation) only when a row's 原文證據 is expanded, never eagerly for the whole
// queue (Codex #106 P1). Its query key `["relation", project, id]` is also the one
// useDecideReviewTarget invalidates, so a decision refreshes an open detail.
function RelationEvidence({ project, relationId }: { project: string; relationId: string }) {
  const detail = useRelation(project, relationId);
  if (detail.isPending) return <p className="targets__quote">載入原文…</p>;
  if (detail.isError)
    return (
      <p className="targets__quote targets__quote--muted">原文載入失敗:{message(detail.error)}</p>
    );
  const quote = detail.data?.evidence?.find((ev) => ev.quote)?.quote ?? null;
  return (
    <p className="targets__quote">{quote ? `「${quote}」` : "(這個關聯沒有原文引文可佐證)"}</p>
  );
}

// GOV2-fe-2: the relation review queue (DESIGN §17). Relations the pipeline flagged
// `needs_review`, grounded by their type + confidence + (on demand) the evidenced
// quote. Same decision UX as the entity tab — the CORRECTED GOV2-fe-1 pattern
// (Codex #105): deterministic idem-key in the hook, 保留 (approve, non-destructive)
// inline, 排除 (reject, removes from the active graph with no in-Console undo yet)
// behind a confirm, whole-queue isPending lock (Codex #104 P2). The src/dst entity
// ids are uuids — they ride the 原始資料 fold; a src→name→dst detail drawer is a
// follow-up (GOV2-fe-4).
export function RelationReview({ project }: { project: string }) {
  const queue = useRelationReviewQueue(project);
  const decide = useDecideReviewTarget(project);
  const [confirmingReject, setConfirmingReject] = useState<string | null>(null);
  // which rows have their evidence expanded (drives the lazy detail fetch)
  const [openEvidence, setOpenEvidence] = useState<ReadonlySet<string>>(() => new Set());

  const onApprove = (id: string) =>
    decide.mutate({ kind: "relation", targetId: id, verb: "approve", reason: null });
  const onConfirmReject = (id: string) => {
    decide.mutate({ kind: "relation", targetId: id, verb: "reject", reason: null });
    setConfirmingReject(null);
  };
  const toggleEvidence = (id: string) =>
    setOpenEvidence((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  if (queue.isPending) return <p className="review__line">載入審核佇列…</p>;
  if (queue.isError)
    return <p className="review__line review__line--error">無法載入佇列:{message(queue.error)}</p>;
  if (queue.data.length === 0) return <p className="review__line">目前沒有待審的關聯。</p>;

  return (
    <ul className="targets">
      {queue.data.map((r) => (
        <li key={r.id} className="targets__row">
          <div className="targets__head">
            <span className="targets__type">{r.type}</span>
            {r.confidence !== null && r.confidence !== undefined ? (
              <span className="targets__confidence">信心 {r.confidence.toFixed(2)}</span>
            ) : null}
          </div>
          <div className="targets__evidence">
            <button
              type="button"
              className="targets__evidence-toggle"
              aria-expanded={openEvidence.has(r.id)}
              onClick={() => toggleEvidence(r.id)}
            >
              {openEvidence.has(r.id) ? "收合原文證據" : "查看原文證據"}
            </button>
            {openEvidence.has(r.id) ? (
              <RelationEvidence project={project} relationId={r.id} />
            ) : null}
          </div>
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
      ))}
      {decide.isError ? (
        <li className="review__line review__line--error">決定失敗:{message(decide.error)}</li>
      ) : null}
    </ul>
  );
}
