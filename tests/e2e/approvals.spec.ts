// SPDX-License-Identifier: AGPL-3.0-or-later
// Journey: a finished render's draft waits in /approvals; keyboard-only approve uploads it (mock YouTube) and the
// card lands in Live with the remote id; a scheduled one lands in Scheduled; then the queue is empty. Also the
// 360/768/1280 layouts and both colour schemes. Real stack: `rw serve` + the built bundle, seeded via page.request.
import { execFileSync } from "node:child_process";

import { type APIRequestContext, expect, type Page, test } from "@playwright/test";

const WORKER = "e2e-gpu";
const OUT = "D:\\out\\approvals-e2e.mp4";

// A 90 s edit with three beats (hook, problem, outro) as the rough cut writes them: one marker per script line.
const OTIO = execFileSync(
  "uv",
  [
    "run",
    "python",
    "-c",
    [
      "from spectracut.edit import Clip, Edit, FrameRate, Marker, Track",
      "from reelwright.edit.build import edit_to_otio_json",
      "clip = Clip(id='c1', media_ref='D:/f/a.mp4', source_in=0, source_out=2700)",
      "marks = [Marker(name=n, frame=f) for n, f in [('hook:2', 0), ('problem:5', 900), ('outro:9', 1800)]]",
      "print(edit_to_otio_json(Edit(id='e', rate=FrameRate(numerator=30), tracks=[Track(id='v', items=[clip])], markers=marks)))",
    ].join("\n"),
  ],
  { encoding: "utf8" },
);

interface Pub {
  id: number;
  status: string;
  payload_hash: string;
}

async function ok<T>(response: Awaited<ReturnType<APIRequestContext["get"]>>): Promise<T> {
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()) as T;
}

/** A project with a render driven to output through the worker endpoints, and a draft for it. */
async function seedDraft(request: APIRequestContext, title: string): Promise<Pub> {
  const project = await ok<{ id: number }>(await request.post("/api/projects", { data: { title } }));
  const beat = { name: WORKER, vram_total_mb: 12000, vram_free_mb: 8000, capabilities: ["nvenc"] };
  await ok(await request.post("/api/workers/heartbeat", { data: beat }));
  const render = await ok<{ id: number }>(
    await request.post(`/api/projects/${project.id}/renders`, { data: { otio: OTIO, output_path: OUT } }),
  );
  for (let i = 0; i < 10; i++) {
    const current = await ok<{ status: string }>(await request.get(`/api/projects/${project.id}/renders/${render.id}`));
    if (current.status === "previewing" || current.status === "done") break;
    const job = await request.get(`/api/workers/next?worker=${WORKER}`);
    if (job.status() !== 200) continue;
    const { id } = (await job.json()) as { id: number };
    await ok(
      await request.post(`/api/workers/jobs/${id}/complete`, {
        data: { worker: WORKER, ok: true, outputs: { returncode: 0 } },
      }),
    );
  }
  return ok<Pub>(
    await request.post(`/api/projects/${project.id}/publish`, {
      data: { render_id: render.id, title, description: "Made on the real stack.", tags: ["e2e"] },
    }),
  );
}

/** A retried run may leave drafts behind; approve them so the empty state is reachable. */
async function drainWaiting(request: APIRequestContext) {
  for (const status of ["draft", "failed"]) {
    for (const p of await ok<Pub[]>(await request.get(`/api/publish?status=${status}`))) {
      await request.post(`/api/publish/${p.id}/approve`, { data: { approved_by: "e2e", payload_hash: p.payload_hash } });
    }
  }
}

async function tabTo(page: Page, name: string) {
  for (let i = 0; i < 60; i++) {
    await page.keyboard.press("Tab");
    const label = await page.evaluate(() => document.activeElement?.textContent?.trim() ?? "");
    if (label === name) return;
  }
  throw new Error(`could not reach "${name}" with Tab`);
}

test.describe.configure({ mode: "serial" });

