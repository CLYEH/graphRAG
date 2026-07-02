import { expect, test } from "@playwright/test";

test("console renders the heading", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /graphRAG Console/i })).toBeVisible();
});
