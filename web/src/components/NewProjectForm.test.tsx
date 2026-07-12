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
  it("creates with a name-keyed idempotency key and lands in the new project", async () => {
    const post = stubPost(project("acme", "Acme Inc"));
    renderForm();

    fireEvent.change(screen.getByLabelText("name"), { target: { value: "acme" } });
    fireEvent.change(screen.getByLabelText(/display name/i), { target: { value: "Acme Inc" } });
    fireEvent.click(screen.getByRole("button", { name: /create project/i }));

    // navigation lands in the created project (health, matching RootRedirect)
    expect(await screen.findByText(/health for acme/i)).toBeInTheDocument();
    // name is the projects PK, so it doubles as the Idempotency-Key: a lost 201
    // replays on retry rather than the name conflict misreporting a committed create
    expect(post).toHaveBeenCalledWith(
      "/projects",
      expect.objectContaining({
        params: { header: { "Idempotency-Key": "acme" } },
        body: { name: "acme", display_name: "Acme Inc" },
      }),
    );
  });

  it("cannot submit without a name (the required primary key)", () => {
    stubPost(project("acme"));
    renderForm();
    expect(screen.getByRole("button", { name: /create project/i })).toBeDisabled();
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
