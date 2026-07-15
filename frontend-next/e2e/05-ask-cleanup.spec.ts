// Ask (grounded Q&A over the uploaded policy — the one flow that makes a real
// LLM call, a single cheap Groq completion) and then policy deletion, which
// doubles as the app-level cleanup of storage + metadata + Pinecone vectors.

import { expect, test } from "@playwright/test";

import { POLICY_FILENAME, failOnPageErrors, seedSession } from "./helpers";

test("ask answers about the uploaded policy without surfacing an error", async ({ page }) => {
  const assertNoPageErrors = failOnPageErrors(page);
  await seedSession(page);
  await page.goto("/ask");

  await page
    .getByPlaceholder("e.g. How long do we retain KYC records?")
    .fill("How long are customer records retained?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();

  // Streaming ends when the button returns from "Answering…" to "Ask".
  await expect(page.getByRole("button", { name: "Ask", exact: true })).toBeVisible({
    timeout: 90_000,
  });

  const error = page.locator("div.st-gap");
  if (await error.count()) {
    const msg = (await error.first().innerText()).trim();
    // Free-tier throttling is provider weather, not an app bug.
    test.skip(/rate ?limit|429|quota/i.test(msg), `LLM throttled: ${msg}`);
    throw new Error(msg);
  }
  // A real answer (or an honest refusal) — never an empty panel.
  await expect(page.locator(".whitespace-pre-wrap").first()).not.toBeEmpty();
  await assertNoPageErrors();
});

test("deleting the policy removes it from the list (and cleans its vectors)", async ({ page }) => {
  await seedSession(page);
  await page.goto("/policies");

  await expect(page.getByText(POLICY_FILENAME)).toBeVisible({ timeout: 30_000 });
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: `Delete ${POLICY_FILENAME}` }).click();

  await expect(page.getByText(POLICY_FILENAME)).toBeHidden({ timeout: 60_000 });
  await expect(page.getByText("0 documents")).toBeVisible();
});
