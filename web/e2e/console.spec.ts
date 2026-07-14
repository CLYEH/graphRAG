import { expect, test } from "@playwright/test";

// The e2e server runs only Vite (no backend), so GET /projects is stubbed at
// the network layer to drive the real shell/switcher/routing without an API.
const META = {
  request_id: "00000000-0000-0000-0000-000000000000",
  build_id: null,
  elapsed_ms: 1,
  next_cursor: null,
};

function projectsResponse(names: string[]) {
  return {
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      data: names.map((name) => ({
        name,
        display_name: null,
        description: null,
        config: {},
        created_at: "2026-07-01T00:00:00Z",
      })),
      meta: META,
    }),
  };
}

function healthResponse() {
  return {
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      data: {
        status: "healthy",
        active_build_id: null,
        counts: { sources: 1, documents: 3, chunks: 12, entities: 5, relations: 4 },
        pending_review: 0,
        drift: null,
        warnings: [],
      },
      meta: META,
    }),
  };
}

function buildsResponse() {
  return {
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      data: [
        {
          id: "b1111111-aaaa-4aaa-8aaa-000000000001",
          project: "acme",
          status: "active",
          config_hash: null,
          source_hash: null,
          started_at: "2026-07-01T00:00:00Z",
          finished_at: "2026-07-01T00:05:00Z",
          activated_at: "2026-07-01T00:06:00Z",
          metrics: null,
          eval: null,
        },
      ],
      meta: META,
    }),
  };
}

test("console shell loads with the project switcher and section nav", async ({ page }) => {
  await page.route("**/projects*", (route) => route.fulfill(projectsResponse(["acme", "beta"])));
  await page.route("**/projects/*/health", (route) => route.fulfill(healthResponse()));
  await page.route("**/projects/*/sources*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data: [], meta: META }),
    }),
  );
  await page.route("**/projects/*/builds*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data: [], meta: META }),
    }),
  );
  await page.goto("/");

  // the root redirects into the first project's 總覽 (UXA2) — the page that
  // says what to do next; a fresh project points at step ①
  await expect(page.getByRole("heading", { name: "總覽" })).toBeVisible();
  await expect(page.getByText(/尚未開始/)).toBeVisible();
  await expect(page.getByRole("combobox", { name: /project/i })).toHaveValue("acme");
  for (const label of ["總覽", "匯入", "建置", "檢視", "清洗", "圖譜", "審核", "問答", "診斷"]) {
    await expect(page.getByRole("link", { name: label, exact: true })).toBeVisible();
  }

  // Health stays the diagnostics page, one click away
  await page.getByRole("link", { name: "診斷" }).click();
  await expect(page.getByRole("heading", { name: "專案健康(診斷)" })).toBeVisible();
  await expect(page.getByRole("status")).toHaveText("健康");
});

test("the overview walks the setup checklist and activates a build", async ({ page }) => {
  await page.route("**/projects*", (route) => route.fulfill(projectsResponse(["acme"])));
  await page.route("**/projects/*/health", (route) => route.fulfill(healthResponse()));
  await page.route("**/projects/*/sources*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: [
          {
            id: "s1111111-aaaa-4aaa-8aaa-000000000001",
            project: "acme",
            kind: "text",
            uri: "file:///data/corpus",
            metadata: {},
            created_at: "2026-07-01T00:00:00Z",
          },
        ],
        meta: META,
      }),
    }),
  );
  const readyBuild = {
    id: "b1111111-aaaa-4aaa-8aaa-000000000001",
    project: "acme",
    status: "ready",
    config_hash: null,
    source_hash: null,
    started_at: "2026-07-01T00:00:00Z",
    finished_at: "2026-07-01T00:05:00Z",
    activated_at: null,
    metrics: null,
    eval: { score: 1.0, passed: 3, failed: 0 },
  };
  await page.route("**/projects/*/builds?*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data: [readyBuild], meta: META }),
    }),
  );
  let activatePath = "";
  let activateKey = "";
  await page.route("**/builds/*/activate", (route) => {
    activatePath = new URL(route.request().url()).pathname;
    activateKey = route.request().headers()["idempotency-key"] ?? "";
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data: { ...readyBuild, status: "active" }, meta: META }),
    });
  });

  await page.goto("/");
  // steps ①-③ done, step ④ offers the activate button behind a confirm
  await expect(page.getByText(/已建置,尚未上線/)).toBeVisible();
  await page.getByRole("button", { name: "上線這個版本" }).click();
  expect(activatePath).toBe("");
  await page.getByRole("button", { name: "確定上線" }).click();
  await expect.poll(() => activatePath).toMatch(/\/builds\/[^/]+\/activate$/);
  expect(activateKey).toMatch(/[0-9a-f-]{36}/);
});

