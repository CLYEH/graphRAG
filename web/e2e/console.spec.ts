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
  for (const label of [
    "總覽",
    "匯入",
    "建置",
    "檢視",
    "清洗",
    "圖譜",
    "治理",
    "品質",
    "檢索",
    "診斷",
    "設定",
  ]) {
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

test("the quality section runs an eval and shows the per-case verdicts", async ({ page }) => {
  await page.route("**/projects*", (route) => route.fulfill(projectsResponse(["acme"])));
  await page.route("**/projects/*/health", (route) => route.fulfill(healthResponse()));
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
    eval: null as unknown,
  };
  const evaluatedBuild = {
    ...readyBuild,
    eval: {
      build_id: readyBuild.id,
      score: 0.66,
      passed: 1,
      failed: 1,
      fingerprint: "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
      metrics: {},
      cases: [
        { question: "海祭是哪一族的祭儀?", mode: "hybrid", score: 0.92, passed: true },
        { question: "區域探索廳在幾樓?", mode: "sql", score: 0.4, passed: false },
      ],
    },
  };
  // the report exists only AFTER the eval job ran: the builds read serves the
  // unevaluated build until the eval POST is accepted, then the evaluated one
  // (the page's terminal-job invalidation is what triggers the refetch)
  let evalAccepted = false;
  await page.route("**/projects/*/builds?*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data: [evalAccepted ? evaluatedBuild : readyBuild], meta: META }),
    }),
  );
  let evalPath = "";
  let evalKey = "";
  await page.route("**/builds/*/eval", (route) => {
    evalPath = new URL(route.request().url()).pathname;
    evalKey = route.request().headers()["idempotency-key"] ?? "";
    evalAccepted = true;
    return route.fulfill(jobAcceptedResponse());
  });
  await page.route("**/jobs/*/events", (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: "" }),
  );
  await page.route("**/jobs/*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          job_id: "0c9f7a3e-2f65-4f0a-8a2b-7d1e9c4b5a6f",
          status: "done",
          kind: "eval",
          project: "acme",
          build_id: readyBuild.id,
          step: null,
          progress: 1,
          message: null,
          error: null,
          created_at: "2026-07-01T00:00:00Z",
          finished_at: "2026-07-01T00:01:00Z",
        },
        meta: META,
      }),
    }),
  );
  await page.goto("/");

  await page.getByRole("link", { name: "品質", exact: true }).click();
  await expect(page.getByRole("heading", { name: "品質(評測)" })).toBeVisible();
  await expect(page.getByText("此版本還沒有評測結果。")).toBeVisible();

  await page.getByRole("button", { name: "開始評測" }).click();
  await expect.poll(() => evalPath).toMatch(/\/builds\/[^/]+\/eval$/);
  expect(evalKey).toMatch(/[0-9a-f-]{36}/);

  // the terminal job invalidates the builds read → the per-case verdicts render
  // (role-scoped: the raw JSON fold also carries the question text)
  await expect(page.getByRole("cell", { name: "海祭是哪一族的祭儀?" })).toBeVisible();
  await expect(page.getByText("通過", { exact: true })).toBeVisible();
  await expect(page.getByText("未過", { exact: true })).toBeVisible();
  await expect(page.getByText(/總分:0\.66/)).toBeVisible();
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
  await page.getByRole("link", { name: "治理" }).click();

  // the governance surface (治理) defaults to the 合併 (merge) tab — the flow below
  // adjudicates a merge candidate, so no tab switch is needed
  await expect(page.getByRole("heading", { name: "治理" })).toBeVisible();
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

function ontologyProposalsResponse() {
  return {
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      data: [
        {
          id: "d1111111-aaaa-4aaa-8aaa-000000000001",
          project: "acme",
          kind: "entity",
          type_name: "Spaceship",
          proposal_key: "fpv2:entity:spaceship",
          fingerprint_version: 2,
          example: "Rocinante",
          chunk_ref: "chunk:abc123:0",
          status: "proposed",
          decided_by: null,
          decided_at: null,
          reason: null,
          created_at: "2026-07-01T00:00:00Z",
        },
      ],
      meta: META,
    }),
  };
}

