// SPDX-License-Identifier: AGPL-3.0-or-later
// Smoke journey: the real API (uv run rw serve) serving the built web bundle, on a throwaway SQLite DB.
// Build the web app first: pnpm --filter @reelwright/web build
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { defineConfig, devices } from "@playwright/test";

const port = Number(process.env.RW_E2E_PORT ?? 8765);
const baseURL = `http://127.0.0.1:${port}`;

// The config is evaluated in the runner and again in each worker; create the DB dir once and pass it on.
process.env.REELWRIGHT_DB_URL ??= `sqlite:///${join(mkdtempSync(join(tmpdir(), "rw-e2e-")), "e2e.sqlite")}`;
process.env.REELWRIGHT_WEB_DIST ??= join(__dirname, "apps", "web", "dist");

export default defineConfig({
  testDir: "tests/e2e",
  fullyParallel: false,
  workers: Number(process.env.FORGE_HOST_PARALLELISM ?? 1),
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "line",
  use: { baseURL, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `uv run rw serve --host 127.0.0.1 --port ${port}`,
    url: `${baseURL}/healthz`,
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      REELWRIGHT_DB_URL: process.env.REELWRIGHT_DB_URL,
      REELWRIGHT_WEB_DIST: process.env.REELWRIGHT_WEB_DIST,
      // Voice follow without a model: the server "reads" the script at one word per 60 ms of audio.
      REELWRIGHT_PROMPTER_ASR: "mock",
      REELWRIGHT_PROMPTER_MOCK_MS_PER_WORD: "60",
    },
  },
});
