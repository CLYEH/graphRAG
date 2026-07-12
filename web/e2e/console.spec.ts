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

test("console shell loads with the project switcher and section nav", async ({ page }) => {
  await page.route("**/projects*", (route) => route.fulfill(projectsResponse(["acme", "beta"])));
  await page.goto("/");

  // the root redirects into the first project's health page
  await expect(page.getByRole("heading", { name: /project health/i })).toBeVisible();
  await expect(page.getByRole("combobox", { name: /project/i })).toHaveValue("acme");
  for (const label of ["Health", "Jobs", "Review", "Playground"]) {
    await expect(page.getByRole("link", { name: label })).toBeVisible();
  }
});

test("console shows an empty state when there are no projects", async ({ page }) => {
  await page.route("**/projects*", (route) => route.fulfill(projectsResponse([])));
  await page.goto("/");

  await expect(page.getByText(/no projects yet/i)).toBeVisible();
});
