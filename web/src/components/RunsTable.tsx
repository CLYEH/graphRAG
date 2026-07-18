import { useRef, useState } from "react";

import { useBuildSteps, useBuilds, useRetryBuild, useStepItems } from "../api/queries";

import type { Build, ItemDiagnosisStatus } from "../api/queries";

function message(error: unknown): string {
  return error instanceof Error ? error.message : "unknown error";
}

// BuildStatus (DESIGN §4/§14) → badge tone shared with the health status light.
const TONE: Record<Build["status"], string> = {
  active: "ok",
  ready: "info",
  building: "warn",
  failed: "bad",
  archived: "muted",
};

// Build["status"] → operator words; keying on the contract enum makes a new
// value a type error, not a silently-english badge (UXA3).
const STATUS_LABEL: Record<Build["status"], string> = {
  active: "上線中",
  ready: "已就緒",
  building: "建置中",
  failed: "失敗",
  archived: "已封存",
};

function fmt(ts: string | null | undefined): string {
  return ts ? ts.replace("T", " ").replace(/\..*$/, "").replace("Z", " UTC") : "—";
}

export function RunsTable({ project }: { project: string }) {
  const { data: builds, isPending, isError, error } = useBuilds(project);
  const [open, setOpen] = useState<string | null>(null);

  if (isPending) return <p className="runs__muted">Loading runs…</p>;
  if (isError)
    return (
      <p className="runs__muted runs__muted--error">
        Could not load runs: {error instanceof Error ? error.message : "unknown error"}
      </p>
    );
  if (builds.length === 0) return <p className="runs__muted">No builds yet.</p>;

  return (
    <table className="runs">
      <thead>
        <tr>
          <th>版本</th>
          <th>狀態</th>
          <th>開始</th>
          <th>完成</th>
        </tr>
      </thead>
      <tbody>
        {builds.map((b) => (
          <BuildRow
            key={b.id}
            project={project}
            build={b}
            open={open === b.id}
            onToggle={() => setOpen(open === b.id ? null : b.id)}
          />
        ))}
      </tbody>
    </table>
  );
}

function BuildRow({
  project,
  build,
  open,
  onToggle,
}: {
  project: string;
  build: Build;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <tr className="runs__row" onClick={onToggle} aria-expanded={open}>
        {/* words on the surface, uuid on hover: the start time names the
            version for humans; the full id survives in the title attribute and
            the expanded detail (UXA3 translation layer) */}
        <td className="runs__id" title={build.id}>
          {fmt(build.started_at)} 版
        </td>
        <td>
          <span className={`runs__badge runs__badge--${TONE[build.status]}`}>
            {STATUS_LABEL[build.status]}
          </span>
        </td>
        <td>{fmt(build.started_at)}</td>
        <td>{fmt(build.finished_at)}</td>
      </tr>
      {open && (
        <tr className="runs__detail">
          <td colSpan={4}>
            <dl>
              <div>
                <dt>build id</dt>
                <dd>{build.id}</dd>
              </div>
              <div>
                <dt>activated</dt>
                <dd>{fmt(build.activated_at)}</dd>
              </div>
              <div>
                <dt>config hash</dt>
                <dd>{build.config_hash ?? "—"}</dd>
              </div>
              <div>
                <dt>source hash</dt>
                <dd>{build.source_hash ?? "—"}</dd>
              </div>
              {/* RB1 lineage: a retry build points back at the build it retried;
                  the parent's terminal record is immutable (audit integrity) */}
              {build.parent_build_id && (
                <div>
                  <dt>重試自</dt>
                  <dd>{build.parent_build_id}</dd>
                </div>
              )}
              <div>
                <dt>metrics</dt>
                <dd>{build.metrics ? JSON.stringify(build.metrics) : "—"}</dd>
              </div>
              <div>
                <dt>eval</dt>
                <dd>{build.eval ? JSON.stringify(build.eval) : "—"}</dd>
              </div>
            </dl>
            {/* RB1-fe: a failed build gets its failure diagnosis (step/item
                drill-down) + a safe "retry failed only" action */}
            {build.status === "failed" && <FailureRecovery project={project} buildId={build.id} />}
          </td>
        </tr>
      )}
    </>
  );
}

