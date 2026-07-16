import { fireEvent, screen, waitFor } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Quality } from "./Quality";
import { api } from "../api/client";
import { build, job, projectRoute, renderWithProviders } from "../test-utils";

import type { Build, Job } from "../api/queries";

// The job SSE rides raw fetch (jobStream.ts), which jsdom cannot serve — the
// module is mocked behind a swappable impl: silent by default (states driven
// through the job SNAPSHOT), overridden per test to emit frames or close (the
// stream-terminal invalidation and refetch-failure cells need the stream).
type StreamOpts = { signal: AbortSignal; onFrame: (frame: { data: string }) => void };
const stream = vi.hoisted(() => ({
  impl: ((_jobId: string, _opts: unknown) => new Promise<void>(() => {})) as (
    jobId: string,
    opts: unknown,
  ) => Promise<void>,
}));
vi.mock("../api/jobStream", async (importOriginal) => {
  const real = await importOriginal<typeof import("../api/jobStream")>();
  return {
    ...real,
    streamJobEvents: (jobId: string, opts: unknown) => stream.impl(jobId, opts),
  };
});

const META = {
  request_id: "00000000-0000-0000-0000-000000000000",
  build_id: null,
  elapsed_ms: 1,
  next_cursor: null,
};

const READY_ID = "b1111111-1111-1111-1111-111111111111";
const ACTIVE_ID = "b2222222-2222-2222-2222-222222222222";
const JOB_ID = "0c9f7a3e-2f65-4f0a-8a2b-7d1e9c4b5a6f";

const CASES = [
  { question: "海祭是哪一族的祭儀?", mode: "hybrid", score: 0.92, passed: true },
  { question: "區域探索廳在幾樓?", mode: "sql", score: 0.4, passed: false },
];

function evalBlock(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    build_id: READY_ID,
    score: 0.66,
    passed: 1,
    failed: 1,
    fingerprint: "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    metrics: {},
    cases: CASES,
    ...overrides,
  };
}

// Route-aware GET stub: builds may answer DIFFERENT payloads per call (the
// terminal-invalidation tests prove the refetch renders the refreshed eval),
// and the job snapshot drives the watch states the silent stream cannot.
function stubQualityWorld({
  buildPages,
  jobSnapshot,
}: {
  buildPages: Build[][];
  jobSnapshot?: Job;
}) {
  let buildCall = 0;
  return vi.spyOn(api, "GET").mockImplementation(((path: string) => {
    if (path === "/projects/{project}/builds") {
      const page = buildPages[Math.min(buildCall, buildPages.length - 1)];
      buildCall += 1;
      return Promise.resolve({ data: { data: page, meta: META }, error: undefined });
    }
    if (path === "/jobs/{job_id}")
      return Promise.resolve({ data: { data: jobSnapshot, meta: META }, error: undefined });
    throw new Error(`unstubbed GET ${path}`);
  }) as never);
}

function stubEvalAccepted() {
  return vi.spyOn(api, "POST").mockResolvedValue({
    data: { data: { job_id: JOB_ID, status: "queued" }, meta: META },
    error: undefined,
  } as never);
}

function renderQuality() {
  return renderWithProviders(
    <Routes>
      <Route path="/p/:project/quality" element={<Quality />} />
    </Routes>,
    { route: projectRoute("acme", "quality") },
  );
}

afterEach(() => {
  stream.impl = () => new Promise<void>(() => {});
  vi.restoreAllMocks();
});

