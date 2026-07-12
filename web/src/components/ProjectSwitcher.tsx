import { useNavigate, useParams } from "react-router-dom";

import { useProjects } from "../api/queries";

// The active project is the `:project` route segment; switching navigates to
// the chosen project (the layout index redirects on to its default section).
export function ProjectSwitcher() {
  const { project } = useParams<{ project: string }>();
  const navigate = useNavigate();
  const { data: projects, isPending, isError } = useProjects();

  if (isPending) return <span className="switcher switcher--muted">Loading projects…</span>;
  if (isError) return <span className="switcher switcher--error">Projects unavailable</span>;
  if (!projects || projects.length === 0)
    return <span className="switcher switcher--muted">No projects</span>;

  return (
    <label className="switcher">
      <span className="switcher__label">Project</span>
      <select
        className="switcher__select"
        value={project ?? ""}
        onChange={(e) => navigate(`/p/${e.target.value}`)}
      >
        {projects.map((p) => (
          <option key={p.name} value={p.name}>
            {p.display_name ?? p.name}
          </option>
        ))}
      </select>
    </label>
  );
}
