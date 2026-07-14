import { fireEvent, screen, waitFor } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Overview } from "./Overview";
import { api } from "../api/client";
import { build, healthReport, projectRoute, renderWithProviders } from "../test-utils";

import type { Build, HealthReport, Source } from "../api/queries";

const META = {
  request_id: "00000000-0000-0000-0000-000000000000",
  build_id: null,
  elapsed_ms: 1,
  next_cursor: null,
};

const READY_ID = "b1111111-1111-1111-1111-111111111111";

function source(): Source {
  return {
    id: "s0000000-0000-0000-0000-000000000000",
    project: "acme",
    kind: "text",
    uri: "file:///data/corpus",
    metadata: {},
    created_at: "2026-07-01T00:00:00Z",
  } as Source;
}

// Route-aware stub for the Overview's three reads (+ the activate POST spy is
// separate): a single mockResolvedValue would feed health-shaped data to the
// sources/builds reads and scramble every checklist assertion.
function stubOverview({
  health = healthReport(),
  sources = [],
  builds = [],
}: {
  health?: HealthReport;
  sources?: Source[];
  builds?: Build[];
}) {
  return vi.spyOn(api, "GET").mockImplementation(((path: string) => {
    if (path === "/projects/{project}/health")
      return Promise.resolve({ data: { data: health, meta: META }, error: undefined });
    if (path === "/projects/{project}/sources")
      return Promise.resolve({ data: { data: sources, meta: META }, error: undefined });
    if (path === "/projects/{project}/builds")
      return Promise.resolve({ data: { data: builds, meta: META }, error: undefined });
    throw new Error(`unstubbed GET ${path}`);
  }) as never);
}

