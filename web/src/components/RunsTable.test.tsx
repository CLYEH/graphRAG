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
    // the run-level pointer is UNCONDITIONAL — it must show even when a step DID
    // fail, so a LATER crash can't hide behind an earlier item failure (R4)
    expect(screen.getByText(/確切失敗原因/)).toBeInTheDocument();
  });

  it("filters the drill-down to one diagnosis status and switches to skipped on demand (RB1)", async () => {
    // Under `sampled`/`all` verbosity the recorder ALSO persists successes,
    // ordered by id, so an UNFILTERED page could be all successes and bury the
    // failures this view exists to show. The drill-down must always send
    // `filter[status]` (default failed) and offer a SEPARATE fetch for skipped
    // items (Codex #102). The stub keys on that filter — a missing/wrong filter
    // renders NOTHING, so this is a real mutation probe, not a path-only stub.
    vi.spyOn(api, "GET").mockImplementation(((
      path: string,
      opts?: { params?: { query?: { filter?: { status?: string } } } },
    ) => {
      if (path.endsWith("/items")) {
        const s = opts?.params?.query?.filter?.status;
        const items =
          s === "failed"
            ? [{ id: "if1", item_kind: "document", item_ref: "hash-fail", status: "failed" }]
            : s === "skipped"
              ? [{ id: "is1", item_kind: "document", item_ref: "hash-skip", status: "skipped" }]
              : []; // no/unknown filter would let successes leak — here, nothing shows
        return Promise.resolve({ data: { data: items, meta: META }, error: undefined });
      }
      if (path.endsWith("/steps"))
        return Promise.resolve({
          data: {
            data: [
              {
                id: "s2222222-cccc-4ccc-8ccc-000000000002",
                step_name: "graph",
                status: "failed",
                failed_count: 3,
                skipped_count: 2,
                input_count: 20,
              },
            ],
            meta: META,
          },
          error: undefined,
        });
      return Promise.resolve({
        data: { data: [build({ id: FAILED, status: "failed" })], meta: META },
        error: undefined,
      });
    }) as never);
    renderWithProviders(<RunsTable project="acme" />);

    fireEvent.click((await screen.findAllByText(/版$/))[0]);
    fireEvent.click(await screen.findByRole("button", { name: /graph/i }));
    // default tab = 失敗: the failed item shows; the skipped one is NOT fetched
    expect(await screen.findByText(/document:hash-fail/)).toBeInTheDocument();
    expect(screen.queryByText(/document:hash-skip/)).not.toBeInTheDocument();
    // the selector labels carry the step's nullable counts (失敗 3 / 跳過 2) —
    // exact names so they don't also match the step-meta line's "跳過 2"
    fireEvent.click(screen.getByRole("button", { name: "跳過 2" }));
    // switching re-queries with filter[status]=skipped — the separate strategy
    expect(await screen.findByText(/document:hash-skip/)).toBeInTheDocument();
    expect(screen.queryByText(/document:hash-fail/)).not.toBeInTheDocument();
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
    fireEvent.click(await screen.findByRole("button", { name: "重試此建置" }));

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
    fireEvent.click(await screen.findByRole("button", { name: "重試此建置" }));

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
    const btn = await screen.findByRole("button", { name: "重試此建置" });
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
    const btn = await screen.findByRole("button", { name: "重試此建置" });
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

  it("renders unmeasured (null) step counts as — not 0 (RB1)", async () => {
    // a step that never ran reports NULL counts; the contract distinguishes null
    // from a measured 0, so rendering 0 would fake "0 failures observed" and
    // mislead the diagnosis — show "—" for null (Codex #102).
    stubDrilldown({
      steps: [
        {
          id: "s1111111-cccc-4ccc-8ccc-000000000009",
          step_name: "summarize",
          status: "pending",
          failed_count: null,
          skipped_count: null,
          input_count: null,
        },
      ],
    });
    renderWithProviders(<RunsTable project="acme" />);

    fireEvent.click((await screen.findAllByText(/版$/))[0]);
    expect(await screen.findByText(/失敗 — · 跳過 — · 輸入 —/)).toBeInTheDocument();
  });

  it("paginates step items with load-more instead of exhausting the chain (RB1)", async () => {
    // a step can hold corpus-sized items; the drill-down must render a PAGE + a
    // load-more, not download the whole cursor chain up front (Codex #102).
    let itemsCall = 0;
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path.endsWith("/items")) {
        itemsCall += 1;
        return Promise.resolve(
          itemsCall === 1
            ? {
                data: {
                  data: [{ id: "it1", item_kind: "document", item_ref: "r1", status: "failed" }],
                  meta: { ...META, next_cursor: "c2" },
                },
                error: undefined,
              }
            : {
                data: {
                  data: [{ id: "it2", item_kind: "document", item_ref: "r2", status: "skipped" }],
                  meta: META,
                },
                error: undefined,
              },
        );
      }
      if (path.endsWith("/steps"))
        return Promise.resolve({
          data: {
            data: [
              {
                id: "s1",
                step_name: "graph",
                status: "failed",
                failed_count: 1,
                skipped_count: 1,
                input_count: 2,
              },
            ],
            meta: META,
          },
          error: undefined,
        });
      return Promise.resolve({
        data: { data: [build({ id: FAILED, status: "failed" })], meta: META },
        error: undefined,
      });
    }) as never);
    renderWithProviders(<RunsTable project="acme" />);

    fireEvent.click((await screen.findAllByText(/版$/))[0]);
    fireEvent.click(await screen.findByRole("button", { name: /graph/i }));
    // only page 1 is fetched up front — NOT the whole chain
    expect(await screen.findByText(/document:r1/)).toBeInTheDocument();
    expect(screen.queryByText(/document:r2/)).not.toBeInTheDocument();
    // load-more pulls the next page
    fireEvent.click(screen.getByRole("button", { name: /載入更多項目/ }));
    expect(await screen.findByText(/document:r2/)).toBeInTheDocument();
  });

  it("surfaces a step item's structured error when message is absent (RB1)", async () => {
    // the failure reason may ride the structured `error` object (frozen on
    // BuildStepItem), not the optional `message` — the diagnosis must not
    // discard it (Codex #102 R2). With only `it.message`, this shows nothing.
    stubDrilldown({
      steps: [
        {
          id: "s1111111-cccc-4ccc-8ccc-00000000000a",
          step_name: "graph",
          status: "failed",
          failed_count: 1,
          skipped_count: 0,
          input_count: 5,
        },
      ],
      items: [
        {
          id: "i1111111-dddd-4ddd-8ddd-00000000000a",
          item_kind: "document",
          item_ref: "hash-x",
          status: "failed",
          message: null,
          error: { kind: "ParseError", detail: "non-JSON extraction output" },
        },
      ],
    });
    renderWithProviders(<RunsTable project="acme" />);

    fireEvent.click((await screen.findAllByText(/版$/))[0]);
    fireEvent.click(await screen.findByRole("button", { name: /graph/i }));
    // the structured error is surfaced (not swallowed with only status + ref)
    expect(await screen.findByText(/ParseError/)).toBeInTheDocument();
    expect(screen.getByText(/non-JSON extraction output/)).toBeInTheDocument();
  });

  it("guides the operator when a hard failure left no failing step in the drill-down (RB1)", async () => {
    // a stage CRASH records only pipeline_runs.error and never a failed step, so
    // the drill-down shows only successful steps — the UI must still point at the
    // run level, not imply nothing failed. The pointer is UNCONDITIONAL: this
    // no-failed-step case and the failed-step case above both show it, so a later
    // crash can never hide behind an earlier item failure (Codex #102 R3/R4).
    stubDrilldown({
      steps: [
        {
          id: "s1111111-cccc-4ccc-8ccc-00000000000b",
          step_name: "ingest",
          status: "done",
          failed_count: 0,
          skipped_count: 0,
          input_count: 3,
        },
      ],
    });
    renderWithProviders(<RunsTable project="acme" />);

    fireEvent.click((await screen.findAllByText(/版$/))[0]);
    const note = await screen.findByText(/確切失敗原因/);
    // …but it must NOT send the operator to the run's job id: the dashboard
    // exposes only the BUILD id and there is no build→job lookup or jobs list, so
    // that identifier is unobtainable from this flow. The note states the Console
    // limitation honestly instead of promising a false path (Codex #102 P1).
    expect(note).toHaveTextContent(/尚未呈現|後續增強/);
    expect(screen.queryByText(/job id/i)).not.toBeInTheDocument();
  });
});
