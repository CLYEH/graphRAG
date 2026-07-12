import { fireEvent, screen } from "@testing-library/react";
import { Route, Routes, useParams } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NewProjectForm } from "./NewProjectForm";
import { decodeProjectSegment } from "../project/projectRoute";
import { project, renderWithProviders, stubPost, stubPostError } from "../test-utils";

afterEach(() => {
  vi.restoreAllMocks();
});

// The destination echoes the decoded :project param, so a successful create's
// navigation is observable — proving both that it fired and that it landed in the
// new project (its health page, which agrees with RootRedirect's own redirect).
function HealthEcho() {
  const { project } = useParams();
  return <div>health for {project ? decodeProjectSegment(project) : ""}</div>;
}

function renderForm(route = "/") {
  return renderWithProviders(
    <Routes>
      <Route path="/" element={<NewProjectForm />} />
      <Route path="/p/:project/health" element={<HealthEcho />} />
    </Routes>,
    { route },
  );
}

describe("NewProjectForm", () => {
  it("creates a project and lands in it, sending no client idempotency key", async () => {
    const post = stubPost(project("acme", "Acme Inc"));
    renderForm();

    fireEvent.change(screen.getByLabelText("name"), { target: { value: "acme" } });
    fireEvent.change(screen.getByLabelText(/display name/i), { target: { value: "Acme Inc" } });
    fireEvent.click(screen.getByRole("button", { name: /create project/i }));

    // navigation lands in the created project (health, matching RootRedirect)
    expect(await screen.findByText(/health for acme/i)).toBeInTheDocument();
    // no Idempotency-Key: `name` allows unicode / >255 chars, which isn't a valid
    // HTTP header value — keying on it would break exactly the names the contract
    // permits; the projects PK dedups a retry instead (Codex #70)
    const [path, init] = post.mock.calls[0] as [string, { params?: unknown; body: unknown }];
    expect(path).toBe("/projects");
    expect(init.body).toEqual({ name: "acme", display_name: "Acme Inc" });
    expect(init.params).toBeUndefined();
  });

  it("cannot submit without a name (the required primary key)", () => {
    stubPost(project("acme"));
    renderForm();
    expect(screen.getByRole("button", { name: /create project/i })).toBeDisabled();
  });

  it("blocks a name the console can't address before POSTing", () => {
    const post = stubPost(project("acme"));
    renderForm();

    // a "/"-bearing (or "."/"..") key can't ride the single {project} REST segment,
    // so creating it would strand the operator on an unusable project — the submit
    // gate must refuse it rather than POST (Codex #70)
    fireEvent.change(screen.getByLabelText("name"), { target: { value: "a/b" } });
    expect(screen.getByText(/can't contain "\/"/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /create project/i })).toBeDisabled();
    expect(post).not.toHaveBeenCalled();
  });

  it("fails loud when the create is rejected instead of silently no-op'ing", async () => {
    stubPostError("PROJECT_EXISTS", "project 'acme' already exists");
    renderForm();

    fireEvent.change(screen.getByLabelText("name"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /create project/i }));

    expect(
      await screen.findByText(/create failed: project 'acme' already exists/i),
    ).toBeInTheDocument();
  });
});
