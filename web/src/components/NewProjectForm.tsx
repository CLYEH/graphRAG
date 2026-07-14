import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { useCreateProject } from "../api/queries";
import { encodeProjectSegment, isPathAddressable } from "../project/projectRoute";
import "./NewProjectForm.css";

// Creates a project and drops the operator into it (its 總覽 page — the setup
// checklist is exactly what a fresh project needs, UXA2). `name` is required
// (the projects primary key); display name and description are optional and are
// omitted when blank. Fails loud — a name clash or store outage shows its
// message rather than a silent no-op. Shared by the root empty-state (bootstrap
// the first project, since /p/:project is unreachable with none) and the Import
// page (create another). The target must AGREE with RootRedirect's own
// projects-became-nonempty redirect — otherwise, from the root, the create's
// invalidateQueries(["projects"]) re-renders the still-mounted RootRedirect
// into a competing <Navigate> that races and clobbers this one. Uses the
// returned canonical name, base64url-encoded so any key round-trips into the URL.
export function NewProjectForm() {
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const navigate = useNavigate();
  const create = useCreateProject();

  // Block a name the rest of the console can't address ("/"-bearing, or "."/"..") —
  // creating one would strand the operator on an unusable project the moment the
  // switcher/pages try to load it (Codex #70). The route encodes any key, but a
  // REST path segment can't carry these; refuse them at the source.
  const trimmed = name.trim();
  const unaddressable = trimmed !== "" && !isPathAddressable(trimmed);
  const canCreate = trimmed !== "" && !unaddressable && !create.isPending;

  function submit() {
    create.mutate(
      { name: name.trim(), displayName, description },
      { onSuccess: (project) => navigate(`/p/${encodeProjectSegment(project.name)}/overview`) },
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
      {unaddressable && (
        <p className="npf__error">
          Name can&apos;t contain &quot;/&quot; or be &quot;.&quot; / &quot;..&quot; — the console
          addresses each project by this key in the URL.
        </p>
      )}
      {create.isError && (
        <p className="npf__error">
          Create failed: {create.error instanceof Error ? create.error.message : "unknown error"}
        </p>
      )}
    </form>
  );
}
