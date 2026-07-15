// E2E harness: boots the REAL FastAPI backend (real Supabase/Pinecone from the
// repo's .env, but JUDGE_PROVIDER=fake so a gap check is instant and free) plus
// `next dev`, on dedicated ports so a developer's own servers on 8000/3000 are
// untouched. Specs run serially — they share one test user and real services.

import path from "node:path";

import { defineConfig } from "@playwright/test";

import { findPython } from "./e2e/env";

const API_PORT = 8010;
const WEB_PORT = 3010;

export default defineConfig({
  testDir: "./e2e",
  timeout: 180_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  globalSetup: "./e2e/global.setup.ts",
  globalTeardown: "./e2e/global.teardown.ts",
  use: {
    baseURL: `http://localhost:${WEB_PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command: `"${findPython()}" -m uvicorn src.api.main:app --port ${API_PORT}`,
      cwd: path.resolve(__dirname, ".."),
      url: `http://localhost:${API_PORT}/health`,
      reuseExistingServer: false,
      timeout: 240_000, // startup preloads the local embedding model
      env: {
        JUDGE_PROVIDER: "fake",
        CORS_ORIGINS: `http://localhost:${WEB_PORT}`,
        // Keep test traffic out of the developer's observability/cache.
        LANGSMITH_TRACING: "false",
        REDIS_URL: "",
      },
    },
    {
      command: `npm run dev -- -p ${WEB_PORT}`,
      cwd: __dirname,
      url: `http://localhost:${WEB_PORT}`,
      reuseExistingServer: false,
      timeout: 240_000,
      env: { NEXT_PUBLIC_API_BASE: `http://localhost:${API_PORT}` },
    },
  ],
});
