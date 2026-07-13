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
  await page.goto("/");

  // the root redirects into the first project's health page, which renders the
  // §19 status light once /health resolves
  await expect(page.getByRole("heading", { name: /project health/i })).toBeVisible();
  await expect(page.getByRole("status")).toHaveText("Healthy");
  await expect(page.getByText("chunks")).toBeVisible();
  await expect(page.getByRole("combobox", { name: /project/i })).toHaveValue("acme");
  for (const label of ["Health", "Import", "Clean", "Inspect", "Jobs", "Review", "Playground"]) {
    await expect(page.getByRole("link", { name: label })).toBeVisible();
  }
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
          left_snapshot: null,
          right_snapshot: null,
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

  await page.getByRole("link", { name: "Jobs" }).click();
  await expect(page.getByRole("heading", { name: /pipeline/i })).toBeVisible();
  await expect(page.getByText(/watch a job/i)).toBeVisible();
  await expect(page.getByText("active")).toBeVisible(); // a run row badge
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

  await page.goto("/");
  await page.getByRole("link", { name: "Review" }).click();

  await expect(page.getByRole("heading", { name: /entity review/i })).toBeVisible();
  await expect(page.getByText("pending")).toBeVisible();

  await page.getByRole("button", { name: "Approve" }).click();
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

  await page.getByRole("link", { name: "Playground" }).click();
  await expect(page.getByRole("heading", { name: /query playground/i })).toBeVisible();

  await page.getByLabel("query", { exact: true }).fill("what is the answer?");
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

  await page.getByRole("link", { name: "Import" }).click();
  await expect(page.getByRole("heading", { name: /^import$/i })).toBeVisible();

  // register a text source by file:// uri (no byte upload — the contract models a
  // uri reference, and text is the ingest-wired default kind)
  await page.getByLabel("uri").fill("file:///data/corpus/");
  await page.getByRole("button", { name: /add source/i }).click();
  await expect.poll(() => sourceBody).toContain("file:///data/corpus/");

  // trigger a full build and watch the accepted job
  await page.getByRole("button", { name: /^build$/i }).click();
  await expect(page.getByText(/accepted job/i)).toBeVisible();
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
  await page.getByRole("link", { name: "Inspect" }).click();
  await expect(page.getByRole("heading", { name: /^inspect$/i })).toBeVisible();

  await expect(page.getByRole("button", { name: "file:///data/corpus/a.txt" })).toBeVisible();
  await expect.poll(() => listUrl).toContain("/documents?");
  expect(listUrl).not.toContain("sort");
  expect(listUrl).not.toContain("filter");

  await page.getByRole("button", { name: "file:///data/corpus/a.txt" }).click();
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
  await page.getByRole("link", { name: "Clean" }).click();
  await expect(page.getByRole("heading", { name: /^clean$/i })).toBeVisible();

  await page.locator("textarea").fill("alpha beta gamma delta");
  await page.getByRole("button", { name: /^preview$/i }).click();
  await expect(page.getByText("alpha beta", { exact: true })).toBeVisible();
  expect(JSON.parse(previewBody)).toEqual({ text: "alpha beta gamma delta" });

  await page.getByRole("button", { name: /save 500\/50 to config/i }).click();
  await expect(page.getByText(/saved — the next build/i)).toBeVisible();
  const patched = JSON.parse(patchBody);
  expect(patched.config.ontology).toEqual({ entity_types: ["PERSON"] });
  expect(patched.config.chunking).toEqual({ max_chars: 500, overlap: 50 });
});