function renderOverview() {
  return renderWithProviders(
    <Routes>
      <Route path="/p/:project/overview" element={<Overview />} />
    </Routes>,
    { route: projectRoute("acme", "overview") },
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Overview", () => {
  it("fresh project: nothing done, step ① is the active next step", async () => {
    stubOverview({});
    renderOverview();

    expect(await screen.findByText(/尚未開始/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "去匯入" })).toBeInTheDocument();
    // the later steps offer no actions yet
    expect(screen.queryByRole("link", { name: "去建置" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "上線這個版本" })).not.toBeInTheDocument();
  });

  it("built but not evaluated: step ③ hands over the copyable CLI command", async () => {
    // eval has no API endpoint until UXC1 — the checklist must hand the
    // operator the exact terminal command instead of a dead end
    stubOverview({
      sources: [source()],
      builds: [build({ id: READY_ID, status: "ready", eval: null })],
    });
    renderOverview();

    expect(await screen.findByText(/已建置,尚未上線/)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`eval --build ${READY_ID} -- acme`))).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "上線這個版本" })).not.toBeInTheDocument();
  });

  it("ready to activate: the activate button posts with an Idempotency-Key after confirm", async () => {
    stubOverview({
      sources: [source()],
      builds: [build({ id: READY_ID, status: "ready", eval: { score: 1 } })],
    });
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: { data: build({ id: READY_ID, status: "active" }), meta: META },
      error: undefined,
    } as never);
    renderOverview();

    fireEvent.click(await screen.findByRole("button", { name: "上線這個版本" }));
    // activation swaps what every reader sees — never one click away
    expect(post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "確定上線" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledTimes(1);
      const [path, opts] = post.mock.calls[0] as [string, Record<string, unknown>];
      expect(path).toBe("/projects/{project}/builds/{build_id}/activate");
      const params = (opts as { params: { path: unknown; header: Record<string, string> } }).params;
      expect(params.path).toEqual({ project: "acme", build_id: READY_ID });
      expect(params.header["Idempotency-Key"]).toMatch(/[0-9a-f-]{36}/);
    });
  });

  it("取消 backs out of the confirm without posting", async () => {
    stubOverview({
      sources: [source()],
      builds: [build({ id: READY_ID, status: "ready", eval: { score: 1 } })],
    });
    const post = vi.spyOn(api, "POST");
    renderOverview();

    fireEvent.click(await screen.findByRole("button", { name: "上線這個版本" }));
    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(post).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "上線這個版本" })).toBeEnabled();
  });

  it("renders the server's refusal verbatim, with the CLI hint ONLY for the missing-score case", async () => {
    // the server owns §14 — the UI relays its message and turns it into
    // guidance instead of a dead end
    stubOverview({
      sources: [source()],
      builds: [build({ id: READY_ID, status: "ready", eval: { score: 1 } })],
    });
    // the REAL refusal envelope: a generic message with the gate strings in
    // details.failures (tests/test_builds_api_integration.py) — mocking the
    // gate string into .message was a shape the server never emits
    vi.spyOn(api, "POST").mockResolvedValue({
      data: undefined,
      error: {
        error: {
          code: "BUILD_NOT_READY",
          message: "activation preflight failed for build b1111111",
          details: {
            failures: [
              "eval gate (§20): candidate build has no eval score — run `graphrag eval` on it first; an unmeasured candidate cannot pass the gate",
            ],
          },
        },
      },
    } as never);
    renderOverview();

    fireEvent.click(await screen.findByRole("button", { name: "上線這個版本" }));
    fireEvent.click(screen.getByRole("button", { name: "確定上線" }));

    expect(await screen.findByText(/上線失敗:eval gate/)).toBeInTheDocument();
    expect(screen.getByText(/先在終端機執行/)).toBeInTheDocument();
  });

  it("does NOT claim missing scores for OTHER eval-gate refusals (Codex #77 R2)", async () => {
    // the gate also refuses failed golden cases / unscored ACTIVE builds — a
    // blanket eval-regex would misdiagnose those as「還沒有評測分數」and send
    // the operator down the wrong recovery path; the verbatim message stands alone
    stubOverview({
      sources: [source()],
      builds: [build({ id: READY_ID, status: "ready", eval: { score: 1 } })],
    });
    vi.spyOn(api, "POST").mockResolvedValue({
      data: undefined,
      error: {
        error: {
          code: "BUILD_NOT_READY",
          message: "activation preflight failed for build b1111111",
          details: {
            failures: [
              "eval gate (§20): 2 golden case(s) below their min_score in the candidate's eval report — activation blocked",
            ],
          },
        },
      },
    } as never);
    renderOverview();

    fireEvent.click(await screen.findByRole("button", { name: "上線這個版本" }));
    fireEvent.click(screen.getByRole("button", { name: "確定上線" }));

    expect(await screen.findByText(/上線失敗:eval gate/)).toBeInTheDocument();
    expect(screen.queryByText(/還沒有評測分數/)).not.toBeInTheDocument();
  });

  it("shell-escapes quotes/dollars/backticks in the CLI hint (Codex #77 R2 round 2)", async () => {
    // the first escape implementation was a no-op ("\\$1" in intent, "$1" at
    // runtime) and its spaces-only test could not tell — this key pins the
    // actual escaping of every dangerous character
    stubOverview({
      sources: [source()],
      builds: [build({ id: READY_ID, status: "ready", eval: null })],
    });
    renderWithProviders(
      <Routes>
        <Route path="/p/:project/overview" element={<Overview />} />
      </Routes>,
      { route: projectRoute('a"b$c`d', "overview") },
    );

    const code = await screen.findByText((text) => text.includes('-- "a\\"b\\$c\\`d"'), {
      selector: "code",
    });
    expect(code).toBeInTheDocument();
  });

  it("orders the eval command so a leading-dash key stays positional (Codex #77 R3)", async () => {
    // quoting cannot save `-foo`: the shell strips quotes before argv and
    // argparse reads it as an option — everything after `--` is positional
    stubOverview({
      sources: [source()],
      builds: [build({ id: READY_ID, status: "ready", eval: null })],
    });
    renderWithProviders(
      <Routes>
        <Route path="/p/:project/overview" element={<Overview />} />
      </Routes>,
      { route: projectRoute("-corpus", "overview") },
    );

    const code = await screen.findByText(
      (text) => text.includes(`eval --build ${READY_ID} -- -corpus`),
      { selector: "code" },
    );
    expect(code).toBeInTheDocument();
  });

  it("shell-quotes a project key with spaces in the CLI hint (Codex #77 R2)", async () => {
    stubOverview({
      sources: [source()],
      builds: [build({ id: READY_ID, status: "ready", eval: null })],
    });
    renderWithProviders(
      <Routes>
        <Route path="/p/:project/overview" element={<Overview />} />
      </Routes>,
      { route: projectRoute("my corpus", "overview") },
    );

    expect(
      await screen.findByText(new RegExp(`eval --build ${READY_ID} -- "my corpus"`)),
    ).toBeInTheDocument();
  });

  it("active project: 服務中 status, scale strip, all steps done, review deep link", async () => {
    const ACTIVE = "b2222222-2222-2222-2222-222222222222";
    stubOverview({
      health: healthReport({
        status: "needs_review",
        active_build_id: ACTIVE,
        pending_review: 55,
        counts: { documents: 410, entities: 1409, relations: 1158 },
      }),
      sources: [source()],
      builds: [
        build({
          id: ACTIVE,
          status: "active",
          eval: { score: 1 },
          activated_at: "2026-07-13T16:35:46Z",
        }),
      ],
    });
    renderOverview();

    expect(await screen.findByText(/服務中/)).toBeInTheDocument();
    expect(screen.getByText(/410 份文件 · 1409 個知識點 · 1158 條關聯/)).toBeInTheDocument();
    const reviewLink = screen.getByRole("link", { name: /55 筆疑似重複/ });
    expect(reviewLink).toHaveAttribute("href", expect.stringContaining("review"));
    expect(screen.getAllByText("完成")).toHaveLength(4);
    expect(screen.queryByRole("button", { name: "上線這個版本" })).not.toBeInTheDocument();
  });

  it("targets the newest ready build by started_at, not list order (Codex #77)", async () => {
    // the builds API pages by UUID id desc — arbitrary in time; an OLDER ready
    // build listed first must not become the activation target
    const OLD_ID = "b0ldbldb-0000-4000-8000-000000000000";
    stubOverview({
      sources: [source()],
      builds: [
        build({ id: OLD_ID, status: "ready", eval: null, started_at: "2026-07-01T00:00:00Z" }),
        build({ id: READY_ID, status: "ready", eval: null, started_at: "2026-07-02T00:00:00Z" }),
      ],
    });
    renderOverview();

    // the CLI command names the NEWER build even though it is listed second
    expect(
      await screen.findByText(new RegExp(`eval --build ${READY_ID} -- acme`)),
    ).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(OLD_ID))).not.toBeInTheDocument();
  });

  it("offers an update card when an active project grows a newer evaluated ready build (Codex #77)", async () => {
    // activation is not once-only onboarding: without this card the Console's
    // ONLY activate path would vanish after first launch
    const ACTIVE = "b2222222-2222-2222-2222-222222222222";
    stubOverview({
      health: healthReport({ active_build_id: ACTIVE, counts: {} }),
      sources: [source()],
      builds: [
        build({ id: ACTIVE, status: "active", eval: { score: 1 } }),
        build({
          id: READY_ID,
          status: "ready",
          eval: { score: 1 },
          started_at: "2026-07-02T00:00:00Z",
        }),
      ],
    });
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: { data: build({ id: READY_ID, status: "active" }), meta: META },
      error: undefined,
    } as never);
    renderOverview();

    expect(await screen.findByText(/有更新的建置版本可上線/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "上線這個版本" }));
    fireEvent.click(screen.getByRole("button", { name: "確定上線" }));
    await waitFor(() => {
      const [, opts] = post.mock.calls[0] as [string, Record<string, unknown>];
      const params = (opts as { params: { path: { build_id: string } } }).params;
      expect(params.path.build_id).toBe(READY_ID);
    });
  });

  it("never offers a LINGERING OLDER ready build as an update (downgrade guard)", async () => {
    // activation archives only the previously-active build, so an older ready
    // build lingers; labelling it 有更新的版本 would offer a downgrade
    const ACTIVE = "b2222222-2222-2222-2222-222222222222";
    stubOverview({
      health: healthReport({ active_build_id: ACTIVE, counts: {} }),
      sources: [source()],
      builds: [
        build({
          id: ACTIVE,
          status: "active",
          eval: { score: 1 },
          started_at: "2026-07-02T00:00:00Z",
        }),
        build({
          id: READY_ID,
          status: "ready",
          eval: { score: 1 },
          started_at: "2026-07-01T00:00:00Z",
        }),
      ],
    });
    renderOverview();

    expect(await screen.findByText(/服務中/)).toBeInTheDocument();
    expect(screen.queryByText(/有更新的建置版本可上線/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "上線這個版本" })).not.toBeInTheDocument();
  });

  it("the update card without eval scores hands over the CLI command, no button", async () => {
    const ACTIVE = "b2222222-2222-2222-2222-222222222222";
    stubOverview({
      health: healthReport({ active_build_id: ACTIVE, counts: {} }),
      sources: [source()],
      builds: [
        build({ id: ACTIVE, status: "active", eval: { score: 1 } }),
        build({ id: READY_ID, status: "ready", eval: null, started_at: "2026-07-02T00:00:00Z" }),
      ],
    });
    renderOverview();

    expect(await screen.findByText(/新版本還沒有評測分數/)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`eval --build ${READY_ID} -- acme`))).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "上線這個版本" })).not.toBeInTheDocument();
  });

  it("locks the activate write while the builds read is refetching (fail-closed)", async () => {
    // R5/R10 applied at birth: a (re)fetching builds list means the candidate
    // row may be about to change — the write waits for the settled world.
    // A hung second GET latches isFetching; the refetch is started directly on
    // the query client, so no other gate can explain the disabled button.
    let buildsCalls = 0;
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path === "/projects/{project}/health")
        return Promise.resolve({ data: { data: healthReport(), meta: META }, error: undefined });
      if (path === "/projects/{project}/sources")
        return Promise.resolve({ data: { data: [source()], meta: META }, error: undefined });
      if (path === "/projects/{project}/builds") {
        buildsCalls += 1;
        if (buildsCalls > 1) return new Promise(() => {});
        return Promise.resolve({
          data: {
            data: [build({ id: READY_ID, status: "ready", eval: { score: 1 } })],
            meta: META,
          },
          error: undefined,
        });
      }
      throw new Error(`unstubbed GET ${path}`);
    }) as never);
    const { queryClient } = renderOverviewWithClient();

    expect(await screen.findByRole("button", { name: "上線這個版本" })).toBeEnabled();
    void queryClient.invalidateQueries({ queryKey: ["builds", "acme"] });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "上線這個版本" })).toBeDisabled(),
    );
  });

  it("fails loud when a status read fails — a status page must not guess", async () => {
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path === "/projects/{project}/health")
        return Promise.resolve({
          data: undefined,
          error: { error: { code: "STORE_UNAVAILABLE", message: "postgres down" } },
        });
      return Promise.resolve({ data: { data: [], meta: META }, error: undefined });
    }) as never);
    renderOverview();

    expect(await screen.findByText(/無法載入專案狀態:postgres down/)).toBeInTheDocument();
  });
});

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { render } from "@testing-library/react";

function renderOverviewWithClient() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, retryDelay: 0 } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[projectRoute("acme", "overview")]}>
        <Routes>
          <Route path="/p/:project/overview" element={<Overview />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { queryClient };
}
