import { Navigate } from "react-router-dom";

import { useProjects } from "../api/queries";
import { NewProjectForm } from "./NewProjectForm";
import { encodeProjectSegment } from "../project/projectRoute";

// Landing route: send the user into the first project's 總覽 (UXA2 — the page
// that says what to do next), or — when none exist — show the create form here
// (the Import page lives under /p/:project and is unreachable with zero
// projects, so bootstrapping the first one must happen at the root).
export function RootRedirect() {
  const { data: projects, isPending, isError } = useProjects();

  if (isPending) return <p className="root-status">Loading…</p>;
  if (isError) return <p className="root-status">Could not reach the API.</p>;
  if (projects && projects.length > 0)
    return <Navigate to={`/p/${encodeProjectSegment(projects[0].name)}/overview`} replace />;

  return (
    <section className="root-status">
      <h1>graphRAG Console</h1>
      <p>No projects yet. Create your first project to get started.</p>
      <NewProjectForm />
    </section>
  );
}
