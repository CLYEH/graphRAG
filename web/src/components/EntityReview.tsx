import { useRef, useState } from "react";

import { useDecideReviewTarget, useEntityReviewList } from "../api/queries";

import type { Entity, ReviewTargetVerb } from "../api/queries";

// approve/reject → operator words, keyed on the contract verb enum so a new verb
// is a type error, not a silently-english label (the UXA3 translation layer).
const VERB_LABEL: Record<ReviewTargetVerb, string> = {
  approve: "保留",
  reject: "排除",
};

function message(error: unknown): string {
  return error instanceof Error ? error.message : "unknown error";
}

// The per-row audit fold — raw ids / entity_key / store-vocabulary live ONLY here
// (the chrome-invariant escape hatch; the per-decision audit trail, GOV2 §17).
function EntityAudit({ e }: { e: Entity }) {
  return (
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
  );
}

// GOV2-fe: the entity review surface (DESIGN §17), two views:
// - 待審 (needs_review): the queue — 保留 (approve, non-destructive) inline; 排除
//   (reject, removes from the active graph) behind a confirm. Deterministic
//   idem-key in the hook (queue rows are decided at most once).
// - 已排除 (rejected, GOV2-fe-4a): the decided view — rows a curator excluded,
//   with a 復原(保留) restore (approve re-activates; review.py appends +
//   latest-manual-wins). Restores mint a FRESH random idem-key per attempt: a
//   reject→restore→reject cycle would otherwise replay an earlier cycle's stored
//   response. Non-destructive → inline, no confirm.
// Both views page INCREMENTALLY (Codex #105 P2) with a load-more; a next-page
// failure keeps the loaded rows and offers an inline retry (the #102 discipline).
// Every decision control locks while a decision posts OR the list refreshes
// (decide.isPending || list.isFetching — Codex #104 P2 / #106 P1d).
export function EntityReview({ project }: { project: string }) {
  const [view, setView] = useState<"queue" | "rejected">("queue");
  const list = useEntityReviewList(project, view === "queue" ? "needs_review" : "rejected");
  const decide = useDecideReviewTarget(project);
  // the entity id awaiting a reject confirm (one at a time; queue view only)
  const [confirmingReject, setConfirmingReject] = useState<string | null>(null);

  // lock on isError too: if the post-decision refetch FAILS, react-query keeps
  // the old pages and clears isFetching while setting isError — the decided row
  // would otherwise re-enable and an opposite verb would silently reverse the
  // decision just made (Codex #108 P1). Decisions stay locked until a clean load.
  const locked = decide.isPending || list.isFetching || list.isError;

  const onApprove = (id: string) =>
    decide.mutate({ kind: "entity", targetId: id, verb: "approve", reason: null });
  const onConfirmReject = (id: string) => {
    decide.mutate({ kind: "entity", targetId: id, verb: "reject", reason: null });
    setConfirmingReject(null);
  };
  // ONE key per LOGICAL restore, not per click (Codex #108 R2): the key is minted
  // on the first attempt for a row and RETAINED across failed retries — a lost
  // response replayed with the same key returns the stored 200 instead of
  // appending a second approval (whose newer latest-wins timestamp could override
  // an intervening decision). Cleared on success, so a later reject→restore cycle
  // mints a fresh key (the deterministic `${id}:approve` would replay across
  // cycles — see useDecideReviewTarget).
  const restoreKeys = useRef(new Map<string, string>());
  const onRestore = (id: string) => {
    const key = restoreKeys.current.get(id) ?? crypto.randomUUID();
    restoreKeys.current.set(id, key);
    decide.mutate(
      { kind: "entity", targetId: id, verb: "approve", reason: null, idempotencyKey: key },
      { onSuccess: () => restoreKeys.current.delete(id) },
    );
  };

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
        // initial load failed — nothing to show, fail loud
        <p className="review__line review__line--error">無法載入清單:{message(list.error)}</p>
      ) : rows.length === 0 ? (
        <p className="review__line">
          {view === "queue" ? "目前沒有待審的知識點。" : "沒有已排除的知識點。"}
        </p>
      ) : (
        <ul className="targets">
          {rows.map((e) => (
            <li key={e.id} className="targets__row">
              <div className="targets__head">
                <span className="targets__name">{e.canonical_name}</span>
                <span className="targets__type">{e.type}</span>
              </div>
              {view === "rejected" ? (
                <div className="targets__actions">
                  {/* restore re-activates the entity — non-destructive, inline */}
                  <button
                    type="button"
                    className="targets__approve"
                    disabled={locked}
                    onClick={() => onRestore(e.id)}
                  >
                    復原({VERB_LABEL.approve})
                  </button>
                </div>
              ) : confirmingReject === e.id ? (
                <div className="targets__confirm" role="alertdialog" aria-label="確認排除">
                  <p>排除後這個知識點會從上線的知識庫移除(可在「已排除」視圖復原)。確定嗎?</p>
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
              <EntityAudit e={e} />
            </li>
          ))}
        </ul>
      )}

      {/* any list failure with rows on screen: keep the rows visible (#102) but
          retry via a FULL refetch — it recomputes pageParams from the fresh page 1
          (v5 re-threading), which recovers BOTH a transient failure AND the
          build-swap pin trip; a fetchNextPage retry would replay the stale cursor
          + old pin forever (Codex #108 P2) */}
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