describe("Quality (品質/評測)", () => {
  it("runs eval on the default target (newest ready) with an Idempotency-Key", async () => {
    // the run wire: POST to the UXC1a eval endpoint, per-attempt random key
    // (a lost 202 must replay the original job id, not double-run), and the
    // DEFAULT target is the newest READY build by started_at — the build the
    // operator would activate next, never the API's arbitrary id-desc order
    const older = build({
      id: ACTIVE_ID,
      status: "ready",
      eval: null,
      started_at: "2026-07-01T00:00:00Z",
    });
    const newer = build({
      id: READY_ID,
      status: "ready",
      eval: null,
      started_at: "2026-07-02T00:00:00Z",
    });
    stubQualityWorld({
      buildPages: [[older, newer]],
      jobSnapshot: job({ job_id: JOB_ID, status: "queued", kind: "eval", build_id: READY_ID }),
    });
    const post = stubEvalAccepted();
    renderQuality();

    fireEvent.click(await screen.findByRole("button", { name: "開始評測" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledTimes(1);
      const [path, opts] = post.mock.calls[0] as [string, Record<string, unknown>];
      expect(path).toBe("/projects/{project}/builds/{build_id}/eval");
      const params = (opts as { params: { path: unknown; header: Record<string, string> } }).params;
      expect(params.path).toEqual({ project: "acme", build_id: READY_ID });
      expect(params.header["Idempotency-Key"]).toMatch(/[0-9a-f-]{36}/);
    });
    // the accepted job renders as live progress in operator words
    expect(await screen.findByRole("status")).toHaveTextContent("排隊中");
  });

  it("renders a 409 JOB_CONFLICT verbatim (the server's one-job-per-project rule)", async () => {
    stubQualityWorld({
      buildPages: [[build({ id: READY_ID, status: "ready", eval: null })]],
    });
    vi.spyOn(api, "POST").mockResolvedValue({
      data: undefined,
      error: {
        error: {
          code: "JOB_CONFLICT",
          message: "a job is already running for project acme",
          details: null,
          request_id: META.request_id,
        },
      },
    } as never);
    renderQuality();

    fireEvent.click(await screen.findByRole("button", { name: "開始評測" }));

    expect(
      await screen.findByText(/無法開始評測:a job is already running for project acme/),
    ).toBeInTheDocument();
  });

  it("locks the run button while the watched eval job is still in flight", async () => {
    // one eval at a time from this page: a second click would 409 with no job
    // id to watch — the gate prevents the dead end instead of racing the server
    stubQualityWorld({
      buildPages: [[build({ id: READY_ID, status: "ready", eval: null })]],
      jobSnapshot: job({
        job_id: JOB_ID,
        status: "running",
        kind: "eval",
        build_id: READY_ID,
        progress: 0.5,
      }),
    });
    stubEvalAccepted();
    renderQuality();

    const run = await screen.findByRole("button", { name: "開始評測" });
    fireEvent.click(run);

    expect(await screen.findByRole("status")).toHaveTextContent("評測中");
    expect(screen.getByRole("button", { name: "開始評測" })).toBeDisabled();
    // a live eval is cancellable (the cooperative §22 cancel)
    expect(screen.getByRole("button", { name: "取消評測" })).toBeEnabled();
  });

  it("a terminal job refetches the builds read and renders the fresh per-case verdicts", async () => {
    // THE wire this task exists for: the report lands in builds.eval when the
    // JOB completes, so the terminal snapshot invalidates ["builds"] — the
    // SAME read the Overview checklist's step ③ projects from — and the
    // refreshed block feeds the table with no new coupling
    const before = build({ id: READY_ID, status: "ready", eval: null });
    const after = build({ id: READY_ID, status: "ready", eval: evalBlock() });
    const getSpy = stubQualityWorld({
      buildPages: [[before], [after]],
      jobSnapshot: job({
        job_id: JOB_ID,
        status: "done",
        kind: "eval",
        build_id: READY_ID,
        progress: 1,
      }),
    });
    stubEvalAccepted();
    renderQuality();

    expect(await screen.findByText("此版本還沒有評測結果。")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "開始評測" }));

    // job done → builds invalidated → second page (with the report) renders
    expect(await screen.findByText("海祭是哪一族的祭儀?")).toBeInTheDocument();
    expect(screen.getByText("通過")).toBeInTheDocument();
    expect(screen.getByText("未過")).toBeInTheDocument();
    expect(screen.getByText(/總分:0\.66/)).toBeInTheDocument();
    const buildCalls = (getSpy.mock.calls as unknown as [string][]).filter(
      ([path]) => path === "/projects/{project}/builds",
    );
    expect(buildCalls.length).toBeGreaterThanOrEqual(2);
    // the gate reopens once the job is terminal
    await waitFor(() => expect(screen.getByRole("button", { name: "開始評測" })).toBeEnabled());
  });

  it("a failed eval job surfaces the job error verbatim", async () => {
    // drift refusals ("eval inputs changed since accepted"), missing golden
    // sets etc. terminalize the JOB — the page must relay that stated failure,
    // not leave a spinner or pretend the eval never happened
    stubQualityWorld({
      buildPages: [[build({ id: READY_ID, status: "ready", eval: null })]],
      jobSnapshot: job({
        job_id: JOB_ID,
        status: "failed",
        kind: "eval",
        build_id: READY_ID,
        error: {
          code: "INTERNAL",
          message: "golden set not found: eval/golden.yaml",
          details: null,
          request_id: META.request_id,
        },
      }),
    });
    stubEvalAccepted();
    renderQuality();

    fireEvent.click(await screen.findByRole("button", { name: "開始評測" }));

    expect(
      await screen.findByText(/評測失敗:golden set not found: eval\/golden\.yaml/),
    ).toBeInTheDocument();
  });

  it("renders per-case verdicts as words with the question as the row identity", async () => {
    stubQualityWorld({
      buildPages: [[build({ id: READY_ID, status: "ready", eval: evalBlock() })]],
    });
    renderQuality();

    expect(await screen.findByText("海祭是哪一族的祭儀?")).toBeInTheDocument();
    expect(screen.getByText("區域探索廳在幾樓?")).toBeInTheDocument();
    expect(screen.getByText("0.92")).toBeInTheDocument();
    expect(screen.getByText("0.40")).toBeInTheDocument();
    expect(screen.getByText("通過")).toBeInTheDocument();
    expect(screen.getByText("未過")).toBeInTheDocument();
    expect(screen.getByText(/通過 1 題/)).toBeInTheDocument();
    expect(screen.getByText(/未過 1 題/)).toBeInTheDocument();
  });

  it("withholds the table on a malformed cases list instead of dropping rows", async () => {
    // all-or-nothing: a partially-rendered verdict table would silently drop
    // cases — the exact false-green an eval gate must not have. The raw block
    // stays readable in the 進階 fold.
    stubQualityWorld({
      buildPages: [
        [
          build({
            id: READY_ID,
            status: "ready",
            eval: evalBlock({ cases: [CASES[0], { question: "斷掉的案例" }] }),
          }),
        ],
      ],
    });
    renderQuality();

    expect(await screen.findByText(/無法解讀逐題結果/)).toBeInTheDocument();
    // NO partial table: not even the well-formed first case renders as a row
    expect(screen.queryByText("海祭是哪一族的祭儀?")).not.toBeInTheDocument();
    expect(screen.getByText(/進階:原始評測資料/)).toBeInTheDocument();
  });

  it("says so when the selected build has no eval yet", async () => {
    stubQualityWorld({
      buildPages: [[build({ id: READY_ID, status: "ready", eval: null })]],
    });
    renderQuality();

    expect(await screen.findByText("此版本還沒有評測結果。")).toBeInTheDocument();
  });

  it("offers only evaluable builds and guides to 建置 when there are none", async () => {
    // the eval binding scores "a ready candidate or the active build"
    // (core/eval/runner.py) — building/failed builds would be refused by the
    // job, so they are not offered; none at all is guidance, not an error
    stubQualityWorld({
      buildPages: [
        [
          build({ id: READY_ID, status: "building", eval: null }),
          build({ id: ACTIVE_ID, status: "failed", eval: null }),
        ],
      ],
    });
    renderQuality();

    expect(await screen.findByText(/還沒有可評測的版本/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "去建置" })).toHaveAttribute(
      "href",
      expect.stringContaining("import"),
    );
    expect(screen.queryByRole("button", { name: "開始評測" })).not.toBeInTheDocument();
  });

  it("says so when the accepted job's snapshot cannot be loaded (no silent locked button)", async () => {
    // the run gate is fail-closed on an unknown job state — but a disabled
    // button with no explanation is a silent dead end: the snapshot error must
    // render, with a retry (Rule 12; reviewer catch on this task)
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path === "/projects/{project}/builds")
        return Promise.resolve({
          data: { data: [build({ id: READY_ID, status: "ready", eval: null })], meta: META },
          error: undefined,
        });
      return Promise.resolve({
        data: undefined,
        error: {
          error: {
            code: "STORE_UNAVAILABLE",
            message: "jobs store down",
            details: null,
            request_id: META.request_id,
          },
        },
      });
    }) as never);
    stubEvalAccepted();
    renderQuality();

    fireEvent.click(await screen.findByRole("button", { name: "開始評測" }));

    expect(await screen.findByText(/無法載入評測工作狀態:jobs store down/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新整理狀態" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "開始評測" })).toBeDisabled();
  });

  it("fails loud when the builds read fails — no run button over an unknown world", async () => {
    vi.spyOn(api, "GET").mockResolvedValue({
      data: undefined,
      error: {
        error: {
          code: "STORE_UNAVAILABLE",
          message: "down",
          details: null,
          request_id: META.request_id,
        },
      },
    } as never);
    renderQuality();

    expect(await screen.findByText(/無法載入版本:down/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "開始評測" })).not.toBeInTheDocument();
  });

  it("invalidates on a TERMINAL STREAM EVENT even when the snapshot never turns terminal", async () => {
    // the stream can see the job finish while the snapshot read is stuck: if
    // the post-close refetch fails, react-query retains the stale RUNNING
    // snapshot forever — a snapshot-only terminal guard would then never
    // refresh the builds read and the fresh report never renders (Codex #82
    // triage 3). Revert-probe: drop streamTerminal from the invalidation
    // predicate and the table below never appears.
    stream.impl = async (_jobId, opts) => {
      (opts as StreamOpts).onFrame({
        data: JSON.stringify({
          job_id: JOB_ID,
          status: "done",
          step: null,
          progress: 1,
          message: null,
          ts: "2026-07-01T00:01:00Z",
        }),
      });
      await new Promise<void>(() => {}); // never closes — no close-refetch path
    };
    stubQualityWorld({
      buildPages: [
        [build({ id: READY_ID, status: "ready", eval: null })],
        [build({ id: READY_ID, status: "ready", eval: evalBlock() })],
      ],
      // the snapshot stays non-terminal (the stale-read cell under test)
      jobSnapshot: job({
        job_id: JOB_ID,
        status: "running",
        kind: "eval",
        build_id: READY_ID,
        progress: 0.9,
      }),
    });
    stubEvalAccepted();
    renderQuality();

    fireEvent.click(await screen.findByRole("button", { name: "開始評測" }));

    // the event-terminal invalidation refreshed the builds read → report renders
    expect(await screen.findByText("海祭是哪一族的祭儀?")).toBeInTheDocument();
  });

  it("a second eval run on the same mount refreshes ITS report (stale prior-job event gated)", async () => {
    // useJobStream retains job A's terminal event across the jobId flip to
    // job B until its reset effect runs — the invalidation effect's first
    // jobId=B render still sees it. Without gating streamTerminal on
    // event.job_id === jobId, B is prematurely marked settled and B's REAL
    // completion is skipped by the once-per-job guard: the builds read never
    // refreshes and B's report never renders (reviewer catch on triage 3).
    // Revert-probe: drop the job_id gate and the second report never appears.
    // The builds read models WORLD STATE (which evals have actually finished),
    // never a call count: in the buggy version the premature settle at B-start
    // fires an EXTRA builds refetch, and a call-counted page list would serve
    // the "after B" world to it — making the probe pass with the gate removed
    // (a false-green revert-probe, the first version of this test).
    const SECOND_RUN_CASES = [
      { question: "第二輪:黑潮對台灣有什麼影響?", mode: "semantic", score: 0.8, passed: true },
    ];
    const JOB_B = "1d8e8b4f-3a76-4b1b-9c3d-8e2f0a1b2c3e";
    let aFinished = false;
    let bFinished = false;
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path === "/projects/{project}/builds") {
        const evalState = bFinished
          ? evalBlock({ cases: SECOND_RUN_CASES })
          : aFinished
            ? evalBlock()
            : null;
        return Promise.resolve({
          data: {
            data: [build({ id: READY_ID, status: "ready", eval: evalState })],
            meta: META,
          },
          error: undefined,
        });
      }
      if (path === "/jobs/{job_id}")
        // snapshots stay non-terminal — settlement rides the stream events
        return Promise.resolve({
          data: {
            data: job({ job_id: JOB_ID, status: "running", kind: "eval", build_id: READY_ID }),
            meta: META,
          },
          error: undefined,
        });
      throw new Error(`unstubbed GET ${path}`);
    }) as never);
    // job A's stream reports done immediately; job B's terminal event is HELD
    // until the test releases it, so at the jobId A→B flip the retained A
    // event is the ONLY terminal event in sight — the exact stale-frame window
    let releaseB: () => void = () => {};
    const bGate = new Promise<void>((resolve) => {
      releaseB = resolve;
    });
    stream.impl = async (jobId, opts) => {
      if (jobId !== JOB_ID) await bGate;
      if (jobId === JOB_ID) aFinished = true;
      else bFinished = true;
      (opts as StreamOpts).onFrame({
        data: JSON.stringify({
          job_id: jobId,
          status: "done",
          step: null,
          progress: 1,
          message: null,
          ts: "2026-07-01T00:01:00Z",
        }),
      });
      await new Promise<void>(() => {}); // never closes — no close-refetch path
    };
    const post = vi
      .spyOn(api, "POST")
      .mockResolvedValueOnce({
        data: { data: { job_id: JOB_ID, status: "queued" }, meta: META },
        error: undefined,
      } as never)
      .mockResolvedValueOnce({
        data: { data: { job_id: JOB_B, status: "queued" }, meta: META },
        error: undefined,
      } as never);
    renderQuality();

    // run A settles via its own stream event → first report renders
    fireEvent.click(await screen.findByRole("button", { name: "開始評測" }));
    expect(await screen.findByText("海祭是哪一族的祭儀?")).toBeInTheDocument();

    // run B on the same mount — job A's terminal event is still retained at
    // the moment jobId flips; B must settle on ITS OWN event, not A's
    const run = screen.getByRole("button", { name: "開始評測" });
    await waitFor(() => expect(run).toBeEnabled());
    fireEvent.click(run);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));

    // only NOW does job B finish: in the buggy version B was already marked
    // settled by A's stale event (with no B result in the world), so this real
    // completion is skipped by the once-per-job guard and the second report
    // never renders
    releaseB();
    expect(await screen.findByText("第二輪:黑潮對台灣有什麼影響?")).toBeInTheDocument();
  });

  it("surfaces a FAILED snapshot refetch even when stale job data still renders", async () => {
    // stream closed (job finished server-side) but the refetch for the
    // terminal-only fields fails: the panel keeps rendering the stale
    // running snapshot — that broken read must not be silent (no error line,
    // no recovery) just because data exists (Codex #82 triage 3)
    let jobCalls = 0;
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path === "/projects/{project}/builds")
        return Promise.resolve({
          data: { data: [build({ id: READY_ID, status: "ready", eval: null })], meta: META },
          error: undefined,
        });
      if (path === "/jobs/{job_id}") {
        jobCalls += 1;
        if (jobCalls === 1)
          return Promise.resolve({
            data: {
              data: job({ job_id: JOB_ID, status: "running", kind: "eval", build_id: READY_ID }),
              meta: META,
            },
            error: undefined,
          });
        return Promise.resolve({
          data: undefined,
          error: {
            error: {
              code: "STORE_UNAVAILABLE",
              message: "jobs store down",
              details: null,
              request_id: META.request_id,
            },
          },
        });
      }
      throw new Error(`unstubbed GET ${path}`);
    }) as never);
    stream.impl = () => Promise.resolve(); // closes immediately → triggers the refetch
    stubEvalAccepted();
    renderQuality();

    fireEvent.click(await screen.findByRole("button", { name: "開始評測" }));

    // the stale panel still shows the last-known state…
    expect(await screen.findByRole("status")).toHaveTextContent("評測中");
    // …but the broken refetch is stated, with a retry
    expect(await screen.findByText(/無法載入評測工作狀態:jobs store down/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新整理狀態" })).toBeEnabled();
  });

  it("honors a ?build= deep link as the initial selection (the Overview step-③ hand-off)", async () => {
    // the Overview CTA names the build whose MISSING score blocks step ③
    // (active ?? newest ready) — this page's own default (newest ready) can
    // be a different build, so the link's target must win or the operator
    // evaluates the wrong build and the checklist stays blocked (Codex #82)
    const newerReady = build({
      id: READY_ID,
      status: "ready",
      eval: null,
      started_at: "2026-07-02T00:00:00Z",
    });
    const activeNoEval = build({
      id: ACTIVE_ID,
      status: "active",
      eval: null,
      started_at: "2026-07-01T00:00:00Z",
    });
    stubQualityWorld({ buildPages: [[newerReady, activeNoEval]] });
    renderWithProviders(
      <Routes>
        <Route path="/p/:project/quality" element={<Quality />} />
      </Routes>,
      { route: `${projectRoute("acme", "quality")}?build=${ACTIVE_ID}` },
    );

    // the deep-linked ACTIVE build is selected, not the newest-ready default
    expect(((await screen.findByLabelText("選擇版本")) as HTMLSelectElement).value).toBe(ACTIVE_ID);
  });

  it("pins the evaluated build: a newer ready build landing mid-run must not steal the scope", async () => {
    // running from the IMPLICIT default selection must pin the target: the
    // terminal builds refetch re-derives the default, and a newer ready build
    // that landed mid-run would otherwise become `selected` — the results
    // section would show THAT build's "no eval yet" instead of the just-
    // computed report (Codex #82). Revert-probe: drop the onRun pin and the
    // table below never renders.
    const evaluated = build({
      id: READY_ID,
      status: "ready",
      eval: null,
      started_at: "2026-07-01T00:00:00Z",
    });
    const evaluatedWithReport = build({
      id: READY_ID,
      status: "ready",
      eval: evalBlock(),
      started_at: "2026-07-01T00:00:00Z",
    });
    const newerMidRun = build({
      id: ACTIVE_ID,
      status: "ready",
      eval: null,
      started_at: "2026-07-02T00:00:00Z",
    });
    stubQualityWorld({
      buildPages: [[evaluated], [evaluatedWithReport, newerMidRun]],
      jobSnapshot: job({
        job_id: JOB_ID,
        status: "done",
        kind: "eval",
        build_id: READY_ID,
        progress: 1,
      }),
    });
    stubEvalAccepted();
    renderQuality();

    // run from the DEFAULT selection (no explicit pick)
    fireEvent.click(await screen.findByRole("button", { name: "開始評測" }));

    // the terminal refetch serves a NEWER ready build — the scope must stay
    // on the evaluated one and render its fresh report
    expect(await screen.findByText("海祭是哪一族的祭儀?")).toBeInTheDocument();
    expect((screen.getByLabelText("選擇版本") as HTMLSelectElement).value).toBe(READY_ID);
  });

  it("falls back to the default target when the picked build leaves the evaluable set", async () => {
    // the selection DERIVES from (picked id, builds) each render — a picked
    // build archived by a refetch must not linger as a stale scope (class 17)
    const newer = build({
      id: READY_ID,
      status: "ready",
      eval: null,
      started_at: "2026-07-02T00:00:00Z",
    });
    const older = build({
      id: ACTIVE_ID,
      status: "ready",
      eval: null,
      started_at: "2026-07-01T00:00:00Z",
    });
    const olderArchived = build({
      id: ACTIVE_ID,
      status: "archived",
      eval: null,
      started_at: "2026-07-01T00:00:00Z",
    });
    stubQualityWorld({
      buildPages: [
        [newer, older],
        [newer, olderArchived],
      ],
      jobSnapshot: job({
        job_id: JOB_ID,
        status: "done",
        kind: "eval",
        build_id: ACTIVE_ID,
        progress: 1,
      }),
    });
    stubEvalAccepted();
    renderQuality();

    // pick the OLDER build explicitly, run eval on it
    const select = await screen.findByLabelText("選擇版本");
    fireEvent.change(select, { target: { value: ACTIVE_ID } });
    fireEvent.click(screen.getByRole("button", { name: "開始評測" }));

    // terminal job → refetch → the picked build is now archived (not
    // evaluable) → the selection falls back to the newest ready build
    await waitFor(() => {
      expect((screen.getByLabelText("選擇版本") as HTMLSelectElement).value).toBe(READY_ID);
    });
  });
});
