// Deletes every user on the synthetic E2E email domain (the run's main user
// plus any signup-spec accounts) and their app rows. Best-effort: a failed
// cleanup never fails the run — the next teardown sweeps stragglers.
// Pinecone vectors are cleaned by the delete-policy spec itself (the app's
// DELETE /documents removes storage + metadata + vectors).

import fs from "node:fs";
import path from "node:path";

import { E2E_EMAIL_DOMAIN, supabaseEnv } from "./env";

const STATE_FILE = path.join(__dirname, ".state.json");
const APP_TABLES = ["compliance_checks", "messages", "conversations", "user_documents"];

export default async function globalTeardown() {
  const { url, service } = supabaseEnv();
  const headers = { apikey: service, Authorization: `Bearer ${service}` };

  try {
    const res = await fetch(`${url}/auth/v1/admin/users?per_page=1000`, { headers });
    if (!res.ok) throw new Error(`list users: HTTP ${res.status}`);
    const users: { id: string; email?: string }[] = (await res.json()).users ?? [];
    for (const u of users) {
      if (!u.email?.endsWith(`@${E2E_EMAIL_DOMAIN}`)) continue;
      for (const table of APP_TABLES) {
        try {
          await fetch(`${url}/rest/v1/${table}?user_id=eq.${u.id}`, {
            method: "DELETE",
            headers,
          });
        } catch {
          /* table may not exist / row cleanup is best-effort */
        }
      }
      await fetch(`${url}/auth/v1/admin/users/${u.id}`, { method: "DELETE", headers });
    }
  } catch (e) {
    console.warn(`[e2e teardown] cleanup incomplete: ${e}`);
  }

  // Regulations the regulation-upload spec created (shared table, so they'd
  // otherwise appear in the real dropdown). PostgREST turns * into a LIKE %.
  try {
    await fetch(`${url}/rest/v1/regulations?name=like.${encodeURIComponent("E2E Reg")}*`, {
      method: "DELETE",
      headers,
    });
  } catch (e) {
    console.warn(`[e2e teardown] regulation cleanup incomplete: ${e}`);
  }

  try {
    fs.unlinkSync(STATE_FILE);
  } catch {
    /* already gone */
  }
}
