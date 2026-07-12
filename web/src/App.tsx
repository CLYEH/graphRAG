import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./components/AppShell";
import { RootRedirect } from "./components/RootRedirect";
import { Import } from "./pages/Import";
import { JobsDashboard } from "./pages/JobsDashboard";
import { NotFound } from "./pages/NotFound";
import { Playground } from "./pages/Playground";
import { ProjectHealth } from "./pages/ProjectHealth";
import { ReviewQueue } from "./pages/ReviewQueue";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<RootRedirect />} />
      <Route path="/p/:project" element={<AppShell />}>
        <Route index element={<Navigate to="health" replace />} />
        <Route path="health" element={<ProjectHealth />} />
        <Route path="import" element={<Import />} />
        <Route path="jobs" element={<JobsDashboard />} />
        <Route path="review" element={<ReviewQueue />} />
        <Route path="playground" element={<Playground />} />
        <Route path="*" element={<NotFound />} />
      </Route>
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
