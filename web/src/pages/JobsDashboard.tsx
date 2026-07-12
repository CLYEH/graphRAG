import { JobWatcher } from "../components/JobWatcher";
import { RunsTable } from "../components/RunsTable";
import { isPathAddressable, useActiveProject } from "../project/projectRoute";
import "./JobsDashboard.css";

// FE8 Pipeline dashboard (DESIGN §19): build/run history from the builds list
// plus a live single-job progress panel (SSE). The frozen contract has no jobs
// list, so runs are shown via builds and a job is watched by its CLI-returned id.
export function JobsDashboard() {
  const project = useActiveProject();

  if (project === undefined) return <p className="pipeline__line">Unknown project.</p>;
  if (!isPathAddressable(project))
    return (
      <p className="pipeline__line pipeline__line--error">
        Project &quot;{project}&quot; isn&apos;t addressable over the API — its key contains
        &quot;/&quot; or is &quot;.&quot; / &quot;..&quot;, which a URL path segment can&apos;t
        carry.
      </p>
    );

  return (
    <section className="pipeline">
      <h1 className="pipeline__title">Pipeline</h1>

      <h2>Runs</h2>
      <RunsTable project={project} />

      <h2>Watch a job</h2>
      <JobWatcher />
    </section>
  );
}
