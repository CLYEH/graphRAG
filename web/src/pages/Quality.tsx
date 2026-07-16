import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";

import { useBuilds, useCancelJob, useJob, useRunEval } from "../api/queries";
import { useJobStream } from "../hooks/useJobStream";
import { isPathAddressable, useActiveProject } from "../project/projectRoute";
import "./Quality.css";

import type { Build, Job } from "../api/queries";

// UXC2a (Track 4, Phase C): the 品質 page — run the golden-set eval on a chosen
// build and read the per-case verdicts, so「檢查品質」stops being a CLI hand-off.
//
// State machine, enumerated up front (class 20/25):
// - Reads: builds (B) — the ONE read this page shares with the Overview
//   checklist's step ③ (candidate.eval !== null), so an eval completing here
//   feeds that step with zero new coupling. B pending → loading line; B error
//   → LOUD error (fail closed — no run button over a world we can't see).
// - Evaluable set: status ∈ {ready, active} — the eval binding's own rule
//   (core/eval/runner.py resolve_eval_binding scores "a ready candidate or the
//   active build"); other statuses would be refused by the job, so they are
//   not offered. Empty set → guidance to build first, not an error.
// - Selection DERIVES per render from (picked id, B): a picked build that
//   vanished on a refetch falls back to the default (newest ready, else
//   active) instead of rendering a stale scope (class 17).
// - The RUN write is fail-closed: locked while the mutation is in flight,
//   while a watched eval job is non-terminal (one eval at a time on this
//   page; the server enforces one job per PROJECT with 409 JOB_CONFLICT,
//   rendered verbatim), and while builds are refetching/failed (the target
//   row may be about to change — the Overview activate discipline).
// - Idempotency-Key: one random key per (target build, logical attempt),
//   reused across retries of the SAME attempt (a lost 202 replays the
//   original job id), cleared on accept so a LATER re-run mints a fresh key
//   (the stored 202 must not replay a finished job's id as if it were live).
// - The job watch lives at PAGE level (hooks in this component, no keyed
//   child that a transitional render could unmount — class 20). Stream
//   closed → refetch the snapshot (terminal-only fields); snapshot terminal
//   → invalidate ["builds"]/["health"] ONCE per job id — that refetch is what
//   updates the per-case table AND the Overview checklist. Stream error ≠
//   job error: the job may still be running — say so honestly and offer a
//   manual refresh instead of pretending either way.
// - Per-case table: parsed from the OPEN build.eval block (the contract
//   types it as a free object). cases parse is all-or-nothing — a partial
//   table would silently drop cases (the exact false-green an eval gate must
//   not have); on any malformed entry the table is withheld with an honest
//   note and the raw JSON stays readable in the 進階 fold. Raw JSON (build
//   uuid, fingerprint hex) lives ONLY in that fold (chrome invariant).
// - Cross-project leak: QualityBody is keyed by project, so switching
//   projects remounts (fresh picked id, fresh job watch) — a lingering job id
//   from project A must never render progress over project B (class 23 R9).

// BuildStatus → operator words; keying on the contract enum makes a new value
// a type error, not a silently-english label (UXA3 — RunsTable's discipline).
const BUILD_LABEL: Record<Build["status"], string> = {
  active: "上線中",
  ready: "已就緒",
  building: "建置中",
  failed: "失敗",
  archived: "已封存",
};

// JobStatus → operator words + badge tone (the JobProgress TONE map translated:
// this page is first-class operator chrome, raw status words are P4's leak).
const JOB_LABEL: Record<Job["status"], string> = {
  queued: "排隊中",
  running: "評測中",
  done: "已完成",
  failed: "失敗",
  cancelled: "已取消",
};
const JOB_TONE: Record<Job["status"], string> = {
  queued: "info",
  running: "warn",
  done: "ok",
  failed: "bad",
  cancelled: "muted",
};
const TERMINAL: ReadonlySet<Job["status"]> = new Set(["done", "failed", "cancelled"]);

const EVALUABLE: ReadonlySet<Build["status"]> = new Set(["ready", "active"]);

function fmt(ts: string | null | undefined): string {
  return ts ? ts.replace("T", " ").replace(/\..*$/, "").replace("Z", " UTC") : "—";
}

// One golden case's verdict as persisted at builds.eval (core/eval/runner.py
// to_eval_payload) — question text is the case's stable identity (golden
// contract). The block is OPEN in the frozen contract, so this narrows at
// runtime instead of trusting a cast.
type EvalCase = { question: string; mode: string; score: number; passed: boolean };