// RB1-fe failure recovery: WHERE the build failed (its §27.7 steps, each
// expandable to the failed/skipped items), plus a safe "retry failed only"
// action. Read-only until the operator clicks retry — which opens a NEW child
// build (parent_build_id lineage), never mutates this terminal record.
function FailureRecovery({ project, buildId }: { project: string; buildId: string }) {
  const steps = useBuildSteps(project, buildId);
  const retry = useRetryBuild(project);
  // one Idempotency-Key per retry INTENT for this build (the trigger/cancel
  // discipline): reused across a lost-202 replay so it can't fork a second
  // child; minted on first click.
  const retryKey = useRef<string | null>(null);
  const [openStep, setOpenStep] = useState<string | null>(null);

  const onRetry = () => {
    retryKey.current ??= crypto.randomUUID();
    retry.mutate(
      { buildId, idempotencyKey: retryKey.current },
      // RESET the key on SUCCESS so a deliberate re-retry of the SAME parent
      // mints a fresh child (else it would replay the now-terminal first job and
      // no new build would appear); RETAIN it on FAILURE so a lost-202 replay
      // reuses it — no forked second child. The trigger/eval key lifecycle.
      { onSuccess: () => (retryKey.current = null) },
    );
  };

  return (
    <div className="runs__recovery">
      <h4 className="runs__recovery-title">失敗診斷</h4>
      {steps.isPending ? (
        <p className="runs__muted">載入步驟…</p>
      ) : steps.isError ? (
        <p className="runs__muted runs__muted--error">無法讀取步驟:{message(steps.error)}</p>
      ) : steps.data.length === 0 ? (
        <p className="runs__muted">此建置沒有記錄的步驟。</p>
      ) : (
        <ul className="runs__steps">
          {steps.data.map((s) => (
            <li key={s.id}>
              <button
                type="button"
                className="runs__step"
                aria-expanded={openStep === s.id}
                onClick={() => setOpenStep(openStep === s.id ? null : s.id)}
              >
                <span className="runs__step-name">{s.step_name}</span>
                {/* BuildStep counts are nullable: null = the step never ran /
                    unmeasured, which the contract distinguishes from a measured
                    0 — render "—" so an unobserved count never reads as a real
                    zero and misleads the diagnosis (Codex #102) */}
                <span className="runs__step-meta">
                  {s.status} · 失敗 {s.failed_count ?? "—"} · 跳過 {s.skipped_count ?? "—"} · 輸入{" "}
                  {s.input_count ?? "—"}
                </span>
              </button>
              {openStep === s.id && (
                <StepItems
                  project={project}
                  buildId={buildId}
                  stepId={s.id}
                  failedCount={s.failed_count}
                  skippedCount={s.skipped_count}
                />
              )}
            </li>
          ))}
        </ul>
      )}

      {/* The drill-down shows per-item outcomes only. The AUTHORITATIVE cause is
          at the run level (pipeline_runs.error) and NOT exposed here — and it can
          hide behind item failures: an earlier stage can tolerate failed items
          (recorded as a 'failed' step) and a LATER stage still crash (recorded on
          the run, never as a step). So this pointer is UNCONDITIONAL — never
          gated on "no failed step", which would suppress the real later crash
          behind the earlier item failure (Codex #102 R3/R4). Surfacing the run
          error itself is a backend follow-up. */}
      {steps.isSuccess && (
        <p className="runs__muted">
          下鑽為各步驟的逐項結果;此建置的確切失敗原因(項目失敗超過門檻,或某階段崩潰)記於 pipeline
          run 層級——可於下方「追蹤工作」以該次建置的 job id 查看。
        </p>
      )}

      <div className="runs__retry">
        <button
          type="button"
          className="runs__retry-btn"
          disabled={retry.isPending}
          onClick={onRetry}
        >
          {retry.isPending ? "建立重試中…" : "重試此建置"}
        </button>
        {/* HONEST label: the retry endpoint currently opens a new build that
            reuses the ingested corpus but RE-RUNS every downstream stage — it is
            NOT yet selective (the per-item "only failed" compute-skip is the
            deferred RB1-retry-skip slice), so a "只重試失敗項" label would promise
            selectivity it doesn't deliver and hide the re-run cost (Codex #102). */}
        <p className="runs__muted">
          開新建置重試此語料;目前會重跑成功項(逐項只重試失敗項為後續優化)。
        </p>
        {retry.isError && (
          <p className="runs__muted runs__muted--error">重試失敗:{message(retry.error)}</p>
        )}
        {retry.isSuccess && (
          <p className="runs__muted runs__muted--ok">
            已建立重試工作 {retry.data.job_id}——於下方「追蹤工作」貼上此 id
            觀察進度;新建置會在列表出現。
          </p>
        )}
      </div>
    </div>
  );
}

