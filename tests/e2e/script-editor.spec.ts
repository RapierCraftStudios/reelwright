// SPDX-License-Identifier: AGPL-3.0-or-later
// Journey: write a script in the editor on the real stack (rw serve + built web bundle), see beat/cue
// highlights and an inline parse error, save, reload, lock; check breakpoints and both colour schemes.
import { type Page, expect, test } from "@playwright/test";

const SCRIPT = `---
format: long
working_title: E2E script
---

## beat: hook | intent: open loop
[music: low pulse]
A year ago I hit send. [screen: the email]

## beat: cta | intent: subscribe
The next one is about money. [cut: punch-in] [short: last line]
`;

async function createProject(page: Page, title: string): Promise<number> {
  const response = await page.request.post("/api/projects", { data: { title, format: "long" } });
  expect(response.status()).toBe(201);
  return ((await response.json()) as { id: number }).id;
}

async function putScript(page: Page, id: number, source: string) {
  const response = await page.request.put(`/api/projects/${id}/script`, { data: { source } });
  expect(response.status()).toBe(201);
}

function line(page: Page, n: number) {
  return page.getByTestId("highlight").locator(`[data-line="${n}"]`);
}

test("write, highlight, fix an error, save, reload and lock a script", async ({ page }) => {
  const id = await createProject(page, `Script e2e ${Date.now()}`);
  await page.goto(`/projects/${id}/script`);

  await expect(page.getByText("No script yet — write one or generate from a topic")).toBeVisible();
  await expect(page.getByRole("button", { name: "Generate from a topic" })).toBeVisible();
  await page.getByRole("button", { name: "Write a script" }).click();

  const editor = page.getByLabel("Script", { exact: true });
  await editor.fill(SCRIPT);

  // Beat heading and cues highlighted with icon + type label (not colour only).
  await expect(line(page, 6).locator(".hl-beat")).toHaveText("## beat: hook | intent: open loop");
  await expect(line(page, 6)).toContainText("¶ beat");
  await expect(line(page, 8).locator(".hl-cue.cue-screen")).toHaveText("[screen: the email]");
  await expect(line(page, 8)).toContainText("🖥 screen");
  await expect(line(page, 11)).toContainText("✂ cut");
  await expect(page.getByRole("navigation", { name: "Beat outline" })).toContainText("cta");

  // An unknown cue shows an inline error on its own line.
  await editor.fill(SCRIPT.replace("[screen: the email]", "[zoom: x]"));
  const inline = page.getByTestId("inline-error");
  await expect(inline).toContainText("zoom");
  await expect(line(page, 8)).toHaveClass(/hl-error-line/);
  await expect(page.getByRole("button", { name: "Lock" })).toBeDisabled();

  // Fix it, save with Ctrl+S, and reload: the saved version comes back.
  await editor.fill(SCRIPT);
  await expect(inline).toHaveCount(0);
  await editor.press("Control+s");
  await expect(page.getByText("Saved version 1")).toBeVisible();

  await page.reload();
  await expect(page.getByTestId("script-state")).toContainText("Version 1");
  await expect(page.getByLabel("Script", { exact: true })).toHaveValue(SCRIPT);

  await page.getByRole("button", { name: "Lock" }).click();
  await expect(page.getByText("Locked version 1")).toBeVisible();
  await expect(page.getByTestId("script-state")).toContainText("Locked");
  await expect(page.getByLabel("Script", { exact: true })).toHaveAttribute("readonly", "");
});

test("layout follows the 360/768/1280 breakpoints in dark and light themes", async ({ page }) => {
  const id = await createProject(page, `Layout e2e ${Date.now()}`);
  await putScript(page, id, SCRIPT);

  const outline = page.getByRole("navigation", { name: "Beat outline" });
  const inspector = page.getByRole("complementary", { name: "Cue inspector" });
  const editorBox = page.locator(".editor");

  await page.setViewportSize({ width: 360, height: 800 });
  await page.goto(`/projects/${id}/script`);
  await expect(outline).toBeVisible();
  await expect(inspector).toBeHidden();
  const mobileEditor = await editorBox.boundingBox();
  const mobileOutline = await outline.boundingBox();
  expect(mobileOutline!.y).toBeGreaterThan(mobileEditor!.y); // single column, outline stacked below
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(360);

  await page.setViewportSize({ width: 768, height: 900 });
  await expect(inspector).toBeHidden();
  const tabletEditor = await editorBox.boundingBox();
  const tabletOutline = await outline.boundingBox();
  expect(tabletOutline!.x + tabletOutline!.width).toBeLessThanOrEqual(tabletEditor!.x); // side by side

  await page.setViewportSize({ width: 1280, height: 900 });
  await expect(inspector).toBeVisible();
  await expect(inspector).toContainText("music");
  const desktopEditor = await editorBox.boundingBox();
  const desktopInspector = await inspector.boundingBox();
  expect(desktopInspector!.x).toBeGreaterThanOrEqual(desktopEditor!.x + desktopEditor!.width);

  const background = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  await page.emulateMedia({ colorScheme: "dark" });
  expect(await background()).toBe("rgb(15, 17, 21)");
  await page.emulateMedia({ colorScheme: "light" });
  expect(await background()).toBe("rgb(255, 255, 255)");
  await expect(line(page, 6).locator(".hl-beat")).toBeVisible();
});
