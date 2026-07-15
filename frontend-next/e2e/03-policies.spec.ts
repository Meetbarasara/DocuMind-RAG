// Policy upload against the real pipeline: the file is stored in Supabase,
// chunked, embedded locally and upserted into the user's private Pinecone
// namespace via the background job the UI polls. Deletion is covered by
// 05-ask-cleanup.spec.ts (after the check specs have used the document).

import { expect, test } from "@playwright/test";

import { POLICY_FILENAME, POLICY_FIXTURE, failOnPageErrors, seedSession } from "./helpers";

test("uploading a policy ingests it and lists it under Your policies", async ({ page }) => {
  const assertNoPageErrors = failOnPageErrors(page);
  await seedSession(page);
  await page.goto("/policies");

  await expect(page.getByRole("button", { name: "Upload policy" })).toBeVisible();
  await page.locator('input[type="file"]').setInputFiles(POLICY_FIXTURE);

  // The UI polls the ingestion job; a small txt should complete well inside this.
  await expect(page.getByText(/added · \d+ chunks/)).toBeVisible({ timeout: 120_000 });
  await expect(page.getByText(POLICY_FILENAME)).toBeVisible();

  // Freshly upserted vectors can lag Pinecone's index by a few seconds; give
  // the check spec (next file) a consistent starting point.
  await page.waitForTimeout(8_000);
  await assertNoPageErrors();
});
