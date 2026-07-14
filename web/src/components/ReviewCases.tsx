import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  DetailScopeGoneError,
  PolicyMissingError,
  SubgraphScopeError,
  useDecideMergeCandidate,
  useMergeCandidates,
  useRelation,
  useSubgraph,
} from "../api/queries";

import type { MergeCandidate, ReviewVerb } from "../api/queries";

// UXA1 (Track 4): the decision surface carries the decision's basis. One case
// at a time; the headline is the two snapshot NAMES (the list payload already
// carries them — no extra reads), context sentences are the entity's evidenced
// relations fetched lazily for the CURRENT case only, and the §17-terminal
// verbs (approve/reject) sit behind an explicit confirm step because BA5 makes
// them irreversible — the UI must make a misclick recoverable by interaction.

/** Safe narrow over the contract's untyped snapshot bag ({[k]: unknown} | null). */
function snapshotText(snap: MergeCandidate["left_snapshot"], key: string): string | null {
  const v = snap?.[key];
  return typeof v === "string" && v.trim() !== "" ? v : null;
}

/** §17 名稱相似度 → 人話;原始分數住「進階」摺疊。 */
function scoreWords(score: number): string {
  if (score >= 0.9) return "名稱幾乎相同";
  if (score >= 0.75) return "名稱高度相似";
  return "名稱有部分相似";
}

export function ReviewCases({ project }: { project: string }) {
  const { data: candidates, isPending, isError, error, isFetching } = useMergeCandidates(project);
  const [index, setIndex] = useState(0);
  // Page-level guards (Codex #76 R4): both live HERE, not in the keyed card —
  // per-card state is escapable by navigation.
  // (a) `deciding`: while a decision POST is in flight the index must not move,
  //     or it advances against the old list and silently skips the case that
  //     slides into the removed row's slot.
  // (b) `frozenKey`: one card proving scope loss freezes the WHOLE queue —
  //     every other card is from the same dead snapshot. Anchored to the
  //     queue's CONTENT (candidate-id join), not a fetch timestamp: a refetch
  //     that returns the SAME rows keeps the freeze (re-proof would loop on
  //     the cached context error), and one that returns a CHANGED queue
  //     unfreezes exactly because the world visibly moved on.
  const [deciding, setDeciding] = useState(false);
  const [frozenKey, setFrozenKey] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const queue = (candidates ?? []).filter((c) => c.status === "pending" || c.status === "deferred");
  const queueKey = queue.map((c) => c.id).join(",");
  const queueKeyRef = useRef(queueKey);
  queueKeyRef.current = queueKey;
  const onScopeLoss = useCallback(() => {
    setFrozenKey(queueKeyRef.current);
    void queryClient.invalidateQueries({ queryKey: ["merge-candidates", project] });
  }, [queryClient, project]);
  const scopeFrozen = frozenKey !== null && frozenKey === queueKey;
  // A card change moots any in-flight decision from the PRIOR card, and the
  // mutate-level onSettled is skipped when the card unmounts before the POST
  // settles (react-query v5 gates mutate callbacks on hasListeners) — so the
  // deciding-clear must not depend on that callback surviving unmount.
  const currentId = queue[index]?.id;
  useEffect(() => {
    setDeciding(false);
  }, [currentId]);
  // Queue REPLACEMENT vs queue SHRINK (Codex #76 R6/R8): when our own decision
  // removed a row, the walk keeps its position — the next case slides into the
  // same slot, and deciding the tail lands on the end-of-pass panel (R3). Any
  // OTHER content change (new active build, another tab, a project switch)
  // must reset the walk, or a shorter queue hides behind the end panel and a
  // longer one starts midway. The two are told apart by the removal's SHAPE,
  // not a boolean: the flag records WHICH candidate id we expect to vanish
  // (armed synchronously before the mutate — R7), and only a new queue equal
  // to the old one minus exactly that id counts as our shrink. A coincident
  // external replacement therefore resets even while our POST is in flight
  // (R8 closed the boolean flag's residual). Defer never changes the content
  // by itself, so the tail-defer end-panel state is untouched.
  const expectedRemovalRef = useRef<string | null>(null);
  const setExpectedRemoval = useCallback((id: string | null) => {
    expectedRemovalRef.current = id;
  }, []);
  const prevQueueKeyRef = useRef(queueKey);
  useEffect(() => {
    if (prevQueueKeyRef.current === queueKey) return;
    const prevKey = prevQueueKeyRef.current;
    prevQueueKeyRef.current = queueKey;
    const removal = expectedRemovalRef.current;
    expectedRemovalRef.current = null;
    const ourShrink =
      removal !== null &&
      prevKey
        .split(",")
        .filter((id) => id !== removal)
        .join(",") === queueKey;
    if (!ourShrink) setIndex(0);
  }, [queueKey]);

  if (isPending) return <p className="runs__muted">載入審核佇列…</p>;
  if (isError)
    return (
      <p className="runs__muted runs__muted--error">
        無法載入審核佇列:{error instanceof Error ? error.message : "unknown error"}
      </p>
    );

  // The queue keeps only still-reviewable rows (pending + deferred — the
  // filter above mirrors api/routers/review.py, which keeps the list identical
  // to §19's pending_review gauge; a defensive no-op today that stops a
  // decided row from flashing back if that server rule ever loosens).
  if (queue.length === 0)
    return (
      <p className="runs__muted">
        目前沒有需要審核的項目。建置後若系統發現疑似重複的實體,會出現在這裡。
      </p>
    );

  // Walking past the end is a real state, not an index bug: deferring the LAST
  // case advances past it (its row intentionally stays in the queue), and
  // deciding the last case shrinks the queue underneath the index. Clamping
  // back would re-present a case the curator just skipped — with the skip
  // button gone, that FORCES a decision on the very pair they declined to
  // decide (Codex #76 R3). End the pass instead.
  if (index >= queue.length)
    return (
      <div className="review__flow">
        <p className="runs__muted">這一輪看到底了。佇列還有 {queue.length} 筆未定案(含跳過的)。</p>
        <button type="button" onClick={() => setIndex(0)}>
          從頭再看一輪
        </button>
      </div>
    );
  const current = queue[index];
  // navigation is as dangerous as deciding while the world is in motion: an
  // in-flight decision, a proven-stale queue, or a running refetch all mean
  // "the list under this index is about to change"
  const navLocked = deciding || scopeFrozen || isFetching;

  return (
    <div className="review__flow">
      <div className="review__nav">
        <span className="review__progress">
          第 {index + 1} 筆,共 {queue.length} 筆
        </span>
        <button
          type="button"
          onClick={() => setIndex(Math.max(0, index - 1))}
          disabled={index === 0 || navLocked}
        >
          上一筆
        </button>
        <button
          type="button"
          onClick={() => setIndex(Math.min(queue.length - 1, index + 1))}
          disabled={index >= queue.length - 1 || navLocked}
        >
          下一筆
        </button>
      </div>
      {/* key resets per-case state (reason, confirm) when the case changes */}
      <CaseCard
        key={current.id}
        candidate={current}
        project={project}
        onSkipped={() => setIndex(index + 1)}
        setExpectedRemoval={setExpectedRemoval}
        scopeFrozen={scopeFrozen}
        queueRefreshing={isFetching}
        onScopeLoss={onScopeLoss}
        onDecidingChange={setDeciding}
      />
    </div>
  );
}