function mergeCandidatesResponse() {
  return {
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      data: [
        {
          id: "c1111111-aaaa-4aaa-8aaa-000000000001",
          project: "acme",
          build_id: "b1111111-aaaa-4aaa-8aaa-000000000001",
          left_entity_id: "e1111111-aaaa-4aaa-8aaa-000000000001",
          right_entity_id: "e2222222-aaaa-4aaa-8aaa-000000000002",
          score: 0.91,
          status: "pending",
          decision: null,
          decided_by: null,
          decided_at: null,
          reason: null,
          impact: null,
          left_snapshot: { name: "國立海洋科技博物館", type: "FACILITY" },
          right_snapshot: { name: "海科館", type: "FACILITY" },
        },
      ],
      meta: META,
    }),
  };
}

test("the jobs section shows the pipeline runs table", async ({ page }) => {
  await page.route("**/projects*", (route) => route.fulfill(projectsResponse(["acme"])));
  await page.route("**/projects/*/health", (route) => route.fulfill(healthResponse()));
  await page.route("**/projects/*/builds*", (route) => route.fulfill(buildsResponse()));
  await page.goto("/");

  await page.getByRole("link", { name: "建置" }).click();
  await expect(page.getByRole("heading", { name: "建置與工作" })).toBeVisible();
  await expect(page.getByText(/追蹤工作/)).toBeVisible();
  await expect(page.getByText("上線中")).toBeVisible(); // a run row badge
});

test("the review section shows the queue and records a decision", async ({ page }) => {
  await page.route("**/projects*", (route) => route.fulfill(projectsResponse(["acme"])));
  await page.route("**/projects/*/health", (route) => route.fulfill(healthResponse()));
  await page.route("**/projects/*/merge-candidates*", (route) =>
    route.fulfill(mergeCandidatesResponse()),
  );

  let approvePath = "";
  await page.route("**/merge-candidates/*/approve", (route) => {
    approvePath = new URL(route.request().url()).pathname;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          id: "c1111111-aaaa-4aaa-8aaa-000000000001",
          project: "acme",
          build_id: "b1111111-aaaa-4aaa-8aaa-000000000001",
          left_entity_id: "e1111111-aaaa-4aaa-8aaa-000000000001",
          right_entity_id: "e2222222-aaaa-4aaa-8aaa-000000000002",
          score: 0.91,
          status: "approved",
          decision: "approve",
          decided_by: "console",
          decided_at: "2026-07-02T00:00:00Z",
          reason: null,
          impact: null,
          left_snapshot: null,
          right_snapshot: null,
        },
        meta: META,
      }),
    });
  });

  // the case card's context fetches (UXA1) — empty graph keeps the flow honest
  await page.route("**/projects/*/graph/subgraph*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data: { nodes: [], edges: [] }, meta: META }),
    }),
  );

  await page.goto("/");
  await page.getByRole("link", { name: "審核" }).click();

  await expect(page.getByRole("heading", { name: "實體審核" })).toBeVisible();
  // the decision surface leads with the snapshot NAMES, never the id prefix (UXA1)
  await expect(page.getByText("國立海洋科技博物館")).toBeVisible();
  await expect(page.getByText("c1111111")).not.toBeVisible();

  // approve is §17-terminal: the first click only opens the confirm — nothing
  // posts until the explicit second step
  await page.getByRole("button", { name: "是,合併" }).click();
  expect(approvePath).toBe("");
  await page.getByRole("button", { name: "確定合併" }).click();
  // the approve verb must reach its own path (not reject/defer, not a body verb)
  await expect.poll(() => approvePath).toMatch(/\/merge-candidates\/[^/]+\/approve$/);
});