// The diagnosis outcomes this drill-down offers, with the operator words for
// each. Keyed on ItemDiagnosisStatus so adding an outcome is a type error, not a
// silently-missing tab (the UXA3 translation-layer discipline).
const ITEM_STATUS_LABEL: Record<ItemDiagnosisStatus, string> = {
  failed: "失敗",
  skipped: "跳過",
};

// One step's recorded item outcomes. The list is ALWAYS status-filtered (see
// useStepItems): under `sampled`/`all` verbosity the recorder also persists
// successes, ordered by id, so an unfiltered page could bury the failures this
// view exists to show. A 失敗/跳過 selector switches which diagnosis status is
// fetched — the "separate strategy for skipped items" (Codex #102). item_ref is
// the stable §27.7 retry key that "retry failed only" re-enters.
function StepItems({
  project,
  buildId,
  stepId,
  failedCount,
  skippedCount,
}: {
  project: string;
  buildId: string;
  stepId: string;
  failedCount: number | null | undefined;
  skippedCount: number | null | undefined;
}) {
  // default to 失敗 — the actionable "why it failed" this view is opened for
  const [status, setStatus] = useState<ItemDiagnosisStatus>("failed");
  const items = useStepItems(project, buildId, stepId, status);
  // nullish = unmeasured (never ran), which the contract distinguishes from a
  // real 0 — render "—" on the tab so it can't read as a measured empty (Codex #102)
  const count: Record<ItemDiagnosisStatus, number | null | undefined> = {
    failed: failedCount,
    skipped: skippedCount,
  };
  const rows = items.isSuccess ? items.data.pages.flatMap((p) => p.rows) : [];
  return (
    <div className="runs__item-panel">
      <div className="runs__item-tabs" role="group" aria-label="項目狀態篩選">
        {(["failed", "skipped"] as ItemDiagnosisStatus[]).map((s) => (
          <button
            key={s}
            type="button"
            className={`runs__item-tab${status === s ? " runs__item-tab--on" : ""}`}
            aria-pressed={status === s}
            onClick={() => setStatus(s)}
          >
            {ITEM_STATUS_LABEL[s]} {count[s] ?? "—"}
          </button>
        ))}
      </div>
      {items.isPending ? (
        <p className="runs__muted">載入項目…</p>
      ) : items.isError ? (
        <p className="runs__muted runs__muted--error">無法讀取項目:{message(items.error)}</p>
      ) : rows.length === 0 ? (
        <p className="runs__muted">此步驟沒有記錄的{ITEM_STATUS_LABEL[status]}項。</p>
      ) : (
        <>
          <ul className="runs__items">
            {rows.map((it) => {
              // the reason may ride EITHER the optional message OR the structured
              // `error` object (both frozen on BuildStepItem) — prefer the
              // message, but fall back to the error rather than discarding the
              // only "why" the operator has (Codex #102 R2).
              const detail = it.message ?? (it.error ? JSON.stringify(it.error) : null);
              return (
                <li key={it.id} className="runs__item">
                  <span className={`runs__item-status runs__item-status--${it.status}`}>
                    {it.status}
                  </span>{" "}
                  <span className="runs__item-ref" title={it.item_ref}>
                    {it.item_kind}:{it.item_ref}
                  </span>
                  {detail ? <span className="runs__item-msg"> — {detail}</span> : null}
                </li>
              );
            })}
          </ul>
          {items.hasNextPage && (
            <button
              type="button"
              className="runs__more"
              disabled={items.isFetchingNextPage}
              onClick={() => items.fetchNextPage()}
            >
              {items.isFetchingNextPage ? "載入中…" : "載入更多項目"}
            </button>
          )}
        </>
      )}
    </div>
  );
}
