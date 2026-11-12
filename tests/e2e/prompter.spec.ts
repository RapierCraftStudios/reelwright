// SPDX-License-Identifier: AGPL-3.0-or-later
// Journey: read a locked script on the phone teleprompter, on the real stack (rw serve + built web
// bundle): cues hidden, per-device settings, keyboard/pedal and tap controls, retake marks posted to
// the API, empty/loading/offline states, breakpoints, contrast in both schemes and motion.
import { type Page, expect, test } from "@playwright/test";

const SPOKEN = Array.from({ length: 24 }, (_, i) => `Spoken line number ${i + 1} of the take.`);

const SCRIPT = `---
format: long
working_title: Prompter e2e
---

## beat: hook | intent: open loop
[music: low pulse]
${SPOKEN.slice(0, 12)
  .map((line, i) => (i === 0 ? `${line} [broll: desk shot]` : line))
  .join("\n")}

## beat: cta | intent: subscribe
[cut: punch-in]
${SPOKEN.slice(12).join("\n")} [short: last line]
`;

const CUE_TEXT = /broll|desk shot|music|low pulse|punch-in|short:|\[/;

async function createProject(page: Page, title: string): Promise<number> {
  const response = await page.request.post("/api/projects", { data: { title, format: "long" } });
  expect(response.status()).toBe(201);
  return ((await response.json()) as { id: number }).id;
}

async function lockedProject(page: Page, title = "Prompter"): Promise<number> {
  const id = await createProject(page, `${title} ${Date.now()}`);
  expect((await page.request.put(`/api/projects/${id}/script`, { data: { source: SCRIPT } })).status()).toBe(201);
  expect((await page.request.post(`/api/projects/${id}/script/lock`)).status()).toBe(200);
  return id;
}

async function openPrompter(page: Page, id: number) {
  await page.goto(`/prompter/${id}`);
  await expect(page.getByTestId("prompter-text")).toContainText(SPOKEN[0]);
}

const text = (page: Page) => page.getByTestId("prompter-text");

async function translateY(page: Page): Promise<number> {
  return text(page).evaluate((el) => new DOMMatrix(getComputedStyle(el).transform).m42);
}

async function activeLine(page: Page): Promise<string | null> {
  return page.locator('[data-active="true"]').getAttribute("data-line");
}

async function marks(page: Page, id: number) {
  const response = await page.request.get(`/api/prompter/${id}/marks`);
  expect(response.status()).toBe(200);
  return (await response.json()) as { kind: string; marked_at: string; offset_ms: number; line_no: number }[];
}

test("renders the locked script without cue tags", async ({ page }) => {
  const id = await lockedProject(page);
  await openPrompter(page, id);
  await expect(text(page).locator("[data-line]")).toHaveCount(SPOKEN.length);
  expect(await text(page).textContent()).not.toMatch(CUE_TEXT);
});

test("font size, speed, margin and mirror persist per device", async ({ page }) => {
  const id = await lockedProject(page);
  await openPrompter(page, id);
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByLabel(/Font size/).fill("64");
  await page.getByLabel(/Speed/).fill("120");
  await page.getByLabel(/Margin/).fill("12");
  await page.getByLabel("Mirror for beam splitter").check();

  const check = async () => {
    await expect(text(page)).toHaveCSS("font-size", "64px");
    await expect(page.getByTestId("prompter-viewport")).toHaveClass(/is-mirrored/);
    await page.getByRole("button", { name: "Settings" }).click();
    await expect(page.getByLabel(/Speed/)).toHaveValue("120");
    await expect(page.getByLabel(/Margin/)).toHaveValue("12");
    await expect(page.getByLabel("Mirror for beam splitter")).toBeChecked();
  };

  await page.reload();
  await expect(text(page)).toContainText(SPOKEN[0]);
  await check();

  const other = await lockedProject(page, "Other");
  await openPrompter(page, other);
  await check();
});

test("keys start, pause, go back one line and mark a retake", async ({ page }) => {
  const id = await lockedProject(page);
  await openPrompter(page, id);
  const start = await translateY(page);

  await page.keyboard.press("Space");
  await expect.poll(() => activeLine(page), { timeout: 5000 }).not.toBe("8");
  await page.keyboard.press("Space");
  const paused = await translateY(page);
  expect(paused).toBeLessThan(start);
  await page.waitForTimeout(400);
  expect(await translateY(page)).toBe(paused);

  const before = Number(await activeLine(page));
  await page.keyboard.press("ArrowUp");
  await expect.poll(async () => Number(await activeLine(page))).toBeLessThan(before);

  await page.keyboard.press("r");
  await expect(page.getByTestId("prompter-status")).toContainText("Retake marked");
  await expect.poll(async () => (await marks(page, id)).length).toBe(1);
  const [mark] = await marks(page, id);
  expect(mark.kind).toBe("retake");
  expect(Number.isNaN(Date.parse(mark.marked_at))).toBe(false);
  expect(mark.offset_ms).toBeGreaterThan(0);
});

test("tap controls start, pause, go back and mark a retake", async ({ page }) => {
  const id = await lockedProject(page);
  await openPrompter(page, id);
  await page.getByRole("button", { name: "Start" }).click();
  await expect.poll(() => activeLine(page), { timeout: 5000 }).not.toBe("8");
  await page.getByRole("button", { name: "Pause" }).click();
  const before = Number(await activeLine(page));
  await page.getByRole("button", { name: "Back one line" }).click();
  await expect.poll(async () => Number(await activeLine(page))).toBeLessThan(before);
  await page.getByRole("button", { name: "Mark retake" }).click();
  await expect.poll(async () => (await marks(page, id)).length).toBe(1);
  // Tapping the text toggles play too.
  await page.getByTestId("prompter-viewport").click({ position: { x: 20, y: 200 } });
  await expect(page.getByRole("button", { name: "Pause" })).toBeVisible();
});

test("an unlocked project shows the empty state", async ({ page }) => {
  const id = await createProject(page, `Unlocked ${Date.now()}`);
  expect((await page.request.put(`/api/projects/${id}/script`, { data: { source: SCRIPT } })).status()).toBe(201);
  await page.goto(`/prompter/${id}`);
  await expect(page.getByText("No locked script — lock one in the editor")).toBeVisible();
  await expect(page.getByRole("link", { name: "Open the script editor" })).toHaveAttribute("href", `/projects/${id}/script`);
});

test("loading shows a large spinner over black", async ({ page }) => {
  const id = await lockedProject(page);
  await page.emulateMedia({ colorScheme: "dark" });
  await page.route("**/api/projects/*/script", async (route) => {
    await new Promise((r) => setTimeout(r, 1500));
    await route.continue();
  });
  await page.goto(`/prompter/${id}`);
  const loading = page.getByTestId("prompter-loading");
  await expect(loading).toBeVisible();
  await expect(loading).toHaveAttribute("aria-busy", "true");
  await expect(loading).toHaveCSS("background-color", "rgb(0, 0, 0)");
  const box = await loading.locator(".prompter-spinner").boundingBox();
  expect(box?.width ?? 0).toBeGreaterThanOrEqual(64);
  await expect(text(page)).toContainText(SPOKEN[0]);
});

test("offline: the cached script keeps working and marks sync when back", async ({ page }) => {
  const id = await lockedProject(page);
  await openPrompter(page, id);

  await page.route("**/api/**", (route) => route.abort());
  await page.reload();
  await expect(text(page)).toContainText(SPOKEN[0]);
  await expect(page.getByRole("alert")).toContainText("Lost connection — keep reading, resyncing");

  await page.keyboard.press("r");
  await expect(page.getByTestId("prompter-status")).toContainText("will sync");
  expect(await page.evaluate(() => localStorage.getItem("rw.prompter.pendingMarks"))).toContain('"retake"');

  await page.unroute("**/api/**");
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await expect.poll(async () => (await marks(page, id)).length).toBe(1);
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem("rw.prompter.pendingMarks"))).toBeNull();
});