function queryResponse() {
  return {
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      data: {
        mode: "hybrid",
        build_id: "b1111111-aaaa-4aaa-8aaa-000000000001",
        results: [
          {
            result_type: "chunk",
            id: "d1111111-aaaa-4aaa-8aaa-000000000001",
            title: null,
            text: "the answer is 42",
            score: 0.9,
            confidence: null,
            source_refs: [{ source_type: "document", id: "aaaaaaaa-1111-2222-3333-444444444444" }],
          },
        ],
        graph_context: null,
        warnings: [],
        debug: null,
      },
      meta: META,
    }),
  };
}

test("the playground runs a query and shows results", async ({ page }) => {
  await page.route("**/projects*", (route) => route.fulfill(projectsResponse(["acme"])));
  await page.route("**/projects/*/health", (route) => route.fulfill(healthResponse()));
  let queryPath = "";
  await page.route("**/projects/*/query/*", (route) => {
    queryPath = new URL(route.request().url()).pathname;
    return route.fulfill(queryResponse());
  });
  await page.goto("/");

  await page.getByRole("link", { name: "問答" }).click();
  await expect(page.getByRole("heading", { name: "問答測試" })).toBeVisible();

  await page.getByLabel("問題", { exact: true }).fill("what is the answer?");
  await page.getByRole("button", { name: /run query/i }).click();

  await expect(page.getByText("the answer is 42")).toBeVisible();
  // the default mode routes to the hybrid endpoint
  await expect.poll(() => queryPath).toMatch(/\/query\/hybrid$/);
});

test("console shows an empty state when there are no projects", async ({ page }) => {
  await page.route("**/projects*", (route) => route.fulfill(projectsResponse([])));
  await page.goto("/");

  await expect(page.getByText(/no projects yet/i)).toBeVisible();
});

function sourceResponse() {
  return {
    status: 201,
    contentType: "application/json",
    body: JSON.stringify({
      data: {
        id: "50000000-0000-0000-0000-000000000000",
        kind: "text",
        uri: "file:///data/corpus/",
        metadata: {},
        added_at: "2026-07-01T00:00:00Z",
      },
      meta: META,
    }),
  };
}

function jobAcceptedResponse() {
  return {
    status: 202,
    contentType: "application/json",
    body: JSON.stringify({
      data: { job_id: "0c9f7a3e-2f65-4f0a-8a2b-7d1e9c4b5a6f", status: "queued" },
      meta: META,
    }),
  };
}

function jobResponse() {
  return {
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      data: {
        job_id: "0c9f7a3e-2f65-4f0a-8a2b-7d1e9c4b5a6f",
        status: "running",
        kind: "build",
        project: "acme",
        build_id: null,
        step: "graph",
        progress: 0.5,
        message: null,
        error: null,
        created_at: "2026-07-01T00:00:00Z",
        finished_at: null,
      },
      meta: META,
    }),
  };
}

test("the import section registers a source and triggers a build", async ({ page }) => {
  await page.route("**/projects*", (route) => route.fulfill(projectsResponse(["acme"])));
  await page.route("**/projects/*/health", (route) => route.fulfill(healthResponse()));
  let sourceBody = "";
  await page.route("**/projects/*/sources*", (route) => {
    if (route.request().method() === "POST") {
      sourceBody = route.request().postData() ?? "";
      return route.fulfill(sourceResponse());
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data: [], meta: META }),
    });
  });
  let buildPath = "";
  let buildBody: string | null = "unset";
  await page.route("**/projects/*/build", (route) => {
    buildPath = new URL(route.request().url()).pathname;
    buildBody = route.request().postData();
    return route.fulfill(jobAcceptedResponse());
  });
  await page.route("**/jobs/*/events", (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: "" }),
  );
  await page.route("**/jobs/*", (route) => route.fulfill(jobResponse()));
  await page.goto("/");

  await page.getByRole("link", { name: "匯入", exact: true }).click();
  await expect(page.getByRole("heading", { name: "匯入資料" })).toBeVisible();

  // register a text source by file:// uri (no byte upload — the contract models a
  // uri reference, and text is the ingest-wired default kind)
  await page.getByLabel("uri").fill("file:///data/corpus/");
  await page.getByRole("button", { name: "登記來源" }).click();
  await expect.poll(() => sourceBody).toContain("file:///data/corpus/");

  // trigger a full build and watch the accepted job
  await page.getByRole("button", { name: "開始建置" }).click();
  await expect(page.getByText(/建置已排入佇列/)).toBeVisible();
  // the build trigger hits its own path with an EMPTY body — source_ids/reason are
  // 400-rejected by presence, so nothing may ride along (BA2e-1)
  await expect.poll(() => buildPath).toMatch(/\/build$/);
  expect(buildBody).toBeNull();
});

