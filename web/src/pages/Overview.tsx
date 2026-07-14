import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { useActivateBuild, useBuilds, useHealth, useSources } from "../api/queries";
import { isPathAddressable, useActiveProject } from "../project/projectRoute";
import "./Overview.css";

import type { Build } from "../api/queries";

// UXA2 (Track 4): the landing page answers「現在什麼狀態,下一步做什麼」.
//
// State machine, enumerated up front (class 20):
// - Reads: health (H) + sources (S) + builds (B). Any still pending → one
//   loading line; any error → one LOUD page error with the server's message
//   (a status page that guesses is worse than one that says it can't know).
//   Empty lists are answers, not errors.
// - The checklist is a PROJECTION of server state (class 17) — no client-side
//   wizard flags that can drift:
//     ① 匯入   done ⟺ S has at least one source
//     ② 建置   done ⟺ B has a build whose status ∈ {ready, active, archived}
//               (archived built and was superseded — the step still happened)
//     ③ 品質   done ⟺ the activation candidate (active build if any, else the
//               newest ready build) carries eval scores
//     ④ 上線   done ⟺ H.active_build_id is set
// - The activate WRITE is fail-closed (R5/R10 applied at birth): the button
//   locks while the mutation is in flight OR while builds/health are being
//   (re)fetched — the target row may be about to change. Two-step confirm
//   (activation swaps what every reader sees). The §14 preflight verdict is
//   the SERVER's: its 400 renders verbatim, plus plain guidance and a
//   copyable CLI eval command when the missing piece is scores (eval has no
//   API endpoint until UXC1 — the checklist hands you the command instead of
//   a dead end).
// - The activation target is captured AT CLICK (id shown in the confirm); if
//   the world moved meanwhile the server refuses and the message shows — the
//   UI never pre-empts §14.
export function Overview() {
  const project = useActiveProject();

  if (project === undefined) return <p className="overview__line">Unknown project.</p>;
  if (!isPathAddressable(project))
    return (
      <p className="overview__line overview__line--error">
        Project &quot;{project}&quot; isn&apos;t addressable over the API — its key contains
        &quot;/&quot; or is &quot;.&quot; / &quot;..&quot;, which a URL path segment can&apos;t
        carry.
      </p>
    );

  return (
    <section className="overview">
      <h1 className="overview__title">總覽</h1>
      <OverviewBody project={project} />
    </section>
  );
}

const BUILT_STATUSES = new Set(["ready", "active", "archived"]);

function OverviewBody({ project }: { project: string }) {
  const health = useHealth(project);
  const sources = useSources(project);
  const builds = useBuilds(project);

  if (health.isPending || sources.isPending || builds.isPending)
    return <p className="overview__line">載入中…</p>;
  const failed = [health.error, sources.error, builds.error].find((e) => e instanceof Error);
  if (failed)
    return (
      <p className="overview__line overview__line--error">無法載入專案狀態:{failed.message}</p>
    );
  if (!health.isSuccess || !sources.isSuccess || !builds.isSuccess) return null;

  const h = health.data;
  const activeBuild = builds.data.find((b) => b.id === h.active_build_id);
  // the activation candidate: the active build if one exists (step ③ then
  // reads ITS eval), else the newest ready build (API order: newest first)
  const newestReady = builds.data.find((b) => b.status === "ready");
  const candidate = activeBuild ?? newestReady;

  const step1 = sources.data.length > 0;
  const step2 = builds.data.some((b) => BUILT_STATUSES.has(b.status));
  const step3 = candidate !== undefined && candidate.eval !== null;
  const step4 = h.active_build_id !== null;

  const counts = h.counts as Record<string, number | undefined>;

  return (
    <div className="overview__body">
      {step4 ? (
        <p className="overview__status overview__status--ok">
          ✅ 服務中
          {activeBuild?.activated_at
            ? ` — 知識庫於 ${formatTime(activeBuild.activated_at)} 上線`
            : ""}
        </p>
      ) : step2 ? (
        <p className="overview__status overview__status--warn">🟡 已建置,尚未上線</p>
      ) : (
        <p className="overview__status">⚪ 尚未開始 — 照下面四步把知識庫建起來</p>
      )}

      {(h.pending_review ?? 0) > 0 && (
        <Link to="../review" className="overview__card overview__card--action">
          ⚠ 有 {h.pending_review} 筆疑似重複的知識等你確認 <span aria-hidden>→</span> 前往審核
        </Link>
      )}

      {step4 && (
        <p className="overview__scale">
          知識規模:{counts.documents ?? 0} 份文件 · {counts.entities ?? 0} 個知識點 ·{" "}
          {counts.relations ?? 0} 條關聯
        </p>
      )}

      <ol className="overview__steps">
        <StepRow
          n={1}
          done={step1}
          active={!step1}
          title="匯入資料"
          hint="把語料資料夾登記成來源"
          action={<Link to="../import">去匯入</Link>}
        />
        <StepRow
          n={2}
          done={step2}
          active={step1 && !step2}
          title="建置"
          hint="把資料變成可查詢的知識庫(需要幾分鐘)"
          action={<Link to="../import">去建置</Link>}
        />
        <StepRow
          n={3}
          done={step3}
          active={step2 && !step3}
          title="檢查品質(評測)"
          hint="用評測題組確認檢索品質——沒有分數的版本不能上線"
          action={
            candidate ? (
              <span className="overview__cli">
                目前評測要在終端機執行:
                <code>
                  uv run python -m cli.main eval {project} --build {candidate.id}
                </code>
              </span>
            ) : null
          }
        />
        <ActivateStep
          project={project}
          done={step4}
          active={step2 && step3 && !step4}
          candidate={step4 ? undefined : newestReady}
          writeLocked={builds.isFetching || health.isFetching}
        />
      </ol>
    </div>
  );
}

