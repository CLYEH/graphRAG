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
  for (const label of ["Health", "Jobs", "Review", "Playground"]) {
    await expect(page.getByRole("link", { name: label })).toBeVisible();
  }
});

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

test("console shows an empty state when there are no projects", async ({ page }) => {
  await page.route("**/projects*", (route) => route.fulfill(projectsResponse([])));
  await page.goto("/");

  await expect(page.getByText(/no projects yet/i)).toBeVisible();
});
