import { useState } from "react";

import { useBuilds } from "../api/queries";

import type { Build } from "../api/queries";

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
  build,
  open,
  onToggle,
}: {
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
              <div>
                <dt>metrics</dt>
                <dd>{build.metrics ? JSON.stringify(build.metrics) : "—"}</dd>
              </div>
              <div>
                <dt>eval</dt>
                <dd>{build.eval ? JSON.stringify(build.eval) : "—"}</dd>
              </div>
            </dl>
          </td>
        </tr>
      )}
    </>
  );
}
