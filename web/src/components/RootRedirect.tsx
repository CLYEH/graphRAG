import { Navigate } from "react-router-dom";

import { useProjects } from "../api/queries";
import { NewProjectForm } from "./NewProjectForm";
import { encodeProjectSegment } from "../project/projectRoute";
import { resolveLandingProject } from "../project/lastProject";

// Landing route: send the user into a project's 總覽 (UXA2 — the page that
// says what to do next), or — when none exist — show the create form here (the
// Import page lives under /p/:project and is unreachable with zero projects, so
// bootstrapping the first one must happen at the root).
//
// QA9/D17: WHICH project is the fix. This used to take `projects[0]`, and the
// list is created_at DESC, so opening the Console dropped the operator into the
// newest project — usually an empty shell someone had just created — rather
// than the one they were working in. resolveLandingProject prefers the last
// project actually opened, falling back to the list when that key is gone.
export function RootRedirect() {
  const { data: projects, isPending, isError } = useProjects();

  if (isPending) return <p className="root-status">載入中…</p>;
  if (isError) return <p className="root-status">無法連線到 API。</p>;
  const landing = resolveLandingProject(projects);
  if (landing !== undefined)
    return <Navigate to={`/p/${encodeProjectSegment(landing)}/overview`} replace />;

  return (
    <section className="root-status">
      <h1>graphRAG Console</h1>
      <p>還沒有任何專案。建立第一個專案就可以開始。</p>
      <NewProjectForm />
    </section>
  );
}
