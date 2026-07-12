import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { useCreateProject } from "../api/queries";
import { encodeProjectSegment } from "../project/projectRoute";
import "./NewProjectForm.css";

// Creates a project and drops the operator into it (its health page — the nav
// then leads to Import). `name` is required (the projects primary key); display
// name and description are optional and are omitted when blank. Fails loud — a
// name clash or store outage shows its message rather than a silent no-op. Shared
// by the root empty-state (bootstrap the first project, since /p/:project is
// unreachable with none) and the Import page (create another). Navigates to health
// rather than import so it agrees with RootRedirect's own projects-became-nonempty
// redirect — otherwise, from the root, the create's invalidateQueries(["projects"])
// re-renders the still-mounted RootRedirect into a competing <Navigate> that races
// and clobbers this one. Uses the returned canonical name, base64url-encoded so any
// key round-trips into the URL.
export function NewProjectForm() {
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const navigate = useNavigate();
  const create = useCreateProject();

  const canCreate = name.trim() !== "" && !create.isPending;

  function submit() {
    create.mutate(
      { name: name.trim(), displayName, description },
      { onSuccess: (project) => navigate(`/p/${encodeProjectSegment(project.name)}/health`) },
    );
  }

  return (
    <form
      className="npf__form"
      onSubmit={(e) => {
        e.preventDefault();
        if (canCreate) submit();
      }}
    >
      <label className="npf__field">
        name
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="acme-corpus" />
      </label>
      <label className="npf__field">
        display name
        <input
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder="optional"
        />
      </label>
      <label className="npf__field npf__field--wide">
        description
        <input
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="optional"
        />
      </label>
      <button type="submit" disabled={!canCreate}>
        {create.isPending ? "Creating…" : "Create project"}
      </button>
      {create.isError && (
        <p className="npf__error">
          Create failed: {create.error instanceof Error ? create.error.message : "unknown error"}
        </p>
      )}
    </form>
  );
}