type EvalSummary = {
  score: number | null;
  passed: number | null;
  failed: number | null;
  /** null = the block carries no readable per-case list (absent OR malformed). */
  cases: EvalCase[] | null;
};

function asFiniteNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function parseEvalBlock(block: { [key: string]: unknown }): EvalSummary {
  let cases: EvalCase[] | null = null;
  if (Array.isArray(block.cases)) {
    const parsed: EvalCase[] = [];
    let sound = true;
    for (const c of block.cases) {
      const rec = c as { [key: string]: unknown } | null;
      if (
        rec !== null &&
        typeof rec === "object" &&
        typeof rec.question === "string" &&
        typeof rec.mode === "string" &&
        asFiniteNumber(rec.score) !== null &&
        typeof rec.passed === "boolean"
      ) {
        parsed.push({
          question: rec.question,
          mode: rec.mode,
          score: rec.score as number,
          passed: rec.passed,
        });
      } else {
        // all-or-nothing: a partially-rendered verdict table silently drops
        // cases — worse than saying "unreadable" and showing the raw block
        sound = false;
        break;
      }
    }
    cases = sound ? parsed : null;
  }
  return {
    score: asFiniteNumber(block.score),
    passed: asFiniteNumber(block.passed),
    failed: asFiniteNumber(block.failed),
    cases,
  };
}

export function Quality() {
  const project = useActiveProject();

  if (project === undefined) return <p className="quality__line">Unknown project.</p>;
  if (!isPathAddressable(project))
    return (
      <p className="quality__line quality__line--error">
        Project &quot;{project}&quot; isn&apos;t addressable over the API — its key contains
        &quot;/&quot; or is &quot;.&quot; / &quot;..&quot;, which a URL path segment can&apos;t
        carry.
      </p>
    );

  return (
    <section className="quality">
      <h1 className="quality__title">品質(評測)</h1>
      {/* keyed by project: switching projects must remount (fresh selection,
          fresh job watch) — a job id from project A never renders over B */}
      <QualityBody key={project} project={project} />
    </section>
  );
}

