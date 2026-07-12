import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  project,
  projectRoute,
  renderWithProviders,
  stubProjects,
  stubProjectsError,
  stubProjectsPages,
} from "./test-utils";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("App shell", () => {
  it("renders the section nav and the routed page for the active project", async () => {
    stubProjects([project("acme", "ACME corpus")]);
    renderWithProviders(<App />, { route: projectRoute("acme") });

    // the routed placeholder page
    expect(await screen.findByRole("heading", { name: /project health/i })).toBeInTheDocument();
    // all four v1 sections are navigable
    for (const label of ["Health", "Jobs", "Review", "Playground"]) {
      expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    }
  });

  it("populates the project switcher from the API and shows the active one", async () => {
    stubProjects([project("acme", "ACME corpus"), project("beta")]);
    renderWithProviders(<App />, { route: projectRoute("acme") });

    const select = await screen.findByRole("combobox", { name: /project/i });
    // display_name is preferred over the key; the bare key shows when absent
    expect(screen.getByRole("option", { name: "ACME corpus" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "beta" })).toBeInTheDocument();
    expect(select).toHaveValue("acme");
  });

  it("switching the project navigates to that project", async () => {
    stubProjects([project("acme"), project("beta")]);
    renderWithProviders(<App />, { route: projectRoute("acme") });

    const select = await screen.findByRole("combobox", { name: /project/i });
    fireEvent.change(select, { target: { value: "beta" } });

    // navigating to /p/beta redirects to its health page and the switcher
    // reflects the new active project (read back from the URL param). The health
    // page loads async now, so await its heading rather than reading it sync.
    expect(await screen.findByRole("combobox", { name: /project/i })).toHaveValue("beta");
    expect(await screen.findByRole("heading", { name: /project health/i })).toBeInTheDocument();
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
    renderWithProviders(<App />, { route: projectRoute("acme") });

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
    renderWithProviders(<App />, { route: projectRoute("acme", "nonsense") });

    expect(await screen.findByRole("heading", { name: /not found/i })).toBeInTheDocument();
  });

  it("pages through next_cursor so a project beyond the first page is reachable", async () => {
    // a switcher that stops at page 1 would drop older projects and blank the
    // select when the user lands on one of their URLs (Codex #65 P2)
    stubProjectsPages([[project("p1")], [project("p2")]]);
    renderWithProviders(<App />, { route: projectRoute("p2") });

    await screen.findByRole("combobox", { name: /project/i });
    expect(screen.getByRole("option", { name: "p1" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "p2" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /project/i })).toHaveValue("p2");
  });

  it("keeps a project whose key has URL-reserved characters openable", async () => {
    // the frozen contract allows any non-empty project key (store tests use
    // slashes/unicode); a raw `/p/${key}` would strand it on NotFound, so the
    // segment is base64url-encoded and the router decodes it back (Codex #65 P2)
    stubProjects([project("a/b", "Slashy")]);
    renderWithProviders(<App />, { route: "/" });

    expect(await screen.findByRole("heading", { name: /project health/i })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /project/i })).toHaveValue("a/b");
  });

  it("resolves a dot-segment project's route but reports it isn't API-addressable", async () => {
    // base64url keeps `.` openable in the route (switcher reflects it), but a REST
    // path can't carry a "." segment (it normalizes to the wrong endpoint), so the
    // health page reports that instead of firing the call (Codex #65 P2 / #66 P2)
    stubProjects([project(".", "Dotty")]);
    renderWithProviders(<App />, { route: "/" });

    expect(await screen.findByText(/reserved url path segment/i)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /project/i })).toHaveValue(".");
  });
});
