import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./components/AppShell";
import { RootRedirect } from "./components/RootRedirect";
import { NotFound } from "./pages/NotFound";
import { Placeholder } from "./pages/Placeholder";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<RootRedirect />} />
      <Route path="/p/:project" element={<AppShell />}>
        <Route index element={<Navigate to="health" replace />} />
        <Route path="health" element={<Placeholder title="Project Health" task="FE7" />} />
        <Route path="jobs" element={<Placeholder title="Jobs & Pipeline" task="FE8" />} />
        <Route path="review" element={<Placeholder title="Entity Review" task="FE5" />} />
        <Route path="playground" element={<Placeholder title="Query Playground" task="FE6" />} />
        <Route path="*" element={<NotFound />} />
      </Route>
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
