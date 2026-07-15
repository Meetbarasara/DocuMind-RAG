import fs from "node:fs";
import path from "node:path";

import { expect, type Page } from "@playwright/test";

export interface E2EState {
  email: string;
  password: string;
  userId: string;
  accessToken: string;
  refreshToken: string;
}

export function state(): E2EState {
  return JSON.parse(fs.readFileSync(path.join(__dirname, ".state.json"), "utf8"));
}

/** Sign the page in as the run's test user by seeding the app's localStorage
 *  session (lib/session.ts shape) before any page script runs. */
export async function seedSession(page: Page): Promise<E2EState> {
  const s = state();
  await page.addInitScript(
    ([key, value]) => window.localStorage.setItem(key, value),
    [
      "kyc.session",
      JSON.stringify({
        accessToken: s.accessToken,
        refreshToken: s.refreshToken,
        email: s.email,
      }),
    ] as const,
  );
  return s;
}

/** A gap-table row card: the expandable header button with the status bar.
 *  (`button[aria-expanded]` alone also matched Next's dev-tools overlay.) */
export const ROW = "button[aria-expanded]:has(span.st-bar)";

export const POLICY_FIXTURE = path.join(__dirname, "fixtures", "e2e_policy.txt");
export const POLICY_FILENAME = "e2e_policy.txt";

/** Fail the test on any uncaught page exception — frontend crashes are bugs
 *  even when the flow "looks" fine. Call at the top of a test. */
export function failOnPageErrors(page: Page) {
  const errors: Error[] = [];
  page.on("pageerror", (e) => errors.push(e));
  return async () => {
    expect(errors, `uncaught page errors: ${errors.map((e) => e.message).join(" | ")}`)
      .toHaveLength(0);
  };
}