function CaseCard({
  candidate,
  project,
  onSkipped,
  setExpectedRemoval,
  scopeFrozen,
  queueRefreshing,
  onScopeLoss,
  onDecidingChange,
}: {
  candidate: MergeCandidate;
  project: string;
  onSkipped: () => void;
  setExpectedRemoval: (id: string | null) => void;
  scopeFrozen: boolean;
  queueRefreshing: boolean;
  onScopeLoss: () => void;
  onDecidingChange: (deciding: boolean) => void;
}) {
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState<Extract<ReviewVerb, "approve" | "reject"> | null>(
    null,
  );
  const decide = useDecideMergeCandidate(project);

  // The four context reads are SCOPE SENTINELS, owned here so proof and
  // pendingness are computed in one place (Codex #76 R9): subgraph(hops=1)
  // per side, then the first evidenced relation's detail (evidence rides ONLY
  // the detail GET — FE4's rule). Failure semantics follow class 19's verdict
  // placement: a scope-NEUTRAL failure (store down, plain errors, missing
  // policy) stays a local context line and never blocks deciding, but a
  // failure that PROVES the world moved (SubgraphScopeError; a success
  // stamped with a different, non-null build; DetailScopeGoneError one hop
  // later) escalates via onScopeLoss — the queue itself is stale and any
  // decide would 404 (review.py rebinds the active build per request).
  const subLeft = useSubgraph(project, candidate.left_entity_id, 1);
  const subRight = useSubgraph(project, candidate.right_entity_id, 1);
  const edgeLeft = subLeft.data?.graph.edges.find(
    (e) => e.src === candidate.left_entity_id || e.dst === candidate.left_entity_id,
  );
  const edgeRight = subRight.data?.graph.edges.find(
    (e) => e.src === candidate.right_entity_id || e.dst === candidate.right_entity_id,
  );
  const relLeft = useRelation(project, edgeLeft ? edgeLeft.id : undefined);
  const relRight = useRelation(project, edgeRight ? edgeRight.id : undefined);

  // buildId null = the meta didn't name a build — not proof of anything;
  // only a NAMED, DIFFERENT build proves the swap.
  const sideProof = (sub: SubResult, rel: RelResult): boolean =>
    (sub.isError && sub.error instanceof SubgraphScopeError) ||
    (sub.isSuccess && sub.data.buildId !== null && sub.data.buildId !== candidate.build_id) ||
    (rel.isError && rel.error instanceof DetailScopeGoneError);
  const scopeProof = sideProof(subLeft, relLeft) || sideProof(subRight, relRight);
  useEffect(() => {
    if (scopeProof) onScopeLoss();
  }, [scopeProof, onScopeLoss]);

  // An UNSETTLED sentinel is an undetermined verdict: before the first
  // round trip settles (isPending) AND while cached sentinel data revalidates
  // (isFetching without isPending — navigating back to a seen case remounts
  // it from cache, and that cache may predate a build swap), this candidate
  // may already belong to a dead build — the verbs wait until every sentinel
  // settles into a proof (freeze) or a neutral answer (Codex #76 R9/R10).
  // isFetching covers both windows; a disabled query (no edge yet / no edge
  // at all) has fetchStatus idle, so it never counts.
  const scopeChecking =
    subLeft.isFetching || subRight.isFetching || relLeft.isFetching || relRight.isFetching;

  // queueRefreshing is the FE1 fail-closed gate on the WRITE side (R5): while
  // the queue refreshes, the rows on screen may be about to be replaced.
  const blocked = decide.isPending || scopeFrozen || queueRefreshing || scopeChecking;

  const submit = (verb: ReviewVerb) => {
    onDecidingChange(true);
    // approve/reject will remove THIS row — record its id BEFORE the mutate
    // call: the hook-level onSuccess invalidates and AWAITS the refetch first,
    // and that refetch can unmount this keyed card, skipping every
    // mutate-level callback (v5 gates them on hasListeners) — a
    // callback-armed flag never rises (Codex #76 R7). Recording the ID rather
    // than a boolean lets the parent verify the removal's shape, so a
    // coincident external replacement still resets (R8). A failed POST
    // disarms in onError (no removal is coming).
    if (verb !== "defer") setExpectedRemoval(candidate.id);
    decide.mutate(
      {
        candidateId: candidate.id,
        verb,
        reason: reason.trim() === "" ? null : reason.trim(),
      },
      {
        // approve/reject leave the queue on refetch, so the clamp advances by
        // itself; a DEFERRED row intentionally STAYS in the queue (review.py
        // keeps pending+deferred), so without an explicit step forward the
        // same pair re-renders and「跳過,下次再問」skips nothing (Codex #76).
        onSuccess: () => {
          if (verb === "defer") onSkipped();
        },
        onError: () => {
          if (verb !== "defer") setExpectedRemoval(null);
        },
        // best-effort: skipped if this card unmounted first (v5 gates mutate
        // callbacks on hasListeners) — the parent's currentId effect covers that
        onSettled: () => {
          onDecidingChange(false);
        },
      },
    );
    setConfirming(null);
  };

  // §17 transitions (BA5, enforced under the lock): pending → any verb;
  // deferred → approve/reject only — never re-defer, so the skip affordance
  // disappears instead of being offered and refused.
  const canDefer = candidate.status === "pending";

  return (
    <section className="review__case" aria-label="審核案例">
      <h2 className="review__question">這兩個是同一個東西嗎?</h2>
      <div className="review__panels">
        <EntityPanel
          snapshot={candidate.left_snapshot}
          sub={subLeft}
          rel={relLeft}
          edge={edgeLeft}
          scopeLost={sideProof(subLeft, relLeft)}
        />
        <EntityPanel
          snapshot={candidate.right_snapshot}
          sub={subRight}
          rel={relRight}
          edge={edgeRight}
          scopeLost={sideProof(subRight, relRight)}
        />
      </div>
      <p className="review__score">{scoreWords(candidate.score)}</p>
      {candidate.status === "deferred" && (
        <p className="review__note">這一筆先前已跳過;這次只能選「合併」或「分開」。</p>
      )}
      {scopeFrozen && (
        <p className="review__error">
          知識庫版本已切換,這批審核清單已過期——正在重新載入最新佇列,暫停所有決定。
          若此訊息持續,請重新整理頁面。
        </p>
      )}
      <input
        className="review__reason"
        aria-label="決定理由"
        placeholder="決定理由(選填,會留在審核紀錄)"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
      />
      {confirming === null ? (
        <div className="review__actions">
          <button type="button" onClick={() => setConfirming("approve")} disabled={blocked}>
            是,合併
          </button>
          <button type="button" onClick={() => setConfirming("reject")} disabled={blocked}>
            不是,分開
          </button>
          {canDefer && (
            <button type="button" onClick={() => submit("defer")} disabled={blocked}>
              跳過,下次再問
            </button>
          )}
        </div>
      ) : (
        <div className="review__confirm" role="alertdialog" aria-label="確認決定">
          <p>
            {confirming === "approve" ? "合併" : "分開"}
            送出後<strong>無法更改</strong>。確定嗎?
          </p>
          <button type="button" onClick={() => submit(confirming)} disabled={blocked}>
            {confirming === "approve" ? "確定合併" : "確定分開"}
          </button>
          <button type="button" onClick={() => setConfirming(null)} disabled={blocked}>
            取消
          </button>
        </div>
      )}
      {decide.isError && (
        <p className="review__error">
          決定送出失敗:{decide.error instanceof Error ? decide.error.message : "unknown error"}
        </p>
      )}
      <details className="review__advanced">
        <summary>進階(原始資料)</summary>
        <dl>
          <div>
            <dt>相似度分數</dt>
            <dd>{candidate.score.toFixed(3)}</dd>
          </div>
          <div>
            <dt>候選 id</dt>
            <dd>{candidate.id}</dd>
          </div>
          <div>
            <dt>左實體 id</dt>
            <dd>{candidate.left_entity_id}</dd>
          </div>
          <div>
            <dt>右實體 id</dt>
            <dd>{candidate.right_entity_id}</dd>
          </div>
          <div>
            <dt>build</dt>
            <dd>{candidate.build_id}</dd>
          </div>
          <div>
            <dt>features</dt>
            <dd>{candidate.features ? JSON.stringify(candidate.features) : "—"}</dd>
          </div>
          <div>
            <dt>impact</dt>
            <dd>{candidate.impact ? JSON.stringify(candidate.impact) : "—"}</dd>
          </div>
        </dl>
      </details>
    </section>
  );
}

