// Auth flows against the REAL backend + Supabase: signup surfaces a human
// answer (never a network failure), login lands in the app, bad credentials
// are rejected in place, the session survives a reload, and sign-out re-arms
// the route guard. The signup test is written to pass whether Supabase
// "Confirm email" is ON (notice) or OFF — but a "Failed to fetch" always fails.

import { expect, test } from "@playwright/test";

import { E2E_EMAIL_DOMAIN } from "./env";
import { failOnPageErrors, state } from "./helpers";

test("signup responds with a real answer, not a network failure", async ({ page }) => {
  await page.goto("/login");
  // Toggle to signup mode (the toggle button precedes the submit button).
  await page.getByRole("button", { name: "Create account" }).first().click();
  await page
    .getByPlaceholder("name@company.com")
    .fill(`e2e-signup-${Date.now()}@${E2E_EMAIL_DOMAIN}`);
  await page.getByPlaceholder("Password").fill("E2ePass!123");
  await page.locator('button[type="submit"]').click();

  const notice = page.locator("p.st-covered");
  const error = page.locator("p.st-gap");
  await expect(notice.or(error)).toBeVisible({ timeout: 30_000 });

  if (await error.isVisible()) {
    const msg = await error.innerText();
    // The bugs this guards: an unreachable/misconfigured API surfaces the
    // browser's raw fetch error, and a non-string error payload used to render
    // as "[object Object]". Either is a hard failure.
    expect(msg, "signup hit a network-level failure").not.toMatch(/failed to fetch/i);
    expect(msg, "error payload rendered unreadably").not.toMatch(/\[object .*Object\]/i);
    // The backend deliberately hides WHY Supabase rejected a signup behind
    // "Sign-up failed. (ref: …)" (SEC-4 anti-enumeration). With "Confirm email"
    // ON, GoTrue can reject our undeliverable synthetic domain — environment,
    // not an app bug, and indistinguishable by design. Skip with the ref so a
    // human can check the server log; any other error text is a real failure.
    test.skip(
      /sign-?up failed\. \(ref:/i.test(msg),
      `Backend rejected signup (likely the synthetic e2e domain + Confirm-email ON): ${msg}`,
    );
    throw new Error(`Signup failed: ${msg}`);
  }
});

test("login lands on the dashboard and the session survives a reload", async ({ page }) => {
  const assertNoPageErrors = failOnPageErrors(page);
  const creds = state();
  await page.goto("/login");
  await page.getByPlaceholder("name@company.com").fill(creds.email);
  await page.getByPlaceholder("Password").fill(creds.password);
  await page.locator('button[type="submit"]').click();

  await expect(page).toHaveURL(/\/dashboard/, { timeout: 30_000 });
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  await expect(page.getByText(creds.email)).toBeVisible();

  await page.reload();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  await expect(page).toHaveURL(/\/dashboard/);
  await assertNoPageErrors();
});

test("wrong password shows an error and stays on the login page", async ({ page }) => {
  const creds = state();
  await page.goto("/login");
  await page.getByPlaceholder("name@company.com").fill(creds.email);
  await page.getByPlaceholder("Password").fill("definitely-wrong-password");
  await page.locator('button[type="submit"]').click();

  await expect(page.locator("p.st-gap")).toBeVisible({ timeout: 30_000 });
  await expect(page).toHaveURL(/\/login/);
});

test("sign-out returns to login and the route guard blocks the app", async ({ page }) => {
  const creds = state();
  await page.goto("/login");
  await page.getByPlaceholder("name@company.com").fill(creds.email);
  await page.getByPlaceholder("Password").fill(creds.password);
  await page.locator('button[type="submit"]').click();
  await expect(page).toHaveURL(/\/dashboard/, { timeout: 30_000 });

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);

  await page.goto("/dashboard");
  await expect(page).toHaveURL(/\/login/, { timeout: 15_000 });
});
