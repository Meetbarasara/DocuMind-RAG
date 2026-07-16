// The user's core product vision, end to end: a NEW circular arrives → the
// user uploads it themselves on /regulations (background requirement
// extraction — instant on the fake judge) → it appears in the list → they run
// a gap check of their policy (uploaded by 03-policies.spec.ts) against it.
// Teardown deletes the E2E regulation rows by name; the few stray vectors it
// leaves in the shared "regulations" Pinecone namespace are inert — checks
// judge against the row's CACHED requirements, never that namespace.

import path from "node:path";

import { expect, test } from "@playwright/test";

import { ROW, failOnPageErrors, seedSession } from "./helpers";

const REGULATION_FIXTURE = path.join(__dirname, "fixtures", "e2e_regulation.txt");

test("user uploads their own regulation and checks the policy against it", async ({ page }) => {
  const assertNoPageErrors = failOnPageErrors(page);
  await seedSession(page);
  await page.goto("/regulations");

  const regName = `E2E Reg ${Date.now()}`;
  await page.getByPlaceholder("RBI KYC Master Direction (2024 update)").fill(regName);
  await page.locator('input[type="file"]').setInputFiles(REGULATION_FIXTURE);
  await page.getByRole("button", { name: "Add regulation" }).click();

  // Background job: parse → extract requirements → ingest → upsert the row.
  await expect(page.getByText(/added · \d+ requirements/)).toBeVisible({ timeout: 120_000 });
  await expect(page.getByText(regName)).toBeVisible();

  // The new circular is immediately checkable.
  await page.goto("/check/new");
  const select = page.locator("select");
  await expect(select).toBeVisible({ timeout: 30_000 });
  await select.selectOption({ label: regName });
  await page.getByRole("button", { name: "Run check" }).click();
  await expect(page.getByText("Saved to your history")).toBeVisible({ timeout: 150_000 });
  expect(await page.locator(ROW).count()).toBeGreaterThan(0);

  await assertNoPageErrors();
});