for (const [width, height] of [
  [360, 640],
  [740, 360],
  [768, 1024],
  [1280, 800],
]) {
  test(`fits ${width}x${height} with controls visible`, async ({ page }) => {
    const id = await lockedProject(page);
    await page.setViewportSize({ width, height });
    await openPrompter(page, id);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    for (const name of ["Start", "Back one line", "Mark retake", "Settings"]) {
      await expect(page.getByRole("button", { name })).toBeInViewport();
    }
  });
}

function luminance(rgb: string): number {
  const [r, g, b] = (rgb.match(/\d+(\.\d+)?/g) ?? []).slice(0, 3).map((v) => {
    const c = Number(v) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

test("text contrast is at least 7:1 in dark and light", async ({ page }) => {
  const id = await lockedProject(page);
  for (const colorScheme of ["dark", "light"] as const) {
    await page.emulateMedia({ colorScheme });
    await openPrompter(page, id);
    const fg = await text(page).evaluate((el) => getComputedStyle(el).color);
    const bg = await page.locator(".prompter-page").evaluate((el) => getComputedStyle(el).backgroundColor);
    const [hi, lo] = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
    expect((hi + 0.05) / (lo + 0.05), `${colorScheme}: ${fg} on ${bg}`).toBeGreaterThanOrEqual(7);
  }
});

test("the dyslexia option changes the font", async ({ page }) => {
  const id = await lockedProject(page);
  await openPrompter(page, id);
  const before = await text(page).evaluate((el) => getComputedStyle(el).fontFamily);
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByLabel("Dyslexia-friendly font").check();
  await expect.poll(() => text(page).evaluate((el) => getComputedStyle(el).fontFamily)).not.toBe(before);
  expect(await text(page).evaluate((el) => getComputedStyle(el).fontFamily)).toContain("Atkinson Hyperlegible");
});

test("reduced motion advances whole lines", async ({ page }) => {
  const id = await lockedProject(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await openPrompter(page, id);
  const tops = await text(page).evaluate((el) =>
    [...el.querySelectorAll<HTMLElement>("[data-line]")].map((line) => -line.offsetTop),
  );
  await page.keyboard.press("Space");
  const samples: number[] = [];
  for (let i = 0; i < 25; i += 1) {
    samples.push(await translateY(page));
    await page.waitForTimeout(100);
  }
  await page.keyboard.press("Space");
  for (const y of samples) expect(tops.some((top) => Math.abs(top - y) < 0.5), `translate ${y}`).toBe(true);
  expect(new Set(samples).size).toBeGreaterThan(1);
});

test("smooth scroll moves a little every frame, with no CSS animation on the text", async ({ page }) => {
  const id = await lockedProject(page);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await openPrompter(page, id);
  await page.keyboard.press("Space");
  const result = await text(page).evaluate(
    (el) =>
      new Promise<{ ys: number[]; lineHeight: number; animations: number }>((resolve) => {
        const ys: number[] = [];
        const lineHeight = (el.querySelector<HTMLElement>("[data-line]")?.offsetHeight ?? 0) as number;
        const sample = () => {
          ys.push(new DOMMatrix(getComputedStyle(el).transform).m42);
          if (ys.length < 60) requestAnimationFrame(sample);
          else {
            const animations = document.getAnimations().filter((a) => {
              const target = (a.effect as KeyframeEffect | null)?.target;
              return target instanceof Node && el.contains(target);
            }).length;
            resolve({ ys, lineHeight, animations });
          }
        };
        requestAnimationFrame(sample);
      }),
  );
  await page.keyboard.press("Space");
  const deltas = result.ys.slice(1).map((y, i) => result.ys[i] - y);
  for (const d of deltas) {
    expect(d).toBeGreaterThanOrEqual(0);
    expect(d).toBeLessThan(result.lineHeight);
  }
  expect(result.ys[0]).toBeGreaterThan(result.ys[result.ys.length - 1]);
  expect(result.animations).toBe(0);
});

// #50: camera control is optional. With no camera paired the prompter behaves exactly as before.
const NO_CAMERA = /No camera paired|Camera control needs Chrome on Android/;

test("with no camera paired the prompter still reads and marks, and never opens a Bluetooth chooser", async ({ page }) => {
  await page.addInitScript(() => {
    const w = window as unknown as { __chooser: number };
    w.__chooser = 0;
    const bt = (navigator as Navigator & { bluetooth?: { requestDevice?: unknown; getDevices?: unknown } }).bluetooth;
    if (bt) {
      for (const name of ["requestDevice", "getDevices"] as const) {
        Object.defineProperty(bt, name, {
          configurable: true,
          value: () => {
            w.__chooser += 1;
            return Promise.reject(new Error("no device in e2e"));
          },
        });
      }
    }
  });
  const takeRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/takes")) takeRequests.push(request.url());
  });
  const id = await lockedProject(page, "No camera");
  await openPrompter(page, id);
  await expect(page.getByTestId("camera-status")).toContainText(NO_CAMERA);
  await expect(page.getByRole("button", { name: "Stop" })).toHaveCount(0);

  await page.keyboard.press("Space");
  await expect.poll(() => activeLine(page), { timeout: 5000 }).not.toBe("8");
  await page.keyboard.press("s");
  await expect(page.getByRole("button", { name: "Start" })).toBeVisible();
  await page.keyboard.press("r");
  await expect.poll(async () => (await marks(page, id)).length).toBe(1);

  expect(await page.evaluate(() => (window as unknown as { __chooser: number }).__chooser)).toBe(0);
  expect(takeRequests).toEqual([]);
});

test("at 360x740 the camera pill is visible beside the controls", async ({ page }) => {
  const id = await lockedProject(page, "Camera pill");
  await page.setViewportSize({ width: 360, height: 740 });
  await openPrompter(page, id);
  await expect(page.getByTestId("camera-status")).toBeInViewport();
  await expect(page.getByTestId("camera-status")).toContainText(NO_CAMERA);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  for (const name of ["Start", "Back one line", "Mark retake", "Settings"]) {
    await expect(page.getByRole("button", { name })).toBeInViewport();
  }
});
