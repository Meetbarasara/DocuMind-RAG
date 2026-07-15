// Creates a pre-confirmed throwaway Supabase user for the run (admin API with
// the service-role key — works whether or not "Confirm email" is enabled) and
// signs it in directly against GoTrue so specs can seed the app's localStorage
// session without depending on the backend being up during setup.

import fs from "node:fs";
import path from "node:path";

import { E2E_EMAIL_DOMAIN, supabaseEnv } from "./env";

export const STATE_FILE = path.join(__dirname, ".state.json");

export default async function globalSetup() {
  const { url, service, anon } = supabaseEnv();
  const email = `e2e-${Date.now()}@${E2E_EMAIL_DOMAIN}`;
  const password = `E2e!${Math.random().toString(36).slice(2, 10)}A9`;

  const created = await fetch(`${url}/auth/v1/admin/users`, {
    method: "POST",
    headers: {
      apikey: service,
      Authorization: `Bearer ${service}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ email, password, email_confirm: true }),
  });
  if (!created.ok) {
    throw new Error(`Could not create E2E user: HTTP ${created.status} ${await created.text()}`);
  }
  const user = await created.json();

  const tok = await fetch(`${url}/auth/v1/token?grant_type=password`, {
    method: "POST",
    headers: { apikey: anon, "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!tok.ok) {
    throw new Error(`E2E user sign-in failed: HTTP ${tok.status} ${await tok.text()}`);
  }
  const t = await tok.json();

  fs.writeFileSync(
    STATE_FILE,
    JSON.stringify(
      {
        email,
        password,
        userId: user.id,
        accessToken: t.access_token,
        refreshToken: t.refresh_token,
      },
      null,
      2,
    ),
  );
}
