import { useHealth } from "../api/queries";
import { isPathAddressable, useActiveProject } from "../project/projectRoute";
import "./ProjectHealth.css";

import type { HealthReport } from "../api/queries";

// The five §19 status lights. Keying the map on HealthReport["status"] makes a
// new contract enum value a type error here rather than a silent grey badge.
const STATUS: Record<HealthReport["status"], { label: string; tone: string }> = {
  healthy: { label: "Healthy", tone: "ok" },
  needs_review: { label: "Needs review", tone: "warn" },
  build_failed: { label: "Build failed", tone: "bad" },
  index_drift: { label: "Index drift", tone: "warn" },
  eval_regression: { label: "Eval regression", tone: "bad" },
};

export function ProjectHealth() {
  const project = useActiveProject();
  const { data, isPending, isError, error } = useHealth(project);

  // A route segment that doesn't decode is an unknown project, not a spinner.
  if (project === undefined) return <Status text="Unknown project." />;
  // Some keys open in the route but can't be a REST path segment ("/"-bearing,
  // or "." / ".."); say so rather than fire a request that 404s or normalizes to
  // the wrong endpoint.
  if (!isPathAddressable(project))
    return (
      <Status
        text={`Project "${project}" isn't addressable over the API — its key contains "/" or is "." / "..", which a URL path segment can't carry.`}
        error
      />
    );
  if (isPending) return <Status text="Loading health…" />;
  if (isError) {
    const message = error instanceof Error ? error.message : "unknown error";
    return <Status text={`Could not load project health: ${message}`} error />;
  }

  return <HealthView report={data} />;
}

function Status({ text, error = false }: { text: string; error?: boolean }) {
  return (
    <section className="health">
      <p className={error ? "health__line health__line--error" : "health__line"}>{text}</p>
    </section>
  );
}

function HealthView({ report }: { report: HealthReport }) {
  const light = STATUS[report.status];
  const counts = Object.entries(report.counts ?? {});
  const drift = Object.entries(report.drift ?? {});
  const warnings = report.warnings ?? [];

  return (
    <section className="health">
      <h1 className="health__title">Project health</h1>
      <div className={`health__badge health__badge--${light.tone}`} role="status">
        {light.label}
      </div>

      <dl className="health__facts">
        <div>
          <dt>Active build</dt>
          <dd>{report.active_build_id ?? "—"}</dd>
        </div>
        <div>
          <dt>Pending review</dt>
          <dd>{report.pending_review ?? 0}</dd>
        </div>
      </dl>

      <h2>Counts</h2>
      {counts.length > 0 ? (
        <dl className="health__grid">
          {counts.map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="health__muted">No counts reported.</p>
      )}

      <h2>Projection drift</h2>
      {drift.length > 0 ? (
        <dl className="health__grid">
          {drift.map(([store, detail]) => (
            <div key={store}>
              <dt>{store}</dt>
              <dd>{JSON.stringify(detail)}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="health__muted">No drift detected.</p>
      )}

      {warnings.length > 0 && (
        <>
          <h2>Warnings</h2>
          <ul className="health__warnings">
            {warnings.map((w, i) => (
              <li key={`${w.code}-${i}`}>
                <code>{w.code}</code> {w.message}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