test("the governance page adjudicates an ontology proposal on the 本體提案 tab", async ({
  page,
}) => {
  await page.route("**/projects*", (route) => route.fulfill(projectsResponse(["acme"])));
  await page.route("**/projects/*/health", (route) => route.fulfill(healthResponse()));
  // the default 合併 tab mounts ReviewCases first, so its query needs a stub too
  await page.route("**/projects/*/merge-candidates*", (route) =>
    route.fulfill(mergeCandidatesResponse()),
  );
  await page.route("**/projects/*/ontology-proposals*", (route) =>
    route.fulfill(ontologyProposalsResponse()),
  );

  let acceptPath = "";
  let idemKey = "";
  // more-specific accept route registered AFTER the list route so it wins for the
  // POST (Playwright matches last-registered first)
  await page.route("**/ontology-proposals/*/accept", (route) => {
    acceptPath = new URL(route.request().url()).pathname;
    idemKey = route.request().headers()["idempotency-key"] ?? "";
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          id: "d1111111-aaaa-4aaa-8aaa-000000000001",
          project: "acme",
          kind: "entity",
          type_name: "Spaceship",
          proposal_key: "fpv2:entity:spaceship",
          fingerprint_version: 2,
          example: "Rocinante",
          chunk_ref: "chunk:abc123:0",
          status: "accepted",
          decided_by: "console",
          decided_at: "2026-07-02T00:00:00Z",
          reason: null,
          created_at: "2026-07-01T00:00:00Z",
        },
        meta: META,
      }),
    });
  });

  await page.goto("/");
  await page.getByRole("link", { name: "治理" }).click();
  // switch from the default 合併 tab to the ontology-proposal pool
  await page.getByRole("tab", { name: "本體提案" }).click();

  // the pool leads with the proposed TYPE name and its honest kind label — never
  // the raw enum or a naked id (UXA3 translation layer)
  await expect(page.getByText("Spaceship")).toBeVisible();
  await expect(page.getByText("實體型別")).toBeVisible();

  // 採納 posts the ACCEPT verb on its own path with the deterministic idem-key; a
  // body-verb, the reject path, or a random key would fail these assertions
  await page.getByRole("button", { name: /採納/ }).click();
  await expect.poll(() => acceptPath).toMatch(/\/ontology-proposals\/[^/]+\/accept$/);
  expect(idemKey).toBe("d1111111-aaaa-4aaa-8aaa-000000000001:accept");
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

  await page.getByRole("link", { name: "檢索" }).click();
  await expect(page.getByRole("heading", { name: "檢索測試" })).toBeVisible();

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

test("the import section uploads files and shows the per-file manifest", async ({ page }) => {
  await page.route("**/projects*", (route) => route.fulfill(projectsResponse(["acme"])));
  await page.route("**/projects/*/health", (route) => route.fulfill(healthResponse()));
  // world-state sources: the managed corpus source exists only AFTER the
  // upload landed (never a call count — a premature refetch must not be
  // handed the "after" world)
  let uploaded = false;
  await page.route("**/projects/*/sources*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: uploaded
          ? [
              {
                id: "50000000-0000-0000-0000-000000000001",
                project: "acme",
                kind: "text",
                uri: "file:///C:/data/uploads/acme",
                metadata: {},
                created_at: "2026-07-01T00:00:00Z",
              },
            ]
          : [],
        meta: META,
      }),
    }),
  );
  let uploadKey = "";
  await page.route("**/projects/*/uploads*", (route) => {
    uploadKey = route.request().headers()["idempotency-key"] ?? "";
    uploaded = true;
    return route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          source_id: "50000000-0000-0000-0000-000000000001",
          files: [
            {
              filename: "deadbeefcafe0000.txt",
              original_filename: "guide.txt",
              status: "accepted",
              document_uri: "file:///C:/data/uploads/acme/deadbeefcafe0000.txt",
              metadata: {
                schema_version: "1.2",
                system: { connector: "upload", original_filename: "guide.txt" },
                context: {},
                governance: {},
              },
            },
            {
              original_filename: "virus.exe",
              status: "rejected",
              reason: "extension '.exe' is not allowlisted (txt, md)",
            },
          ],
        },
        meta: META,
      }),
    });
  });
  await page.goto("/");

  await page.getByRole("link", { name: "匯入", exact: true }).click();
  await expect(page.getByRole("heading", { name: "匯入資料" })).toBeVisible();
  await expect(page.getByText("No sources registered yet.")).toBeVisible();

  await page.setInputFiles('input[type="file"]', [
    { name: "guide.txt", mimeType: "text/plain", buffer: Buffer.from("hello") },
    { name: "virus.exe", mimeType: "application/octet-stream", buffer: Buffer.from("MZ") },
  ]);
  await page.getByRole("button", { name: "上傳", exact: true }).click();

  // honest per-file manifest: verdict words + verbatim refusal reason
  await expect(page.getByText(/接受 1 檔 · 退回 1 檔/)).toBeVisible();
  await expect(page.getByText("已接受")).toBeVisible();
  await expect(page.getByText("已退回")).toBeVisible();
  await expect(page.getByText(/extension '\.exe' is not allowlisted/)).toBeVisible();
  expect(uploadKey).toMatch(/[0-9a-f-]{36}/);

  // the registered managed source appears in the list without a manual refresh
  await expect(page.getByText("file:///C:/data/uploads/acme", { exact: true })).toBeVisible();
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

