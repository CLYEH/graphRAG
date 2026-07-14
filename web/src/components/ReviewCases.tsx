import { useCallback, useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
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
  const { data: candidates, isPending, isError, error } = useMergeCandidates(project);
  const [index, setIndex] = useState(0);

  if (isPending) return <p className="runs__muted">載入審核佇列…</p>;
  if (isError)
    return (
      <p className="runs__muted runs__muted--error">
        無法載入審核佇列:{error instanceof Error ? error.message : "unknown error"}
      </p>
    );

  // The queue endpoint returns only still-reviewable rows (pending + deferred —
  // api/routers/review.py keeps the list identical to §19's pending_review
  // gauge), so this filter is a defensive no-op today; it keeps a decided row
  // from flashing back into the flow if that server rule ever loosens.
  const queue = candidates.filter((c) => c.status === "pending" || c.status === "deferred");
  if (queue.length === 0)
    return (
      <p className="runs__muted">
        目前沒有需要審核的項目。建置後若系統發現疑似重複的實體,會出現在這裡。
      </p>
    );

  // A decision shrinks the queue on refetch — clamp instead of pointing past
  // the end, so「決定 → 自動跳到下一筆」falls out of the invalidation.
  const safe = Math.min(index, queue.length - 1);
  const current = queue[safe];

  return (
    <div className="review__flow">
      <div className="review__nav">
        <span className="review__progress">
          第 {safe + 1} 筆,共 {queue.length} 筆
        </span>
        <button type="button" onClick={() => setIndex(Math.max(0, safe - 1))} disabled={safe === 0}>
          上一筆
        </button>
        <button
          type="button"
          onClick={() => setIndex(Math.min(queue.length - 1, safe + 1))}
          disabled={safe >= queue.length - 1}
        >
          下一筆
        </button>
      </div>
      {/* key resets per-case state (reason, confirm) when the case changes */}
      <CaseCard
        key={current.id}
        candidate={current}
        project={project}
        onSkipped={() => setIndex(safe + 1)}
      />
    </div>
  );
}

function CaseCard({
  candidate,
  project,
  onSkipped,
}: {
  candidate: MergeCandidate;
  project: string;
  onSkipped: () => void;
}) {
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState<Extract<ReviewVerb, "approve" | "reject"> | null>(
    null,
  );
  const decide = useDecideMergeCandidate(project);
  // Scope loss (class 19): a context read can PROVE the active build moved on
  // (SubgraphScopeError, or a success stamped with a different build) — then
  // this whole queue is stale and a decide would 404 against the rebound build
  // (review.py re-resolves the active build). Proof blocks the verbs and
  // refetches the queue; a scope-NEUTRAL context failure stays local and never
  // blocks deciding (Codex #76 round 2).
  const queryClient = useQueryClient();
  const [scopeLost, setScopeLost] = useState(false);
  const onScopeLoss = useCallback(() => {
    setScopeLost(true);
    void queryClient.invalidateQueries({ queryKey: ["merge-candidates", project] });
  }, [queryClient, project]);
  const blocked = decide.isPending || scopeLost;

  const submit = (verb: ReviewVerb) => {
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
          project={project}
          entityId={candidate.left_entity_id}
          snapshot={candidate.left_snapshot}
          expectedBuildId={candidate.build_id}
          onScopeLoss={onScopeLoss}
        />
        <EntityPanel
          project={project}
          entityId={candidate.right_entity_id}
          snapshot={candidate.right_snapshot}
          expectedBuildId={candidate.build_id}
          onScopeLoss={onScopeLoss}
        />
      </div>
      <p className="review__score">{scoreWords(candidate.score)}</p>
      {candidate.status === "deferred" && (
        <p className="review__note">這一筆先前已跳過;這次只能選「合併」或「分開」。</p>
      )}
      {scopeLost && (
        <p className="review__error">
          知識庫版本已切換,這批審核清單已過期——正在重新載入最新佇列,此案暫停決定。
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

function EntityPanel({
  project,
  entityId,
  snapshot,
  expectedBuildId,
  onScopeLoss,
}: {
  project: string;
  entityId: string;
  snapshot: MergeCandidate["left_snapshot"];
  expectedBuildId: string;
  onScopeLoss: () => void;
}) {
  const name = snapshotText(snapshot, "name");
  const type = snapshotText(snapshot, "type");

  return (
    <div className="review__panel">
      <p className="review__name">{name ?? "(名稱快照缺失)"}</p>
      {type && <span className="review__chip">{type}</span>}
      <EntityContext
        project={project}
        entityId={entityId}
        expectedBuildId={expectedBuildId}
        onScopeLoss={onScopeLoss}
      />
    </div>
  );
}

// One evidenced relation as the case's context sentence. Evidence rides ONLY
// the relation detail GET (FE4's rule), so this is subgraph(hops=1) → first
// edge → detail; both fetches are per-CURRENT-case (lazy via the case key) and
// cached by react-query. Failure semantics follow class 19's verdict placement:
// a scope-NEUTRAL failure (store down, plain errors) stays local and never
// blocks the decision — the operator may know the names cold — but a failure
// that PROVES the world moved (SubgraphScopeError: build gone/changed, seed no
// longer active) or a success stamped with a DIFFERENT build than the
// candidate's escalates via onScopeLoss: the queue itself is stale and any
// decide would 404 (review.py rebinds the active build per request).
function EntityContext({
  project,
  entityId,
  expectedBuildId,
  onScopeLoss,
}: {
  project: string;
  entityId: string;
  expectedBuildId: string;
  onScopeLoss: () => void;
}) {
  const sub = useSubgraph(project, entityId, 1);
  const edge = sub.data?.graph.edges.find((e) => e.src === entityId || e.dst === entityId);
  const rel = useRelation(project, edge ? edge.id : undefined);

  // buildId null = the meta didn't name a build — not proof of anything;
  // only a NAMED, DIFFERENT build proves the swap.
  const scopeLost =
    (sub.isError && sub.error instanceof SubgraphScopeError) ||
    (sub.isSuccess && sub.data.buildId !== null && sub.data.buildId !== expectedBuildId);
  useEffect(() => {
    if (scopeLost) onScopeLoss();
  }, [scopeLost, onScopeLoss]);

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
    const node = sub.data.graph.nodes.find((n) => n.id === id);
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
