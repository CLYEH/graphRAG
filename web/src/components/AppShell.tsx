import { NavLink, Outlet } from "react-router-dom";

import { ProjectSwitcher } from "./ProjectSwitcher";
import "./AppShell.css";

// Console areas (DESIGN §10.2 note): health / import / jobs / review / playground.
// Clean, inspect and graph-explorer are the remaining v2 pages and get no nav yet.
const NAV = [
  { to: "health", label: "Health" },
  { to: "import", label: "Import" },
  { to: "inspect", label: "Inspect" },
  { to: "jobs", label: "Jobs" },
  { to: "review", label: "Review" },
  { to: "playground", label: "Playground" },
];

export function AppShell() {
  return (
    <div className="shell">
      <header className="shell__header">
        <div className="shell__brand">graphRAG Console</div>
        <ProjectSwitcher />
      </header>
      <nav className="shell__nav" aria-label="Sections">
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) =>
              isActive ? "shell__navlink shell__navlink--active" : "shell__navlink"
            }
          >
            {item.label}
          </NavLink>
        ))}
      </nav>
      <main className="shell__main">
        <Outlet />
      </main>
    </div>
  );
}
