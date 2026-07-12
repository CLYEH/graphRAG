import { Navigate } from "react-router-dom";

import { useProjects } from "../api/queries";
import { encodeProjectSegment } from "../project/projectRoute";

// Landing route: send the user into the first project's health page, or show
// an empty state when none exist (project-creation UI lands in a later task).
export function RootRedirect() {
  const { data: projects, isPending, isError } = useProjects();

  if (isPending) return <p className="root-status">Loading…</p>;
  if (isError) return <p className="root-status">Could not reach the API.</p>;
  if (projects && projects.length > 0)
    return <Navigate to={`/p/${encodeProjectSegment(projects[0].name)}/health`} replace />;

  return (
    <section className="root-status">
      <h1>graphRAG Console</h1>
      <p>No projects yet. Create one via the API — project management UI lands later.</p>
    </section>
  );
}
