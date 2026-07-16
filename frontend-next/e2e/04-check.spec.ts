// The core product flow, end to end through the real stack: pick a seeded
// regulation, run a check against the policy uploaded by 03-policies.spec.ts
// (real retrieval from Pinecone; deterministic fake judge), watch rows stream,
// then re-open the persisted check at its own URL, re-check it (change
// tracking), and see both runs in the history list.

import { expect, test } from "@playwright/test";

import { POLICY_FILENAME, ROW, failOnPageErrors, seedSession } from "./helpers";

test("run check → rows stream → persisted → re-check → history", async ({ page }) => {
  const assertNoPageErrors = failOnPageErrors(page);
  await seedSession(page);
  await page.goto("/check/new");

  // Regulations must be seeded (scripts/seed_regulation.py) for a check to run.
  // Distinguish the three outcomes so a failure names its real cause: the
  // dropdown (good), a load error (backend/network), or a truly empty table.
  // Counted individually (not .or()) — more than one can be visible at once.
  const select = page.locator("select");
  const loadError = page.locator("p.st-gap");
  const emptyState = page.getByText("No regulations yet");
  await expect
    .poll(
      async () =>
        (await select.count()) + (await loadError.count()) + (await emptyState.count()),
      { timeout: 30_000 },
    )
    .toBeGreaterThan(0);
  if (await loadError.count()) {
    throw new Error(`regulations failed to load: ${await loadError.first().innerText()}`);
  }
  if (await emptyState.count()) {
    throw new Error(
      "no regulations in the database — seed one first (python -m scripts.seed_regulation)",
    );
  }
  // Prefer the small synthetic regulation when present (15 requirements).
  const labels = await select.locator("option").allTextContents();
  const synthetic = labels.findIndex((l) => /synthetic/i.test(l));
  if (synthetic >= 0) await select.selectOption({ index: synthetic });

  await expect(page.getByText(POLICY_FILENAME)).toBeVisible({ timeout: 30_000 });

  await page.getByRole("button", { name: "Run check" }).click();
  await expect(page.getByText("Saved to your history")).toBeVisible({ timeout: 150_000 });

  const rowCount = await page.locator(ROW).count();
  expect(rowCount, "a completed check must have requirement rows").toBeGreaterThan(0);

  // A finished check shows the coverage headline and can be filtered/exported.
  await expect(page.getByRole("button", { name: "Export" })).toBeVisible();

  // The persisted check re-opens instantly at its own URL with the same rows.
  await page.getByRole("link", { name: "open at its own URL" }).click();
  await expect(page).toHaveURL(/\/checks\/[0-9a-f-]{16,}/i);
  await expect(page.locator(ROW)).toHaveCount(rowCount, { timeout: 30_000 });

  // "Suggest a fix" on an actionable row (the one real LLM call in this spec —
  // a single cheap Groq completion; skipped only if the provider throttles).
  const actionableChip = page
    .getByRole("button", { name: /^(Gap|Partial|Conflict) \d+$/ })
    .first();
  if (await actionableChip.count()) {
    await actionableChip.click();
    await page.locator(ROW).first().click();
    const suggest = page.getByRole("button", { name: "Suggest a fix" });
    await expect(suggest).toBeVisible();
    await suggest.click();
    const draft = page.getByText("Suggested fix · draft");
    const suggestErr = page.locator("p.st-gap");
    await expect(draft.or(suggestErr)).toBeVisible({ timeout: 60_000 });
    if (await suggestErr.count()) {
      const msg = await suggestErr.first().innerText();
      test.skip(/rate ?limit|429|quota/i.test(msg), `LLM throttled: ${msg}`);
      throw new Error(`Suggest a fix failed: ${msg}`);
    }
    // Collapse and clear the filter so the re-check assertions see all rows.
    await page.locator(ROW).first().click();
    await page.getByRole("button", { name: /^All \d+$/ }).click();
  }

  // Change-tracked re-check: nothing changed in the regulation, so every row
  // carries forward — and the result is saved as a NEW check.
  await page.getByRole("button", { name: "Re-check" }).click();
  await expect(page.getByText("Saved as a new check")).toBeVisible({ timeout: 150_000 });
  await expect(page.getByText("Change-tracked re-check")).toBeVisible();
  await expect(page.locator(ROW)).toHaveCount(rowCount);

  // Both runs are in the history.
  await page.goto("/checks");
  await expect(page.locator('a[href*="/checks/"]')).toHaveCount(2, { timeout: 30_000 });

  await assertNoPageErrors();
});
