import { useRef, useState } from "react";

import { useBuildSteps, useBuilds, useRetryBuild, useStepItems } from "../api/queries";

import type { Build } from "../api/queries";

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
                <span className="runs__step-meta">
                  {s.status} · 失敗 {s.failed_count ?? 0} · 跳過 {s.skipped_count ?? 0} · 輸入{" "}
                  {s.input_count ?? 0}
                </span>
              </button>
              {openStep === s.id && <StepItems project={project} buildId={buildId} stepId={s.id} />}
            </li>
          ))}
        </ul>
      )}

      <div className="runs__retry">
        <button
          type="button"
          className="runs__retry-btn"
          disabled={retry.isPending}
          onClick={onRetry}
        >
          {retry.isPending ? "建立重試中…" : "只重試失敗項"}
        </button>
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

// One step's recorded item outcomes (default verbosity = failed/skipped only).
// item_ref is the stable §27.7 retry key that "retry failed only" re-enters.
function StepItems({
  project,
  buildId,
  stepId,
}: {
  project: string;
  buildId: string;
  stepId: string;
}) {
  const items = useStepItems(project, buildId, stepId);
  if (items.isPending) return <p className="runs__muted">載入項目…</p>;
  if (items.isError)
    return <p className="runs__muted runs__muted--error">無法讀取項目:{message(items.error)}</p>;
  if (items.data.length === 0) return <p className="runs__muted">此步驟沒有記錄的失敗/跳過項。</p>;
  return (
    <ul className="runs__items">
      {items.data.map((it) => (
        <li key={it.id} className="runs__item">
          <span className={`runs__item-status runs__item-status--${it.status}`}>{it.status}</span>{" "}
          <span className="runs__item-ref" title={it.item_ref}>
            {it.item_kind}:{it.item_ref}
          </span>
          {it.message ? <span className="runs__item-msg"> — {it.message}</span> : null}
        </li>
      ))}
    </ul>
  );
}