function documentsResponse(rows: unknown[], meta: Record<string, unknown> = {}) {
  return {
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      data: rows,
      meta: {
        request_id: "00000000-0000-0000-0000-000000000000",
        build_id: "b1111111-aaaa-4aaa-8aaa-000000000001",
        elapsed_ms: 1,
        schema_version: "0.5",
        ...meta,
      },
    }),
  };
}

test("inspect browses the active build's documents and opens a detail-only field", async ({
  page,
}) => {
  await page.route("**/projects*", (route) => route.fulfill(projectsResponse(["acme"])));
  await page.route("**/projects/*/health", (route) => route.fulfill(healthResponse()));

  // the list request the page issues — captured so the flow can assert what it did NOT
  // send: the frozen op params expose sort/filter, but the API 400s any filter[...] and
  // (on chunks) EVERY explicit sort, so the client must send neither
  let listUrl: string | null = null;
  await page.route("**/projects/*/documents?*", (route) => {
    listUrl = route.request().url();
    return route.fulfill(
      documentsResponse([
        {
          id: "d1111111-aaaa-4aaa-8aaa-000000000001",
          build_id: "b1111111-aaaa-4aaa-8aaa-000000000001",
          source_uri: "file:///data/corpus/a.txt",
          mime: "text/plain",
          status: "ingested",
          ingested_at: "2026-07-13T04:00:00Z",
        },
      ]),
    );
  });
  // `raw` comes back on the DETAIL read only — the list omits the key entirely, which
  // is what a row click is for
  await page.route("**/projects/*/documents/*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          id: "d1111111-aaaa-4aaa-8aaa-000000000001",
          build_id: "b1111111-aaaa-4aaa-8aaa-000000000001",
          source_uri: "file:///data/corpus/a.txt",
          raw: "Ada Lovelace worked at the Analytical Engine.",
        },
        meta: {
          request_id: "00000000-0000-0000-0000-000000000000",
          build_id: "b1111111-aaaa-4aaa-8aaa-000000000001",
          elapsed_ms: 1,
          schema_version: "0.5",
        },
      }),
    }),
  );

  await page.goto("/");
  await page.getByRole("link", { name: "檢視" }).click();
  await expect(page.getByRole("heading", { name: /^inspect$/i })).toBeVisible();

  await expect(page.getByRole("button", { name: /a\.txt/ })).toBeVisible();
  await expect.poll(() => listUrl).toContain("/documents?");
  expect(listUrl).not.toContain("sort");
  expect(listUrl).not.toContain("filter");

  await page.getByRole("button", { name: /a\.txt/ }).click();
  await expect(page.getByText("Ada Lovelace worked at the Analytical Engine.")).toBeVisible();
});

