import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RunsTable } from "./RunsTable";
import { api } from "../api/client";
import { build, renderWithProviders, stubApiError, stubBuilds } from "../test-utils";

afterEach(() => {
  vi.restoreAllMocks();
});

const FAILED = "b2222222-bbbb-4bbb-8bbb-000000000002";
const META = { next_cursor: null, build_id: "b1", request_id: "r", elapsed_ms: 1 };

// A failed build whose steps/items drill-down and retry are stubbed by template
// path (openapi-fetch passes the path TEMPLATE, so /steps and /items are the
// leaf-suffix branches; anything else is the builds list). One page each.
function stubDrilldown(opts: { steps?: unknown[]; items?: unknown[] } = {}) {
  return vi.spyOn(api, "GET").mockImplementation(((path: string) => {
    const data = path.endsWith("/items")
      ? (opts.items ?? [])
      : path.endsWith("/steps")
        ? (opts.steps ?? [])
        : [build({ id: FAILED, status: "failed" })];
    return Promise.resolve({ data: { data, meta: META }, error: undefined });
  }) as never);
}

describe("RunsTable", () => {
  it("lists builds with a status badge per run", async () => {
    stubBuilds([
      build({ id: "b1111111-aaaa-4aaa-8aaa-000000000001", status: "active" }),
      build({ id: "b2222222-bbbb-4bbb-8bbb-000000000002", status: "failed" }),
    ]);
    renderWithProviders(<RunsTable project="acme" />);

    // words on the surface: the start time names the version; the uuid rides
    // the hover title only (UXA3) — its bare prefix must NOT be visible text
    expect((await screen.findAllByText(/版$/)).length).toBeGreaterThan(0);
    expect(screen.queryByText("b1111111")).not.toBeInTheDocument();
    expect(screen.getByText("上線中")).toBeInTheDocument();
    expect(screen.getByText("失敗")).toBeInTheDocument();
  });

  it("expands a run to drill into hashes and metrics", async () => {
    stubBuilds([
      build({
        id: "b1111111-aaaa-4aaa-8aaa-000000000001",
        status: "failed",
        config_hash: "cfg-abc",
        metrics: { groundedness: 0.91 },
      }),
    ]);
    renderWithProviders(<RunsTable project="acme" />);

    fireEvent.click((await screen.findAllByText(/版$/))[0]);
    // the drill-down is what makes a failed run diagnosable from the dashboard
    expect(await screen.findByText("cfg-abc")).toBeInTheDocument();
    expect(screen.getByText(/"groundedness":0\.91/)).toBeInTheDocument();
  });

  it("shows an empty state when there are no builds", async () => {
    stubBuilds([]);
    renderWithProviders(<RunsTable project="acme" />);

    expect(await screen.findByText(/no builds yet/i)).toBeInTheDocument();
  });

  it("fails loud instead of showing an empty table when builds can't load", async () => {
    stubApiError();
    renderWithProviders(<RunsTable project="acme" />);

    expect(await screen.findByText(/could not load runs/i)).toBeInTheDocument();
  });

  it("drills a failed build into its §27.7 steps and their failed items (RB1)", async () => {
    stubDrilldown({
      steps: [
        {
          id: "s1111111-cccc-4ccc-8ccc-000000000001",
          step_name: "graph",
          status: "failed",
          failed_count: 3,
          skipped_count: 1,
          input_count: 20,
        },
      ],
      items: [
        {
          id: "i1111111-dddd-4ddd-8ddd-000000000001",
          item_kind: "document",
          item_ref: "hash-bad",
          status: "failed",
          message: "LLM schema invalid",
        },
      ],
    });
    renderWithProviders(<RunsTable project="acme" />);

    fireEvent.click((await screen.findAllByText(/版$/))[0]); // expand the failed build
    // the step drill-down names WHERE the build failed + how many items
    expect(await screen.findByText("graph")).toBeInTheDocument();
    expect(screen.getByText(/失敗 3 · 跳過 1 · 輸入 20/)).toBeInTheDocument();
    // clicking the step reveals its failed items — item_ref is the retry key
    fireEvent.click(screen.getByRole("button", { name: /graph/i }));
    expect(await screen.findByText(/document:hash-bad/)).toBeInTheDocument();
    expect(screen.getByText(/LLM schema invalid/)).toBeInTheDocument();
  });

  it("retries a failed build via POST /retry with an Idempotency-Key and surfaces the job (RB1)", async () => {
    stubDrilldown({ steps: [] });
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: {
        data: { job_id: "j0000000-0000-4000-8000-000000000001", status: "queued" },
        meta: META,
      },
      error: undefined,
    } as never);
    renderWithProviders(<RunsTable project="acme" />);

    fireEvent.click((await screen.findAllByText(/版$/))[0]);
    fireEvent.click(await screen.findByRole("button", { name: "只重試失敗項" }));

    // posts to /retry for THIS build, carrying an Idempotency-Key (safe retry)
    await waitFor(() => expect(post).toHaveBeenCalled());
    const opts = post.mock.calls[0][1] as {
      params?: { path?: { build_id?: string }; header?: Record<string, string> };
    };
    expect(String(post.mock.calls[0][0])).toContain("/retry");
    expect(opts.params?.path?.build_id).toBe(FAILED);
    expect(opts.params?.header?.["Idempotency-Key"]).toBeTruthy();
    // and surfaces the new job id
    expect(await screen.findByText(/已建立重試工作 j0000000/)).toBeInTheDocument();
  });

  it("surfaces a retry refusal instead of swallowing it — BUILD_NOT_RETRYABLE (RB1)", async () => {
    stubDrilldown({ steps: [] });
    vi.spyOn(api, "POST").mockResolvedValue({
      data: undefined,
      error: {
        error: {
          code: "BUILD_NOT_RETRYABLE",
          message: "build committed no documents; run a full build",
          details: null,
          request_id: "r",
        },
      },
    } as never);
    renderWithProviders(<RunsTable project="acme" />);

    fireEvent.click((await screen.findAllByText(/版$/))[0]);
    fireEvent.click(await screen.findByRole("button", { name: "只重試失敗項" }));

    expect(await screen.findByText(/重試失敗:build committed no documents/)).toBeInTheDocument();
  });

  it("reuses the SAME Idempotency-Key when retrying a FAILED attempt — no forked child (RB1)", async () => {
    // The core safety property: a lost-202 replay must reuse the key so the
    // server replays the ORIGINAL job, never forks a second child. Fail once →
    // retry the same intent → the two POSTs must carry the same key. A
    // `??=`→`=` regression (fresh key per click) makes keyOf(1) ≠ keyOf(0).
    stubDrilldown({ steps: [] });
    const post = vi
      .spyOn(api, "POST")
      .mockResolvedValueOnce({
        data: undefined,
        error: {
          error: { code: "STORE_UNAVAILABLE", message: "down", details: null, request_id: "r" },
        },
      } as never)
      .mockResolvedValueOnce({
        data: {
          data: { job_id: "j0000000-0000-4000-8000-000000000001", status: "queued" },
          meta: META,
        },
        error: undefined,
      } as never);
    renderWithProviders(<RunsTable project="acme" />);

    fireEvent.click((await screen.findAllByText(/版$/))[0]);
    const btn = await screen.findByRole("button", { name: "只重試失敗項" });
    fireEvent.click(btn);
    await screen.findByText(/重試失敗:down/); // first attempt failed → key retained
    fireEvent.click(btn); // retry the SAME intent

    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    const keyOf = (i: number) =>
      (post.mock.calls[i][1] as { params?: { header?: Record<string, string> } }).params?.header?.[
        "Idempotency-Key"
      ];
    expect(keyOf(1)).toBe(keyOf(0));
  });

  it("mints a FRESH Idempotency-Key for a deliberate re-retry after success (RB1)", async () => {
    // The flip side: once a retry SUCCEEDS, a second click is a genuine new
    // attempt and must fork a NEW child — reusing the key would replay the now-
    // terminal job and no build would appear. The key resets on success.
    stubDrilldown({ steps: [] });
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: {
        data: { job_id: "j0000000-0000-4000-8000-000000000001", status: "queued" },
        meta: META,
      },
      error: undefined,
    } as never);
    renderWithProviders(<RunsTable project="acme" />);

    fireEvent.click((await screen.findAllByText(/版$/))[0]);
    const btn = await screen.findByRole("button", { name: "只重試失敗項" });
    fireEvent.click(btn);
    await screen.findByText(/已建立重試工作/); // first retry succeeded → key reset
    fireEvent.click(btn); // a second, genuine re-retry

    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    const keyOf = (i: number) =>
      (post.mock.calls[i][1] as { params?: { header?: Record<string, string> } }).params?.header?.[
        "Idempotency-Key"
      ];
    expect(keyOf(1)).not.toBe(keyOf(0));
  });

  it("shows the parent lineage on a retry (child) build (RB1)", async () => {
    stubDrilldown({ steps: [] });
    // override the builds page to a retry child carrying parent_build_id
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path.endsWith("/steps") || path.endsWith("/items"))
        return Promise.resolve({ data: { data: [], meta: META }, error: undefined });
      return Promise.resolve({
        data: {
          data: [
            build({
              id: FAILED,
              status: "failed",
              parent_build_id: "b0000000-0000-4000-8000-000000000000",
            }),
          ],
          meta: META,
        },
        error: undefined,
      });
    }) as never);
    renderWithProviders(<RunsTable project="acme" />);

    fireEvent.click((await screen.findAllByText(/版$/))[0]);
    expect(await screen.findByText("重試自")).toBeInTheDocument();
    expect(screen.getByText("b0000000-0000-4000-8000-000000000000")).toBeInTheDocument();
  });
});