type SubResult = ReturnType<typeof useSubgraph>;
type RelResult = ReturnType<typeof useRelation>;
type SubEdge = NonNullable<SubResult["data"]>["graph"]["edges"][number];

function EntityPanel({
  snapshot,
  sub,
  rel,
  edge,
  scopeLost,
}: {
  snapshot: MergeCandidate["left_snapshot"];
  sub: SubResult;
  rel: RelResult;
  edge: SubEdge | undefined;
  scopeLost: boolean;
}) {
  const name = snapshotText(snapshot, "name");
  const type = snapshotText(snapshot, "type");

  return (
    <div className="review__panel">
      <p className="review__name">{name ?? "(名稱快照缺失)"}</p>
      {type && <span className="review__chip">{type}</span>}
      <EntityContext sub={sub} rel={rel} edge={edge} scopeLost={scopeLost} />
    </div>
  );
}

// Presentational: the sentinel queries live in CaseCard (R9); this renders
// their settled shape — scope verdict, honest failure lines, or the evidenced
// relation with its quote.
function EntityContext({
  sub,
  rel,
  edge,
  scopeLost,
}: {
  sub: SubResult;
  rel: RelResult;
  edge: SubEdge | undefined;
  scopeLost: boolean;
}) {
  if (scopeLost)
    return <p className="review__context review__context--muted">知識庫版本已切換,此案已過期。</p>;
  if (sub.isPending) return <p className="review__context">載入上下文…</p>;
  if (sub.isError)
    return (
      <p className="review__context review__context--muted">
        {sub.error instanceof PolicyMissingError
          ? "此專案尚未設定查詢政策,無法載入上下文(仍可作決定)。"
          : `上下文載入失敗:${sub.error instanceof Error ? sub.error.message : "unknown error"}(仍可作決定)`}
      </p>
    );
  if (!edge)
    return (
      <p className="review__context review__context--muted">
        這個實體在圖譜中沒有帶證據的關聯可參考。
      </p>
    );

  const nodeLabel = (id: string): string => {
    const node = sub.data?.graph.nodes.find((n) => n.id === id);
    return node?.label ?? "(未知)";
  };
  const quote = rel.data?.evidence?.find((ev) => ev.quote)?.quote;

  return (
    <div className="review__context">
      <p className="review__relation">
        關聯:{nodeLabel(edge.src)} —{edge.type}→ {nodeLabel(edge.dst)}
      </p>
      {rel.isPending && <p className="review__quote">載入原文…</p>}
      {rel.isError && (
        <p className="review__quote review__context--muted">
          原文載入失敗:{rel.error instanceof Error ? rel.error.message : "unknown error"}
        </p>
      )}
      {rel.isSuccess && (
        <p className="review__quote">{quote ? `「${quote}」` : "(此關聯沒有原文引文)"}</p>
      )}
    </div>
  );
}
