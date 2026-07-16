import { NavLink, Outlet } from "react-router-dom";

import { ProjectSwitcher } from "./ProjectSwitcher";
import "./AppShell.css";

// Console areas (DESIGN §10.2), ordered by the operator's WORKFLOW (UXA3):
// 總覽 → 匯入 → 建置 → 檢視 → 清洗 → 圖譜 → 審核 → 品質 (eval, UXC2a) → 問答,
// with 診斷 (health) and 設定 (settings, UXB1) last — diagnostics and
// configuration are where you go when something needs attention, not stops on
// the happy path. Labels are zh (the P4 fix); routes stay stable.
const NAV = [
  { to: "overview", label: "總覽" },
  { to: "import", label: "匯入" },
  { to: "jobs", label: "建置" },
  { to: "inspect", label: "檢視" },
  { to: "clean", label: "清洗" },
  { to: "graph", label: "圖譜" },
  { to: "review", label: "審核" },
  { to: "quality", label: "品質" },
  { to: "playground", label: "問答" },
  { to: "health", label: "診斷" },
  { to: "settings", label: "設定" },
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
