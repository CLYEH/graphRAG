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
  // QA9: the landing project is now persisted, so a leaked key would make
  // these tests order-dependent
  localStorage.clear();
});

describe("App shell", () => {
  it("renders the section nav and the routed page for the active project", async () => {
    stubProjects([project("acme", "ACME corpus")]);
    renderWithProviders(<App />, { route: projectRoute("acme") });

    // the routed placeholder page
    expect(await screen.findByRole("heading", { name: "專案健康(診斷)" })).toBeInTheDocument();
    // every section is navigable
    for (const label of ["診斷", "匯入", "建置", "治理", "檢索"]) {
      expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    }
  });

  it("populates the project switcher from the API and shows the active one", async () => {
    stubProjects([project("acme", "ACME corpus"), project("beta")]);
    renderWithProviders(<App />, { route: projectRoute("acme") });

    const select = await screen.findByRole("combobox", { name: /專案/ });
    // display_name is preferred over the key; the bare key shows when absent
    expect(screen.getByRole("option", { name: "ACME corpus" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "beta" })).toBeInTheDocument();
    expect(select).toHaveValue("acme");
  });

  it("switching the project navigates to that project", async () => {
    stubProjects([project("acme"), project("beta")]);
    renderWithProviders(<App />, { route: projectRoute("acme") });

    const select = await screen.findByRole("combobox", { name: /專案/ });
    fireEvent.change(select, { target: { value: "beta" } });

    // navigating to /p/beta redirects to its 總覽 (UXA2 landing) and the
    // switcher reflects the new active project (read back from the URL param).
    expect(await screen.findByRole("combobox", { name: /專案/ })).toHaveValue("beta");
    expect(await screen.findByRole("heading", { name: "總覽" })).toBeInTheDocument();
  });

  it("shows an empty state at the root when there are no projects", async () => {
    stubProjects([]);
    renderWithProviders(<App />, { route: "/" });

    expect(await screen.findByText(/還沒有任何專案/)).toBeInTheDocument();
  });

  it("redirects the root to the first project's 總覽 page", async () => {
    stubProjects([project("acme")]);
    renderWithProviders(<App />, { route: "/" });

    // lands on the 總覽 landing (UXA2), proving the root redirect resolved
    expect(await screen.findByRole("heading", { name: "總覽" })).toBeInTheDocument();
  });

  it("returns to the project the operator last opened, not the newest one", async () => {
    // WHY (QA9/D17): projects list created_at DESC, so `projects[0]` is the
    // newest — usually the empty shell just created. Opening the Console used
    // to land there, so every session began by switching away.
    localStorage.setItem("graphrag.lastProject", "working-corpus");
    stubProjects([project("brand-new-shell"), project("working-corpus")]);
    renderWithProviders(<App />, { route: "/" });

    // the switcher reads the active project back out of the URL, so this
    // asserts where the redirect actually went — not merely what was stored
    expect(await screen.findByRole("combobox", { name: /專案/ })).toHaveValue("working-corpus");
  });

  it("remembers a project opened by URL, not only one picked in the switcher", async () => {
    // WHY: arriving by bookmark, link or back-button is how an operator most
    // often opens a project; recording only the switcher's onChange would miss
    // every one of those paths.
    stubProjects([project("acme"), project("beta")]);
    renderWithProviders(<App />, { route: projectRoute("beta") });

    await screen.findByRole("combobox", { name: /專案/ });
    expect(localStorage.getItem("graphrag.lastProject")).toBe("beta");
  });

  it("a stale bookmark does not erase the remembered project", async () => {
    // WHY (Codex #148 P2): the effect recorded EVERY decodable :project route,
    // including one that no longer exists. Following a stale bookmark to a
    // deleted project therefore overwrote a perfectly good preference, and the
    // next visit to `/` rejected the now-unknown key and fell back to
    // projects[0] — reinstating the newest-project landing this task exists to
    // remove. The route being decodable says nothing about the project being
    // real, so the list is what decides.
    localStorage.setItem("graphrag.lastProject", "working-corpus");
    stubProjects([project("brand-new-shell"), project("working-corpus")]);
    renderWithProviders(<App />, { route: projectRoute("deleted-since") });

    await screen.findByRole("combobox", { name: /專案/ });
    expect(localStorage.getItem("graphrag.lastProject")).toBe("working-corpus");
  });

  it("filters the switcher once the list is too long to scan", async () => {
    // WHY (QA9/D17): the old comment assumed "project counts are small". A
    // native select cannot be searched and its type-ahead only matches a label
    // PREFIX, so finding a project got harder exactly as it mattered more.
    const many = Array.from({ length: 10 }, (_, i) => project(`proj-${i}`, `Corpus ${i}`));
    stubProjects(many);
    renderWithProviders(<App />, { route: projectRoute("proj-0") });

    const search = await screen.findByRole("searchbox", { name: /搜尋/ });
    fireEvent.change(search, { target: { value: "us 7" } });

    // substring match on the DISPLAY NAME, not just a prefix of it
    expect(screen.getByRole("option", { name: "Corpus 7" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Corpus 3" })).not.toBeInTheDocument();
    // the ACTIVE project survives a filter that excludes it — a select whose
    // value is missing from its options renders blank, which would read as
    // "no project open"
    expect(screen.getByRole("option", { name: "Corpus 0" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /專案/ })).toHaveValue("proj-0");
  });

  it("keeps the switcher plain when there are few projects", async () => {
    // over-block dual: the filter is an answer to long lists, so it must not
    // appear (or take a tab stop) in the common small-list case
    stubProjects([project("acme"), project("beta")]);
    const renderResult = renderWithProviders(<App />, { route: projectRoute("acme") });

    const { container } = renderResult;
    await screen.findByRole("combobox", { name: /專案/ });
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();

    // The live region must EXIST before it ever has text: a region mounted
    // together with its content is unreliably announced (VoiceOver). Reverting
    // to conditional mounting is otherwise invisible to every test.
    expect(container.querySelector('[aria-live="polite"]')).toBeInTheDocument();
  });

  it("says so when the filter matches nothing", async () => {
    const many = Array.from({ length: 10 }, (_, i) => project(`proj-${i}`));
    stubProjects(many);
    renderWithProviders(<App />, { route: projectRoute("proj-0") });

    const search = await screen.findByRole("searchbox", { name: /搜尋/ });
    fireEvent.change(search, { target: { value: "no-such-project" } });

    // an empty select with no explanation reads as "projects failed to load"
    expect(screen.getByText(/沒有符合的專案/)).toBeInTheDocument();
  });

  it("surfaces an API failure instead of an empty switcher", async () => {
    stubProjectsError();
    renderWithProviders(<App />, { route: projectRoute("acme") });

    expect(await screen.findByText("無法載入專案")).toBeInTheDocument();
  });

  it("fails loud at the root when the API is unreachable", async () => {
    stubProjectsError();
    renderWithProviders(<App />, { route: "/" });

    // RootRedirect must not silently swallow the error and strand the user
    expect(await screen.findByText(/無法連線到 API/)).toBeInTheDocument();
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

    await screen.findByRole("combobox", { name: /專案/ });
    expect(screen.getByRole("option", { name: "p1" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "p2" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /專案/ })).toHaveValue("p2");
  });

  it("keeps a project whose key has URL-reserved characters openable and addressable", async () => {
    // a reserved char like "?" percent-encodes to a surviving segment (a%3Fb), so
    // the key is both openable (base64url route, Codex #65) and API-addressable —
    // the 總覽 landing loads end-to-end. Only "/" and "."/".." break (see next test).
    stubProjects([project("a?b", "Questiony")]);
    renderWithProviders(<App />, { route: "/" });

    expect(await screen.findByRole("heading", { name: "總覽" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /專案/ })).toHaveValue("a?b");
  });

  it("resolves an un-addressable project's route but reports it can't be fetched", async () => {
    // base64url keeps "/"-bearing and "."/".." keys openable in the route (switcher
    // reflects it), but a REST path can't carry them (404 / normalization), so the
    // health page reports that instead of firing the call (Codex #65 P2 / #66 P2)
    stubProjects([project("a/b", "Slashy")]);
    renderWithProviders(<App />, { route: "/" });

    expect(await screen.findByText(/isn't addressable over the api/i)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /專案/ })).toHaveValue("a/b");
  });
});
