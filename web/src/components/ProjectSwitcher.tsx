import { useNavigate } from "react-router-dom";

import { useProjects } from "../api/queries";
import { encodeProjectSegment, useActiveProject } from "../project/projectRoute";

// The active project is the decoded `:project` route segment; switching encodes
// the chosen key back into the URL (the layout index redirects to its section).
export function ProjectSwitcher() {
  const active = useActiveProject();
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
        value={active ?? ""}
        onChange={(e) => navigate(`/p/${encodeProjectSegment(e.target.value)}`)}
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