function StepRow({
  n,
  done,
  active,
  title,
  hint,
  action,
}: {
  n: number;
  done: boolean;
  active: boolean;
  title: string;
  hint: string;
  action: React.ReactNode;
}) {
  return (
    <li
      className={
        "overview__step" +
        (done ? " overview__step--done" : "") +
        (active ? " overview__step--active" : "")
      }
    >
      <span className="overview__stepmark" aria-hidden>
        {done ? "✓" : n}
      </span>
      <div className="overview__stepbody">
        <p className="overview__steptitle">
          {title}
          {done && <span className="overview__stepdone">完成</span>}
        </p>
        {!done && <p className="overview__stephint">{hint}</p>}
        {!done && active && action}
      </div>
    </li>
  );
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("zh-TW", { hour12: false });
}

// Step ④ owns the missing activate button. The write gate is born fail-closed:
// in-flight mutation OR a builds/health refetch locks it (the candidate row may
// be about to change under the click). Two-step confirm; the server's §14
// refusal renders verbatim — with the CLI eval hint when the refusal is about
// missing scores, so the message is guidance rather than a dead end.
function ActivateStep({
  project,
  done,
  active,
  candidate,
  writeLocked,
}: {
  project: string;
  done: boolean;
  active: boolean;
  candidate: Build | undefined;
  writeLocked: boolean;
}) {
  const activate = useActivateBuild(project);
  const [confirming, setConfirming] = useState(false);
  // one random key per component MOUNT: retries within this mount reuse it
  // (a lost 2xx replays instead of double-activating), and reuse after a
  // FAILED attempt is safe because the server rolls back failed activations —
  // only success responses ever enter the idempotency store
  const idempotencyKey = useMemo(() => crypto.randomUUID(), []);
  const blocked = activate.isPending || writeLocked;

  return (
    <li
      className={
        "overview__step" +
        (done ? " overview__step--done" : "") +
        (active ? " overview__step--active" : "")
      }
    >
      <span className="overview__stepmark" aria-hidden>
        {done ? "✓" : 4}
      </span>
      <div className="overview__stepbody">
        <p className="overview__steptitle">
          上線
          {done && <span className="overview__stepdone">完成</span>}
        </p>
        {!done && <p className="overview__stephint">啟用建置好的版本,所有查詢從此讀它</p>}
        {!done && active && candidate && !confirming && (
          <button type="button" onClick={() => setConfirming(true)} disabled={blocked}>
            上線這個版本
          </button>
        )}
        {!done && confirming && candidate && (
          <div className="overview__confirm" role="alertdialog" aria-label="確認上線">
            <p>上線後,所有頁面與查詢立即改讀這個版本(舊版本自動下線)。確定?</p>
            <button
              type="button"
              disabled={blocked}
              onClick={() =>
                activate.mutate(
                  { buildId: candidate.id, idempotencyKey },
                  { onSettled: () => setConfirming(false) },
                )
              }
            >
              確定上線
            </button>
            <button type="button" disabled={blocked} onClick={() => setConfirming(false)}>
              取消
            </button>
          </div>
        )}
        {activate.isPending && <p className="overview__stephint">上線中…</p>}
        {activate.isError && (
          <div className="overview__error">
            <p>
              上線失敗:
              {activate.error instanceof Error ? activate.error.message : "unknown error"}
            </p>
            {candidate && /eval|評測|score/i.test(String(activate.error?.message ?? "")) && (
              <p className="overview__cli">
                看起來還沒有評測分數——先在終端機執行:
                <code>
                  uv run python -m cli.main eval {project} --build {candidate.id}
                </code>
              </p>
            )}
          </div>
        )}
      </div>
    </li>
  );
}
