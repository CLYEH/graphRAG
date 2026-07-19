import { useState } from "react";

import {
  useDecideReviewTarget,
  useEntity,
  useRelation,
  useRelationReviewQueue,
} from "../api/queries";

import type { Relation, ReviewTargetVerb } from "../api/queries";

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
// quote is fetched LAZILY from GET /relations/{id} (useRelation) only when a row's
// 原文證據 is expanded (Codex #106 P1a).
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

// Resolves an endpoint entity id to its canonical name. The list row carries only
// uuids, so a decision would be blind to WHICH pair it acts on — the names are
// fetched from GET /entities/{id} (useEntity, cached/deduped across shared
// endpoints) and the decision stays LOCKED until both resolve (Codex #106 P1b), and
// a failed lookup keeps it locked (only "(名稱載入失敗)" shows, never the pair) with
// a retry rather than silently enabling the decision (Codex #106 P1c).
function endpointName(q: ReturnType<typeof useEntity>): string {
  if (q.isPending) return "…";
  if (q.isError) return "(名稱載入失敗)";
  return q.data?.canonical_name ?? "(未知)";
}

// One relation row: resolves src→type→dst names, gates the decision on them,
// keeps the CORRECTED GOV2-fe-1 decision UX (deterministic idem-key in the hook,
// 保留 inline, 排除 behind a confirm, whole-queue isPending lock).
function RelationRow({
  project,
  r,
  decide,
}: {
  project: string;
  r: Relation;
  decide: ReturnType<typeof useDecideReviewTarget>;
}) {
  const src = useEntity(project, r.src_entity_id);
  const dst = useEntity(project, r.dst_entity_id);
  const [confirmingReject, setConfirmingReject] = useState(false);
  const [showEvidence, setShowEvidence] = useState(false);

  // no decision until the operator can actually SEE the pair. A still-loading OR a
  // FAILED name lookup both keep it locked — an error only shows "(名稱載入失敗)",
  // never the pair, so enabling on error would defeat the safeguard and permit an
  // irreversible reject on unknown endpoints (Codex #106 P1c). A retry recovers.
  const namesUnresolved = src.isPending || dst.isPending || src.isError || dst.isError;
  const locked = decide.isPending || namesUnresolved;
  const namesFailed = src.isError || dst.isError;

  const onApprove = () =>
    decide.mutate({ kind: "relation", targetId: r.id, verb: "approve", reason: null });
  const onConfirmReject = () => {
    decide.mutate({ kind: "relation", targetId: r.id, verb: "reject", reason: null });
    setConfirmingReject(false);
  };
  const retryNames = () => {
    void src.refetch();
    void dst.refetch();
  };

  return (
    <li className="targets__row">
      <p className="targets__relation">
        <span className="targets__name">{endpointName(src)}</span>
        {" —"}
        {r.type}
        {"→ "}
        <span className="targets__name">{endpointName(dst)}</span>
      </p>
      {namesFailed ? (
        <p className="review__line review__line--error">
          端點名稱載入失敗,無法確認是哪一對,已暫停決定。
          <button type="button" className="targets__evidence-toggle" onClick={retryNames}>
            重試
          </button>
        </p>
      ) : null}
      {r.confidence !== null && r.confidence !== undefined ? (
        <p className="targets__confidence">信心 {r.confidence.toFixed(2)}</p>
      ) : null}
      <div className="targets__evidence">
        <button
          type="button"
          className="targets__evidence-toggle"
          aria-expanded={showEvidence}
          onClick={() => setShowEvidence((o) => !o)}
        >
          {showEvidence ? "收合原文證據" : "查看原文證據"}
        </button>
        {showEvidence ? <RelationEvidence project={project} relationId={r.id} /> : null}
      </div>
      {confirmingReject ? (
        <div className="targets__confirm" role="alertdialog" aria-label="確認排除">
          <p>排除後這個關聯會從上線的知識庫移除,目前無法從介面復原。確定嗎?</p>
          <button
            type="button"
            className="targets__reject"
            disabled={decide.isPending}
            onClick={onConfirmReject}
          >
            確定{VERB_LABEL.reject}
          </button>
          <button
            type="button"
            disabled={decide.isPending}
            onClick={() => setConfirmingReject(false)}
          >
            取消
          </button>
        </div>
      ) : (
        <div className="targets__actions">
          <button type="button" className="targets__approve" disabled={locked} onClick={onApprove}>
            {VERB_LABEL.approve}
          </button>
          <button
            type="button"
            className="targets__reject"
            disabled={locked}
            onClick={() => setConfirmingReject(true)}
          >
            {VERB_LABEL.reject}
          </button>
        </div>
      )}
      {/* raw ids / signatures / store-vocabulary = the chrome-invariant <details>
          escape hatch (the per-decision audit trail, GOV2 §17) */}
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
            <dt>attributes</dt>
            <dd>{JSON.stringify(r.attributes ?? {})}</dd>
          </div>
        </dl>
      </details>
    </li>
  );
}

// GOV2-fe-2: the relation review queue (DESIGN §17). Relations the pipeline flagged
// `needs_review`, each shown as src→type→dst with confidence and (on demand) the
// evidenced quote. Reuses the CORRECTED GOV2-fe-1 pattern (Codex #105): the shared
// useDecideReviewTarget (deterministic idem-key), 保留 inline / 排除-confirm, and
// the whole-queue isPending lock (Codex #104 P2).
export function RelationReview({ project }: { project: string }) {
  const queue = useRelationReviewQueue(project);
  const decide = useDecideReviewTarget(project);

  if (queue.isPending) return <p className="review__line">載入審核佇列…</p>;
  if (queue.isError)
    return <p className="review__line review__line--error">無法載入佇列:{message(queue.error)}</p>;
  if (queue.data.length === 0) return <p className="review__line">目前沒有待審的關聯。</p>;

  return (
    <ul className="targets">
      {queue.data.map((r) => (
        <RelationRow key={r.id} project={project} r={r} decide={decide} />
      ))}
      {decide.isError ? (
        <li className="review__line review__line--error">決定失敗:{message(decide.error)}</li>
      ) : null}
    </ul>
  );
}