test("approve with the keyboard, schedule, then the queue is empty", async ({ page, request }) => {
  await drainWaiting(request);
  const stamp = Date.now();
  const now = await seedDraft(request, `Approve now ${stamp}`);
  const later = await seedDraft(request, `Schedule later ${stamp}`);

  await page.goto("/");
  await page.getByRole("link", { name: "Approvals" }).click();
  await expect(page.getByRole("heading", { name: "Approvals", level: 1 })).toBeVisible();

  const waiting = page.getByRole("region", { name: /^Waiting/ });
  await expect(waiting.getByTestId(`publication-${now.id}`)).toBeVisible();
  await waiting.getByTestId(`publication-${now.id}`).getByRole("button").focus();
  await page.keyboard.press("Enter");

  const preview = page.getByRole("region", { name: "Preview" });
  await expect(preview.getByRole("heading", { name: `Approve now ${stamp}` })).toBeVisible();
  await expect(preview).toContainText("Chapters\n0:00 Hook\n0:30 Problem\n1:00 Outro");
  const meta = page.getByRole("region", { name: "Metadata" });
  await expect(meta.getByRole("row", { name: /title Added/ })).toBeVisible();
  await expect(meta.getByRole("row", { name: /tags/ })).toContainText("e2e");

  await tabTo(page, "Approve");
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "Publish on YouTube?" })).toBeVisible();
  await page.keyboard.press("Escape"); // cancel: nothing is sent
  await expect(dialog).toBeHidden();
  await expect(waiting.getByTestId(`publication-${now.id}`)).toBeVisible();

  await tabTo(page, "Approve");
  await page.keyboard.press("Enter");
  await expect(dialog.getByLabel("Approved by")).toBeFocused();
  await page.keyboard.type("Yash");
  await page.keyboard.press("Enter"); // submits the confirm form
  const live = page.getByRole("region", { name: /^Live/ });
  const liveCard = live.getByTestId(`publication-${now.id}`);
  await expect(liveCard).toContainText(/mock-\d+/);
  const stored = await ok<{ status: string; remote_id: string }>(await request.get(`/api/publish/${now.id}`));
  expect(stored.status).toBe("live");
  expect(stored.remote_id).toMatch(/^mock-\d+$/);

  await waiting.getByTestId(`publication-${later.id}`).getByRole("button").click();
  await page.getByLabel("Schedule for").fill("2035-06-01T09:30");
  await page.getByRole("button", { name: "Schedule", exact: true }).click();
  await expect(dialog.getByRole("heading", { name: "Schedule on YouTube?" })).toBeVisible();
  await dialog.getByRole("button", { name: "Confirm schedule" }).click();
  await expect(page.getByRole("region", { name: /^Scheduled/ }).getByTestId(`publication-${later.id}`)).toBeVisible();
  await expect(waiting.getByText("Nothing waiting for approval")).toBeVisible();
});

test("layouts at 360, 768 and 1280 in dark and light", async ({ page, request }) => {
  await seedDraft(request, `Layout ${Date.now()}`);
  for (const colorScheme of ["dark", "light"] as const) {
    await page.emulateMedia({ colorScheme });
    for (const width of [360, 768, 1280]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto("/approvals");
      const list = page.getByRole("region", { name: "Publications" });
      const preview = page.getByRole("region", { name: "Preview" });
      const meta = page.getByRole("region", { name: "Metadata" });
      await expect(meta).toBeVisible();
      const [l, p, m] = await Promise.all([list.boundingBox(), preview.boundingBox(), meta.boundingBox()]);
      if (!l || !p || !m) throw new Error("layout regions not rendered");
      if (width === 360) {
        expect(p.y).toBeGreaterThanOrEqual(l.y + l.height - 1); // stacked cards
        expect(m.y).toBeGreaterThanOrEqual(p.y + p.height - 1);
      } else if (width === 768) {
        expect(Math.abs(p.y - l.y)).toBeLessThan(2); // list + preview side by side
        expect(p.x).toBeGreaterThan(l.x + l.width - 1);
        expect(m.y).toBeGreaterThanOrEqual(p.y + p.height - 1);
      } else {
        expect(Math.abs(m.y - l.y)).toBeLessThan(2); // list, preview and metadata in three columns
        expect(m.x).toBeGreaterThan(p.x + p.width - 1);
      }
      const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
      expect(bg).toBe(colorScheme === "dark" ? "rgb(15, 17, 21)" : "rgb(255, 255, 255)");
    }
  }
});