function QualityBody({ project }: { project: string }) {
  const queryClient = useQueryClient();
  const builds = useBuilds(project);
  const runEval = useRunEval(project);
  // an entry link may name its intended target (?build=<id>): the Overview
  // step ③ CTA points at the build whose MISSING score blocks the checklist
  // (active ?? newest ready), and this page's own default (newest ready) can
  // be a DIFFERENT build when an unevaluated active build coexists with ready
  // ones (Codex #82). Read once at mount; an absent/invalid id falls through
  // the derivation below to the normal default.
  const [searchParams] = useSearchParams();
  const [pickedId, setPickedId] = useState<string | null>(() => searchParams.get("build"));
  const [jobId, setJobId] = useState<string | null>(null);

  // page-level job watch (class 20: the mutate/stream lifecycle lives in a
  // host that never unmounts while the operator is on this page)
  const jobQuery = useJob(jobId);
  const stream = useJobStream(jobId);
  const cancel = useCancelJob(jobId);
  const job = jobQuery.data;
  const refetchJob = jobQuery.refetch;

  // the JobProgress merge discipline: a terminal SNAPSHOT is authoritative
  // (the post-close refetch must not be masked by a retained live event);
  // otherwise the live event overlays the fast-moving fields
  const jobStatus: Job["status"] | undefined =
    job === undefined
      ? undefined
      : TERMINAL.has(job.status)
        ? job.status
        : (stream.event?.status ?? job.status);
  const jobProgress = stream.event ? stream.event.progress : (job?.progress ?? 0);
  const jobMessage = stream.event ? stream.event.message : (job?.message ?? null);

  // stream closed = the job reached a terminal state: refetch the snapshot to
  // pull the terminal-only fields (error, finished_at) instead of leaving the
  // stale watch-time values on screen
  useEffect(() => {
    if (stream.status === "closed") void refetchJob();
  }, [stream.status, refetchJob]);

  // terminal job → the eval report (or its absence) is now the world:
  // invalidate the builds/health reads ONCE per job. ["builds", project] is
  // the SAME read the per-case table below and the Overview checklist's step
  // ③ project from — this invalidation IS the "result feeds step ③" wire.
  // Terminal is read from EITHER surface: the snapshot (authoritative) OR the
  // live stream event — if the post-close snapshot refetch fails, react-query
  // retains the stale non-terminal snapshot forever, and a snapshot-only
  // guard would never refresh the report for a job the stream already saw
  // finish (Codex #82 triage 3). The event must name THIS job: useJobStream
  // retains the previous job's event across a jobId change until its own
  // reset effect runs, so on a re-run the first jobId=B render still sees
  // job A's terminal event — ungated, that would prematurely mark B settled
  // and B's REAL completion would be skipped by the once-per-job guard
  // (reviewer catch; mirrors the hook's own stale-frame abort guard).
  const streamTerminal =
    stream.event !== null && stream.event.job_id === jobId && TERMINAL.has(stream.event.status);
  const settledJob = useRef<string | null>(null);
  useEffect(() => {
    if (jobId === null) return;
    const snapshotTerminal = job !== undefined && TERMINAL.has(job.status);
    if (!snapshotTerminal && !streamTerminal) return;
    if (settledJob.current === jobId) return;
    settledJob.current = jobId;
    void queryClient.invalidateQueries({ queryKey: ["builds", project] });
    void queryClient.invalidateQueries({ queryKey: ["health", project] });
  }, [jobId, job, streamTerminal, project, queryClient]);

  // one cancel key per cancel intent per job (the JobProgress discipline)
  const cancelKey = useRef<{ id: string; key: string } | null>(null);
  function onCancel() {
    if (jobId === null) return;
    if (cancelKey.current === null || cancelKey.current.id !== jobId)
      cancelKey.current = { id: jobId, key: crypto.randomUUID() };
    cancel.mutate(cancelKey.current.key);
  }

  // one run key per (target build, logical attempt): reused across retries of
  // the same attempt (a lost 202 replays the original job id), cleared on
  // accept so a later re-run mints a fresh key instead of replaying the
  // stored 202 of a finished eval
  const runKey = useRef<{ build: string; key: string } | null>(null);

  if (builds.isPending) return <p className="quality__line">載入版本中…</p>;
  if (builds.isError)
    return (
      <p className="quality__line quality__line--error">
        無法載入版本:{builds.error instanceof Error ? builds.error.message : "unknown error"}
      </p>
    );

  const evaluable = builds.data.filter((b) => EVALUABLE.has(b.status));
  // default target: the newest READY by started_at (the build you would
  // activate next — the Overview's ordering rule), else the active build
  const newestReady = evaluable
    .filter((b) => b.status === "ready")
    .sort((a, b) => (b.started_at ?? "").localeCompare(a.started_at ?? ""))[0];
  const selected: Build | undefined =
    evaluable.find((b) => b.id === pickedId) ??
    newestReady ??
    evaluable.find((b) => b.status === "active");

  if (evaluable.length === 0 || selected === undefined)
    return (
      <p className="quality__line">
        還沒有可評測的版本——先完成建置。<Link to="../import">去建置</Link>
      </p>
    );

  const evalInFlight = jobId !== null && (jobStatus === undefined || !TERMINAL.has(jobStatus));
  // builds.isError is already an early return above; isFetching locks the run
  // while the target rows may be changing (the Overview activate discipline)
  const blocked = runEval.isPending || evalInFlight || builds.isFetching;

  function onRun(target: Build) {
    // PIN the target as the explicit pick: an IMPLICIT default selection
    // (pickedId null → newest ready) would re-derive on the terminal builds
    // refetch, and a newer ready build landing mid-run would silently swap
    // the results scope away from the build this job actually evaluated
    // (Codex #82). The class-17 fallback still covers a pinned build that
    // later VANISHES from the evaluable set.
    setPickedId(target.id);
    if (runKey.current === null || runKey.current.build !== target.id)
      runKey.current = { build: target.id, key: crypto.randomUUID() };
    runEval.mutate(
      { buildId: target.id, idempotencyKey: runKey.current.key },
      {
        onSuccess: (accepted) => {
          runKey.current = null;
          setJobId(accepted.job_id);
        },
      },
    );
  }

  const evalBlock = selected.eval ?? null;
  const summary = evalBlock === null ? null : parseEvalBlock(evalBlock);
  const jobTarget =
    job?.build_id != null ? builds.data.find((b) => b.id === job.build_id) : undefined;

  return (
    <div className="quality__body">
      <p className="quality__hint">用評測題組檢驗一個版本的檢索品質——沒有分數的版本不能上線。</p>

      <div className="quality__run">
        <label className="quality__pick">
          選擇版本
          <select
            value={selected.id}
            onChange={(e) => setPickedId(e.target.value)}
            disabled={blocked}
          >
            {evaluable.map((b) => (
              <option key={b.id} value={b.id}>
                {fmt(b.started_at)} 版({BUILD_LABEL[b.status]}
                {b.eval !== null ? "・已有分數" : ""})
              </option>
            ))}
          </select>
        </label>
        <button type="button" onClick={() => onRun(selected)} disabled={blocked}>
          {runEval.isPending ? "送出中…" : "開始評測"}
        </button>
      </div>
      {runEval.isError && (
        <p className="quality__line quality__line--error">
          無法開始評測:
          {runEval.error instanceof Error ? runEval.error.message : "unknown error"}
        </p>
      )}

      {jobId !== null && jobQuery.isError && (
        // the job snapshot failed to load (or to REFETCH — stale data may
        // still render below): the run gate may be fail-closed on an unknown
        // job state and the terminal-only fields (job.error) may be missing,
        // but a broken read must never be silent — say what broke and offer
        // the retry (Rule 12; Codex #82 triage 3 widened this from the
        // no-data case to any snapshot error)
        <p className="quality__line quality__line--error">
          無法載入評測工作狀態:
          {jobQuery.error instanceof Error ? jobQuery.error.message : "unknown error"}
          <button type="button" onClick={() => void refetchJob()}>
            重新整理狀態
          </button>
        </p>
      )}

      {jobId !== null && job !== undefined && jobStatus !== undefined && (
        <div className="quality__job">
          <div className="quality__jobhead">
            <span className={`runs__badge runs__badge--${JOB_TONE[jobStatus]}`} role="status">
              {JOB_LABEL[jobStatus]}
            </span>
            <span className="quality__jobtarget" title={job.build_id ?? undefined}>
              評測版本:{jobTarget ? `${fmt(jobTarget.started_at)} 版` : "—"}
            </span>
            <button
              type="button"
              onClick={onCancel}
              disabled={!(jobStatus === "queued" || jobStatus === "running") || cancel.isPending}
            >
              {cancel.isPending ? "取消中…" : "取消評測"}
            </button>
          </div>
          <progress className="quality__bar" max={1} value={jobProgress} />
          <span className="quality__pct">{Math.round(jobProgress * 100)}%</span>
          {jobMessage && <p className="quality__jobmsg">{jobMessage}</p>}
          {job.error && (
            <p className="quality__line quality__line--error">評測失敗:{job.error.message}</p>
          )}
          {cancel.isError && (
            <p className="quality__line quality__line--error">
              取消失敗:{cancel.error instanceof Error ? cancel.error.message : "unknown error"}
            </p>
          )}
          {stream.status === "error" && (
            <p className="quality__line quality__line--warn">
              即時進度中斷(評測可能仍在執行)
              {stream.error ? `:${stream.error}` : ""}
              <button type="button" onClick={() => void refetchJob()}>
                重新整理狀態
              </button>
            </p>
          )}
        </div>
      )}

      <h2>評測結果</h2>
      <p className="quality__scope" title={selected.id}>
        版本:{fmt(selected.started_at)} 版({BUILD_LABEL[selected.status]})
      </p>
      {summary === null ? (
        <p className="quality__line">此版本還沒有評測結果。</p>
      ) : (
        <>
          <p className="quality__summary">
            總分:{summary.score !== null ? summary.score.toFixed(2) : "—"}
            {summary.passed !== null && ` · 通過 ${summary.passed} 題`}
            {summary.failed !== null && ` · 未過 ${summary.failed} 題`}
          </p>
          {summary.cases === null ? (
            <p className="quality__line quality__line--warn">
              無法解讀逐題結果(格式不符)——原始資料見下方進階。
            </p>
          ) : (
            <table className="quality__cases">
              <thead>
                <tr>
                  <th>題目</th>
                  <th>模式</th>
                  <th>分數</th>
                  <th>結果</th>
                </tr>
              </thead>
              <tbody>
                {summary.cases.map((c, i) => (
                  <tr key={`${i}:${c.question}`}>
                    <td className="quality__question">{c.question}</td>
                    <td>{c.mode}</td>
                    <td>{c.score.toFixed(2)}</td>
                    <td>
                      <span className={`runs__badge runs__badge--${c.passed ? "ok" : "bad"}`}>
                        {c.passed ? "通過" : "未過"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <details className="quality__advanced">
            <summary>進階:原始評測資料(JSON)</summary>
            <pre>{JSON.stringify(evalBlock, null, 2)}</pre>
          </details>
        </>
      )}
    </div>
  );
}
