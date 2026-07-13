import { NavLink, Outlet } from "react-router-dom";

import { ProjectSwitcher } from "./ProjectSwitcher";
import "./AppShell.css";

// Console areas (DESIGN §10.2): health / import / clean / inspect / graph / jobs /
// review / playground — the full v1+v2 surface.
const NAV = [
  { to: "health", label: "Health" },
  { to: "import", label: "Import" },
  { to: "clean", label: "Clean" },
  { to: "inspect", label: "Inspect" },
  { to: "graph", label: "Graph" },
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
