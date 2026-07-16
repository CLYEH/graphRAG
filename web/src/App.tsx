import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./components/AppShell";
import { RootRedirect } from "./components/RootRedirect";
import { Import } from "./pages/Import";
import { Inspect } from "./pages/Inspect";
import { Clean } from "./pages/Clean";
import { Graph } from "./pages/Graph";
import { JobsDashboard } from "./pages/JobsDashboard";
import { Overview } from "./pages/Overview";
import { NotFound } from "./pages/NotFound";
import { Playground } from "./pages/Playground";
import { ProjectHealth } from "./pages/ProjectHealth";
import { Quality } from "./pages/Quality";
import { ReviewQueue } from "./pages/ReviewQueue";
import { Settings } from "./pages/Settings";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<RootRedirect />} />
      <Route path="/p/:project" element={<AppShell />}>
        {/* UXA2: land on 總覽 — the page that says what to do next; Health
            stays the diagnostics page */}
        <Route index element={<Navigate to="overview" replace />} />
        <Route path="overview" element={<Overview />} />
        <Route path="health" element={<ProjectHealth />} />
        <Route path="import" element={<Import />} />
        <Route path="inspect" element={<Inspect />} />
        <Route path="clean" element={<Clean />} />
        <Route path="graph" element={<Graph />} />
        <Route path="jobs" element={<JobsDashboard />} />
        <Route path="review" element={<ReviewQueue />} />
        <Route path="quality" element={<Quality />} />
        <Route path="playground" element={<Playground />} />
        <Route path="settings" element={<Settings />} />
        <Route path="*" element={<NotFound />} />
      </Route>
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
