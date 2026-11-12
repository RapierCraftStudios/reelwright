// SPDX-License-Identifier: AGPL-3.0-or-later
// Journey: create a project in the dashboard, then `rw projects list` shows it.
import { execFileSync } from "node:child_process";

import { expect, test } from "@playwright/test";

test("a project created in the dashboard shows up in `rw projects list`", async ({ page }) => {
  const title = `Smoke ${Date.now()}`;

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();
  await page.getByLabel("Title").fill(title);
  await page.getByLabel("Format").selectOption("vlog");
  await page.getByRole("button", { name: "Create project" }).click();
  await expect(page.getByTestId("project-row").filter({ hasText: title })).toBeVisible();

  const listed = execFileSync("uv", ["run", "rw", "projects", "list"], {
    env: { ...process.env, REELWRIGHT_DB_URL: process.env.REELWRIGHT_DB_URL },
    encoding: "utf8",
  });
  expect(listed).toContain(`${title}\tvlog\tdraft`);
});
