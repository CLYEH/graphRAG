import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useProjects } from "../api/queries";
import { encodeProjectSegment, useActiveProject } from "../project/projectRoute";
import { writeLastProject } from "../project/lastProject";
import { switcherView } from "../project/switcherView";

// The active project is the decoded `:project` route segment; switching encodes
// the chosen key back into the URL (the layout index redirects to its section).
//
// QA9/D17: this was a bare <select> whose comment assumed "project counts are
// small" — an assumption that stops holding exactly when finding a project
// matters most, because a native select cannot be searched and its type-ahead
// only matches a PREFIX of the label. What to show is one decision, taken in
// `switcherView` (see there for why the threshold gates the filter's use and
// not just its rendering).
export function ProjectSwitcher() {
  const active = useActiveProject();
  const navigate = useNavigate();
  const { data: projects, isPending, isError } = useProjects();
  const [filter, setFilter] = useState("");

  // Remember what the operator actually opened, so the ROOT can return here
  // next time instead of the newest project. Recorded from the URL rather than
  // from this component's onChange, so arriving by link, bookmark or
  // back-button counts too — those are the paths an onChange hook would miss.
  //
  // Only once the project is known to EXIST (Codex #148 P2). A route segment
  // being decodable says nothing about the project being real, and recording a
  // dead one overwrote a perfectly good preference: following a stale bookmark
  // to a deleted project meant the next visit to `/` rejected the now-unknown
  // key and fell back to projects[0] — reinstating the newest-project landing
  // this task exists to remove. `projects` is undefined while loading, so this
  // simply waits for the list rather than guessing.
  useEffect(() => {
    if (active !== undefined && projects?.some((p) => p.name === active)) writeLastProject(active);
  }, [active, projects]);

  const view = useMemo(() => switcherView(projects, filter, active), [projects, filter, active]);

  if (isPending) return <span className="switcher switcher--muted">載入專案中…</span>;
  if (isError) return <span className="switcher switcher--error">無法載入專案</span>;
  if (!projects || projects.length === 0)
    return <span className="switcher switcher--muted">沒有專案</span>;

  return (
    <div className="switcher">
      {view.filtering && (
        <label className="switcher__filter">
          <span className="switcher__label">搜尋</span>
          <input
            className="switcher__search"
            type="search"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="篩選專案"
          />
        </label>
      )}
      <label>
        <span className="switcher__label">專案</span>
        <select
          className="switcher__select"
          value={active ?? ""}
          onChange={(e) => navigate(`/p/${encodeProjectSegment(e.target.value)}`)}
        >
          {view.options.map((p) => (
            <option key={p.name} value={p.name}>
              {p.display_name ?? p.name}
            </option>
          ))}
        </select>
      </label>
      {/* Rendered UNCONDITIONALLY with its content swapped, not mounted with
          it: a live region that does not already exist in the accessibility
          tree when its text appears is unreliably announced (VoiceOver in
          particular). Without it, a screen-reader user typing in the filter
          gets no feedback at all — neither this message nor the changed
          option set.

          aria-live rather than role="status": an always-mounted role="status"
          is a SECOND status node in every Console page, and the health page
          already owns one (`getByRole("status")` there became ambiguous).
          aria-live carries no implicit role, so the region announces without
          competing for that identity. */}
      <span className="switcher__empty" aria-live="polite">
        {view.noMatch ? "沒有符合的專案" : ""}
      </span>
    </div>
  );
}
