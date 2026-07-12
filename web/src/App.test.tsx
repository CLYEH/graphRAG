import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { project, renderWithProviders, stubProjects, stubProjectsError } from "./test-utils";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("App shell", () => {
  it("renders the section nav and the routed page for the active project", async () => {
    stubProjects([project("acme", "ACME corpus")]);
    renderWithProviders(<App />, { route: "/p/acme/health" });

    // the routed placeholder page
    expect(await screen.findByRole("heading", { name: /project health/i })).toBeInTheDocument();
    // all four v1 sections are navigable
    for (const label of ["Health", "Jobs", "Review", "Playground"]) {
      expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    }
  });

  it("populates the project switcher from the API and shows the active one", async () => {
    stubProjects([project("acme", "ACME corpus"), project("beta")]);
    renderWithProviders(<App />, { route: "/p/acme/health" });

    const select = await screen.findByRole("combobox", { name: /project/i });
    // display_name is preferred over the key; the bare key shows when absent
    expect(screen.getByRole("option", { name: "ACME corpus" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "beta" })).toBeInTheDocument();
    expect(select).toHaveValue("acme");
  });

  it("switching the project navigates to that project", async () => {
    stubProjects([project("acme"), project("beta")]);
    renderWithProviders(<App />, { route: "/p/acme/health" });

    const select = await screen.findByRole("combobox", { name: /project/i });
    fireEvent.change(select, { target: { value: "beta" } });

    // navigating to /p/beta redirects to its health page and the switcher
    // reflects the new active project (read back from the URL param)
    expect(await screen.findByRole("combobox", { name: /project/i })).toHaveValue("beta");
    expect(screen.getByRole("heading", { name: /project health/i })).toBeInTheDocument();
  });

  it("shows an empty state at the root when there are no projects", async () => {
    stubProjects([]);
    renderWithProviders(<App />, { route: "/" });

    expect(await screen.findByText(/no projects yet/i)).toBeInTheDocument();
  });

  it("redirects the root to the first project's health page", async () => {
    stubProjects([project("acme")]);
    renderWithProviders(<App />, { route: "/" });

    // lands on the health placeholder, proving the root redirect resolved
    expect(await screen.findByRole("heading", { name: /project health/i })).toBeInTheDocument();
  });

  it("surfaces an API failure instead of an empty switcher", async () => {
    stubProjectsError();
    renderWithProviders(<App />, { route: "/p/acme/health" });

    expect(await screen.findByText(/projects unavailable/i)).toBeInTheDocument();
  });

  it("fails loud at the root when the API is unreachable", async () => {
    stubProjectsError();
    renderWithProviders(<App />, { route: "/" });

    // RootRedirect must not silently swallow the error and strand the user
    expect(await screen.findByText(/could not reach the api/i)).toBeInTheDocument();
  });

  it("renders NotFound for an unknown section under a valid project", async () => {
    stubProjects([project("acme")]);
    renderWithProviders(<App />, { route: "/p/acme/nonsense" });

    expect(await screen.findByRole("heading", { name: /not found/i })).toBeInTheDocument();
  });

  it("keeps a project whose key has URL-reserved characters openable", async () => {
    // the frozen contract allows any non-empty project key (store tests use
    // slashes/unicode); a raw `/p/${key}` would strand it on NotFound, so the
    // segment is percent-encoded and the router decodes it back (Codex #65 P2)
    stubProjects([project("a/b", "Slashy")]);
    renderWithProviders(<App />, { route: "/" });

    expect(await screen.findByRole("heading", { name: /project health/i })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /project/i })).toHaveValue("a/b");
  });
});
