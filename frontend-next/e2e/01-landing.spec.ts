// The public landing at / — the "LinkedIn try-it" surface. No login, no backend
// dependency (demo mode replays a real check client-side), so this spec catches
// pure frontend regressions in the gap table: streaming, coverage bar, filter
// chips, search, row expansion, CSV export.

import { expect, test } from "@playwright/test";

import { ROW, failOnPageErrors } from "./helpers";

test("demo streams the cited gap table; filters, search, expand and export work", async ({ page }) => {
  const assertNoPageErrors = failOnPageErrors(page);
  await page.goto("/");

  await expect(
    page.getByRole("heading", { name: /check your kyc policy against an rbi circular/i }),
  ).toBeVisible();

  await page.getByRole("button", { name: "Run the demo" }).click();
  // The demo replays 13 rows with realistic pacing.
  await expect(page.locator(ROW)).toHaveCount(13, { timeout: 60_000 });
  await expect(page.getByRole("button", { name: "Replay demo" })).toBeVisible();

  // Status filter: clicking the Gap chip shows exactly the Gap rows.
  const gapChip = page.getByRole("button", { name: /^Gap \d+$/ });
  const gapCount = Number((await gapChip.innerText()).replace(/\D+/g, ""));
  expect(gapCount).toBeGreaterThan(0);
  await gapChip.click();
  await expect(page.locator(ROW)).toHaveCount(gapCount);

  // Search composes with the filter; a nonsense query shows the empty message.
  const search = page.getByPlaceholder("Search requirements…");
  await search.fill("zzzz-no-such-requirement");
  await expect(page.locator(ROW)).toHaveCount(0);
  await expect(page.getByText(/No requirements match/)).toBeVisible();
  await search.fill("");
  await page.getByRole("button", { name: /^All \d+$/ }).click();
  await expect(page.locator(ROW)).toHaveCount(13);

  // Expanding a row shows the side-by-side clause comparison.
  await page.locator(ROW).first().click();
  await expect(page.getByText("RBI requirement", { exact: true })).toBeVisible();
  await expect(page.getByText("Your policy", { exact: true })).toBeVisible();

  // Export downloads a CSV of the full table.
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export" }).click();
  expect((await download).suggestedFilename()).toBe("gap-check.csv");

  await assertNoPageErrors();
});