test("clean previews pasted text and saves chunking by spreading the config", async ({ page }) => {
  // The two FE2 invariants, end-to-end in a real browser: the preview call names
  // exactly one source with omitted knobs left OUT of the body, and the save PATCH
  // carries the project's OTHER config blocks (the column is replaced server-side —
  // a naive {config:{chunking}} would wipe ontology).
  let previewBody = "";
  let patchBody = "";
  // NB: playwright's `*` does not cross `/` — the preview path needs its own route
  await page.route("**/projects/*/clean/preview", (route, request) => {
    previewBody = request.postData() ?? "";
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          chunks: [
            { ordinal: 0, text: "alpha beta", start_offset: 0, end_offset: 10, token_count: 2 },
          ],
        },
        meta: {
          request_id: "00000000-0000-0000-0000-000000000000",
          build_id: null,
          elapsed_ms: 1,
        },
      }),
    });
  });
  await page.route("**/projects*", (route) => route.fulfill(projectsResponse(["acme"])));
  await page.route("**/projects/*/health", (route) => route.fulfill(healthResponse()));
  await page.route("**/projects/acme", (route, request) => {
    if (request.method() === "PATCH") {
      patchBody = request.postData() ?? "";
    }
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          name: "acme",
          display_name: null,
          description: null,
          config: {
            ontology: { entity_types: ["PERSON"] },
            chunking: { max_chars: 500, overlap: 50 },
          },
          created_at: "2026-07-01T00:00:00Z",
        },
        meta: {
          request_id: "00000000-0000-0000-0000-000000000000",
          build_id: null,
          elapsed_ms: 1,
        },
      }),
    });
  });

  await page.goto("/");
  await page.getByRole("link", { name: "清洗" }).click();
  await expect(page.getByRole("heading", { name: "清洗(切塊預覽)" })).toBeVisible();

  await page.locator("textarea").fill("alpha beta gamma delta");
  await page.getByRole("button", { name: "預覽" }).click();
  await expect(page.getByText("alpha beta", { exact: true })).toBeVisible();
  expect(JSON.parse(previewBody)).toEqual({ text: "alpha beta gamma delta" });

  await page.getByRole("button", { name: "儲存 500/50 到專案設定" }).click();
  await expect(page.getByText(/已儲存 500\/50/)).toBeVisible();
  const patched = JSON.parse(patchBody);
  expect(patched.config.ontology).toEqual({ entity_types: ["PERSON"] });
  expect(patched.config.chunking).toEqual({ max_chars: 500, overlap: 50 });
});

test("graph explorer walks a neighborhood and opens an edge's evidence", async ({ page }) => {
  // The §10.2 edge fields (type/confidence/evidence/來源/review_status) come from
  // the relation DETAIL fetch — the list frames omit evidence, so the click must
  // travel to the server and back in a real browser too.
  const E1 = "e1000000-0000-4000-8000-000000000001";
  const E2 = "e2000000-0000-4000-8000-000000000002";
  const R1 = "r1000000-0000-4000-8000-000000000001";
  const meta = {
    request_id: "00000000-0000-0000-0000-000000000000",
    build_id: "b1",
    elapsed_ms: 1,
    next_cursor: null,
  };
  await page.route("**/projects/*/graph/subgraph*", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          nodes: [
            { id: E1, type: "PERSON", label: "Ada Lovelace", properties: {} },
            { id: E2, type: "PERSON", label: "Charles Babbage", properties: {} },
          ],
          edges: [{ id: R1, src: E1, dst: E2, type: "WORKS_WITH", properties: {} }],
        },
        meta,
      }),
    }),
  );
  await page.route("**/projects/*/entities*", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        data: [
          {
            id: E1,
            build_id: "b1",
            type: "PERSON",
            canonical_name: "Ada Lovelace",
            entity_key: "person:ada",
            status: "active",
            attributes: {},
          },
        ],
        meta,
      }),
    }),
  );
  await page.route("**/projects/*/entities/*", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          id: E1,
          build_id: "b1",
          type: "PERSON",
          canonical_name: "Ada Lovelace",
          entity_key: "person:ada",
          status: "active",
          attributes: {},
        },
        meta,
      }),
    }),
  );
  await page.route("**/projects/*/relations/*", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          id: R1,
          build_id: "b1",
          src_entity_id: E1,
          dst_entity_id: E2,
          type: "WORKS_WITH",
          status: "active",
          review_status: "approved",
          created_by: "pipeline",
          confidence: 0.87,
          evidence: [
            {
              id: "ev000000-0000-4000-8000-000000000001",
              evidence_type: "chunk",
              quote: "Ada worked with Charles on the Engine.",
              source_uri: "file:///corpus/ada.txt",
            },
          ],
        },
        meta,
      }),
    }),
  );
  await page.route("**/projects*", (route) => route.fulfill(projectsResponse(["acme"])));
  await page.route("**/projects/*/health", (route) => route.fulfill(healthResponse()));

  await page.goto("/");
  await page.getByRole("link", { name: "圖譜" }).click();
  await expect(page.getByRole("heading", { name: /^graph$/i })).toBeVisible();

  await page.getByRole("button", { name: /ada lovelace/i }).click();
  await expect(page.getByText("WORKS_WITH")).toBeVisible();

  await page.getByText("WORKS_WITH").click();
  await expect(page.getByText(/ada worked with charles/i)).toBeVisible();
  await expect(page.getByText("file:///corpus/ada.txt")).toBeVisible();
});
