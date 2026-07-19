import { useState } from "react";

import {
  useDecideReviewTarget,
  useEntity,
  useRelation,
  useRelationReviewList,
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

// One relation row. mode="queue": 保留 inline + 排除 behind a confirm (the
// corrected GOV2-fe-1 pattern). mode="restore" (the 已排除 decided view,
// GOV2-fe-4a): a single 復原(保留) — approve re-activates; deliberate
// re-decision, so the caller mints a fresh idem-key. EVERY decision entry point
// gates on `locked` (decide posting, list refreshing, names unresolved) — the
// operator must see the pair before any decision, including a restore that
// re-adds the relation to the live graph (Codex #106 P1b/P1c/P2/P1d).
function RelationRow({
  project,
  r,
  decide,
  listRefreshing,
  mode,
}: {
  project: string;
  r: Relation;
  decide: ReturnType<typeof useDecideReviewTarget>;
  listRefreshing: boolean;
  mode: "queue" | "restore";
}) {
  const src = useEntity(project, r.src_entity_id);
  const dst = useEntity(project, r.dst_entity_id);
  const [confirmingReject, setConfirmingReject] = useState(false);
  const [showEvidence, setShowEvidence] = useState(false);

  const namesUnresolved = src.isPending || dst.isPending || src.isError || dst.isError;
  const locked = decide.isPending || namesUnresolved || listRefreshing;
  const namesFailed = src.isError || dst.isError;

  const onApprove = () =>
    decide.mutate({ kind: "relation", targetId: r.id, verb: "approve", reason: null });
  const onConfirmReject = () => {
    decide.mutate({ kind: "relation", targetId: r.id, verb: "reject", reason: null });
    setConfirmingReject(false);
  };
  const onRestore = () =>
    decide.mutate({
      kind: "relation",
      targetId: r.id,
      verb: "approve",
      reason: null,
      // deliberate re-decision — fresh key per attempt (see useDecideReviewTarget)
      idempotencyKey: crypto.randomUUID(),
    });
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
      {mode === "restore" ? (
        <div className="targets__actions">
          <button type="button" className="targets__approve" disabled={locked} onClick={onRestore}>
            復原({VERB_LABEL.approve})
          </button>
        </div>
      ) : confirmingReject ? (
        <div className="targets__confirm" role="alertdialog" aria-label="確認排除">
          <p>排除後這個關聯會從上線的知識庫移除(可在「已排除」視圖復原)。確定嗎?</p>
          <button
            type="button"
            className="targets__reject"
            disabled={locked}
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

// GOV2-fe: the relation review surface (DESIGN §17), two views: 待審 (the queue)
// and 已排除 (the decided view with restore, GOV2-fe-4a). Both page INCREMENTALLY
// (Codex #105 P2) with a load-more; a next-page failure keeps the loaded rows and
// offers an inline retry (the #102 discipline).
export function RelationReview({ project }: { project: string }) {
  const [view, setView] = useState<"queue" | "rejected">("queue");
  const list = useRelationReviewList(project, view === "queue" ? "needs_review" : "rejected");
  const decide = useDecideReviewTarget(project);

  const rows = list.data?.pages.flatMap((p) => p.rows) ?? [];

  return (
    <div>
      <div className="targets__views">
        <button
          type="button"
          className={`targets__view${view === "queue" ? " targets__view--on" : ""}`}
          aria-pressed={view === "queue"}
          onClick={() => setView("queue")}
        >
          待審
        </button>
        <button
          type="button"
          className={`targets__view${view === "rejected" ? " targets__view--on" : ""}`}
          aria-pressed={view === "rejected"}
          onClick={() => setView("rejected")}
        >
          已排除
        </button>
      </div>

      {list.isPending ? (
        <p className="review__line">載入中…</p>
      ) : list.isError && !list.data ? (
        <p className="review__line review__line--error">無法載入清單:{message(list.error)}</p>
      ) : rows.length === 0 ? (
        <p className="review__line">
          {view === "queue" ? "目前沒有待審的關聯。" : "沒有已排除的關聯。"}
        </p>
      ) : (
        <ul className="targets">
          {rows.map((r) => (
            <RelationRow
              key={r.id}
              project={project}
              r={r}
              decide={decide}
              // isError too: a failed post-decision refetch keeps stale pages with
              // isFetching false — the decided row must stay locked (Codex #108 P1)
              listRefreshing={list.isFetching || list.isError}
              mode={view === "rejected" ? "restore" : "queue"}
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