test("the settings page saves a vocabulary edit over a fresh-spread PATCH", async ({ page }) => {
  const config = {
    ontology: {
      entity_types: ["EVENT"],
      relation_types: ["PRACTICED_BY"],
      proposal_policy: "review",
    },
    chunking: { max_chars: 500, overlap: 50 },
  };
  await page.route("**/projects*", (route) => route.fulfill(projectsResponse(["acme"])));
  await page.route("**/projects/*/health", (route) => route.fulfill(healthResponse()));
  let patched: { config?: Record<string, unknown> } | null = null;
  await page.route("**/projects/acme", (route) => {
    if (route.request().method() === "PATCH") {
      patched = route.request().postDataJSON() as { config?: Record<string, unknown> };
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          data: {
            name: "acme",
            display_name: null,
            description: null,
            config: patched.config ?? {},
            created_at: "2026-07-01T00:00:00Z",
          },
          meta: META,
        }),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          name: "acme",
          display_name: null,
          description: null,
          // a real server serves what was just PATCHed — the post-save
          // refetch must see the new vocabulary or the page's confirmation
          // (a comparison, not an event) rightly refuses to stand
          config: patched?.config ?? config,
          created_at: "2026-07-01T00:00:00Z",
        },
        meta: META,
      }),
    });
  });

  await page.goto("/");
  await page.getByRole("link", { name: "設定", exact: true }).click();
  await expect(page.getByRole("heading", { name: "設定" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "知識類型" })).toBeVisible();

  await page.getByLabel("新增實體類型").fill("PLACE");
  await page.getByRole("button", { name: "加入實體類型" }).click();
  await page.getByRole("button", { name: "儲存知識類型" }).click();

  await expect(page.getByText("已儲存。")).toBeVisible();
  // the PATCH spreads the whole config: the untouched chunking block rides along
  expect(patched?.config?.["chunking"]).toEqual({ max_chars: 500, overlap: 50 });
});

// UXC2c — the Phase C 目標 as an executable assertion: the FULL operator
// journey runs in the browser with no terminal — create project → upload →
// build → eval → activate → query. Every read is a WORLD-STATE stub (flags
// flip when the modeled action actually lands, never call counts — class 26),
// so each step only succeeds because the previous step's write happened.
test("the full no-terminal path: create → upload → build → eval → activate → query", async ({
  page,
}) => {
  const B1 = "b1111111-aaaa-4aaa-8aaa-000000000001";
  const world = {
    created: false,
    uploaded: false,
    hasOntology: false,
    buildDone: false,
    evalDone: false,
    activated: false,
  };
  // a text corpus cannot build without an ontology (the worker's
  // OntologyRequiredError; the Import run-gate mirrors it) — the REAL journey
  // includes the Settings 知識類型 step, so the config is world-state too
  const proj = () => ({
    name: "e2e",
    display_name: null,
    description: null,
    config: world.hasOntology
      ? { ontology: { entity_types: ["EVENT"], relation_types: ["PRACTICED_BY"] } }
      : {},
    created_at: "2026-07-01T00:00:00Z",
  });
  const doneJob = (jobId: string, kind: string) => ({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      data: {
        job_id: jobId,
        status: "done",
        kind,
        project: "e2e",
        build_id: B1,
        step: null,
        progress: 1,
        message: null,
        error: null,
        created_at: "2026-07-01T00:00:00Z",
        finished_at: "2026-07-01T00:01:00Z",
      },
      meta: META,
    }),
  });

  await page.route("**/projects", (route) => {
    if (route.request().method() === "POST") {
      world.created = true;
      return route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({ data: proj(), meta: META }),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data: world.created ? [proj()] : [], meta: META }),
    });
  });
  await page.route("**/projects?*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data: world.created ? [proj()] : [], meta: META }),
    }),
  );
  await page.route("**/projects/e2e", (route) => {
    // the Settings page reads/patches the single project (the API path takes
    // the RAW name); saving 知識類型 flips the world's ontology
    if (route.request().method() === "PATCH") world.hasOntology = true;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data: proj(), meta: META }),
    });
  });
  await page.route("**/projects/*/health", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          status: world.activated ? "healthy" : "empty",
          active_build_id: world.activated ? B1 : null,
          counts: world.activated ? { documents: 1, entities: 5, relations: 3 } : {},
          pending_review: 0,
          drift: null,
          warnings: [],
        },
        meta: META,
      }),
    }),
  );
  await page.route("**/projects/*/sources*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: world.uploaded
          ? [
              {
                id: "50000000-0000-0000-0000-000000000001",
                project: "e2e",
                kind: "text",
                uri: "file:///C:/data/uploads/e2e",
                metadata: {},
                created_at: "2026-07-01T00:00:00Z",
              },
            ]
          : [],
        meta: META,
      }),
    }),
  );
  await page.route("**/projects/*/builds?*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: world.buildDone
          ? [
              {
                id: B1,
                project: "e2e",
                status: world.activated ? "active" : "ready",
                config_hash: null,
                source_hash: null,
                started_at: "2026-07-01T00:00:00Z",
                finished_at: "2026-07-01T00:05:00Z",
                activated_at: world.activated ? "2026-07-01T00:10:00Z" : null,
                metrics: null,
                eval: world.evalDone
                  ? {
                      build_id: B1,
                      score: 1,
                      passed: 1,
                      failed: 0,
                      fingerprint: "deadbeef",
                      metrics: {},
                      cases: [
                        {
                          question: "海祭是哪一族的祭儀?",
                          mode: "semantic",
                          score: 1,
                          passed: true,
                        },
                      ],
                    }
                  : null,
              },
            ]
          : [],
        meta: META,
      }),
    }),
  );
  await page.route("**/projects/*/uploads*", (route) => {
    world.uploaded = true;
    return route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          source_id: "50000000-0000-0000-0000-000000000001",
          files: [
            {
              filename: "deadbeefcafe0000.txt",
              original_filename: "guide.txt",
              status: "accepted",
              document_uri: "file:///C:/data/uploads/e2e/deadbeefcafe0000.txt",
              metadata: {
                schema_version: "1.2",
                system: { connector: "upload", original_filename: "guide.txt" },
                context: {},
                governance: {},
              },
            },
          ],
        },
        meta: META,
      }),
    });
  });
  await page.route("**/projects/*/build", (route) => route.fulfill(jobAcceptedResponse()));
  await page.route("**/builds/*/eval", (route) => {
    return route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        data: { job_id: "1d8e8b4f-3a76-4b1b-9c3d-8e2f0a1b2c3e", status: "queued" },
        meta: META,
      }),
    });
  });
  await page.route("**/builds/*/activate", (route) => {
    world.activated = true;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: { id: B1, project: "e2e", status: "active" },
        meta: META,
      }),
    });
  });
  await page.route("**/jobs/*/events", (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: "" }),
  );
  // the world advances when the modeled JOB is OBSERVED terminal (the snapshot
  // GET the UI's job watch performs), never at the trigger POST: a UI that
  // stopped wiring the job watch would leave the flag unset, the builds list
  // empty, and the journey red — the load-bearing boundary of the no-terminal
  // path (Codex #84)
  await page.route("**/jobs/0c9f7a3e*", (route) => {
    world.buildDone = true;
    return route.fulfill(doneJob("0c9f7a3e-2f65-4f0a-8a2b-7d1e9c4b5a6f", "build"));
  });
  await page.route("**/jobs/1d8e8b4f*", (route) => {
    world.evalDone = true;
    return route.fulfill(doneJob("1d8e8b4f-3a76-4b1b-9c3d-8e2f0a1b2c3e", "eval"));
  });
  await page.route("**/projects/*/query/*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          mode: "hybrid",
          build_id: B1,
          results: [
            {
              result_type: "chunk",
              id: "c1111111-aaaa-4aaa-8aaa-000000000001",
              title: null,
              text: "海祭是阿美族的祭儀,每年5月初由頭目率領族人舉行。",
              score: 0.93,
              confidence: null,
              source_refs: [{ source_type: "chunk", id: "c1111111-aaaa-4aaa-8aaa-000000000001" }],
            },
          ],
          graph_context: null,
          warnings: [],
          debug: null,
        },
        meta: META,
      }),
    }),
  );

  // ① create the FIRST project from the root bootstrap form
  await page.goto("/");
  await expect(page.getByText(/No projects yet/)).toBeVisible();
  await page.getByLabel("name", { exact: true }).fill("e2e");
  await page.getByRole("button", { name: "Create project" }).click();
  await expect(page.getByText(/尚未開始/)).toBeVisible(); // landed on 總覽

  // ② upload the corpus (匯入)
  await page.getByRole("link", { name: "去匯入" }).click();
  await page.setInputFiles('input[type="file"]', [
    { name: "guide.txt", mimeType: "text/plain", buffer: Buffer.from("海祭是阿美族的祭儀") },
  ]);
  const uploadBtn = page.getByRole("button", { name: "上傳", exact: true });
  await expect(uploadBtn).toBeEnabled();
  await uploadBtn.click();
  await expect(page.getByText(/接受 1 檔/)).toBeVisible();
  await expect(page.getByText("file:///C:/data/uploads/e2e", { exact: true })).toBeVisible();

  // ②b a text corpus can't build without an ontology — set 知識類型 (設定)
  await page.getByRole("link", { name: "設定", exact: true }).click();
  const entityInput = page.getByLabel("新增實體類型");
  await entityInput.fill("EVENT");
  await entityInput.press("Enter");
  const relationInput = page.getByLabel("新增關係類型");
  await relationInput.fill("PRACTICED_BY");
  await relationInput.press("Enter");
  await page.getByRole("button", { name: "儲存知識類型" }).click();
  await expect(page.getByText("已儲存。")).toBeVisible();

  // ③ build (back on 匯入 — the ontology gate is now open)
  await page.getByRole("link", { name: "匯入", exact: true }).click();
  const buildBtn = page.getByRole("button", { name: "開始建置" });
  await expect(buildBtn).toBeEnabled();
  await buildBtn.click();
  await expect(page.getByText(/建置已排入佇列/)).toBeVisible();

  // ④ eval (品質)
  await page.getByRole("link", { name: "品質", exact: true }).click();
  await expect(page.getByText("此版本還沒有評測結果。")).toBeVisible();
  await page.getByRole("button", { name: "開始評測" }).click();
  await expect(page.getByRole("cell", { name: "海祭是哪一族的祭儀?" })).toBeVisible();
  await expect(page.getByText("通過", { exact: true })).toBeVisible();

  // ⑤ activate (總覽)
  await page.getByRole("link", { name: "總覽", exact: true }).click();
  await page.getByRole("button", { name: "上線這個版本" }).click();
  await page.getByRole("button", { name: "確定上線" }).click();
  await expect(page.getByText(/服務中/)).toBeVisible();

  // ⑥ query (檢索) — the journey ends with real ranked hits, in-browser only
  await page.getByRole("link", { name: "檢索", exact: true }).click();
  await expect(page.getByRole("heading", { name: "檢索測試" })).toBeVisible();
  await page.getByLabel("問題").fill("海祭是哪一族的祭儀?");
  await page.getByRole("button", { name: "Run query" }).click();
  await expect(page.getByText("1 筆結果")).toBeVisible();
  await expect(page.getByText(/海祭是阿美族的祭儀/)).toBeVisible();
});
