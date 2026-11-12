// SPDX-License-Identifier: AGPL-3.0-or-later
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TakeCreate } from "../../api";
import { PENDING_KEY, flushPending, pendingCount, submitMark } from "../marks";
import { type CameraPort, TakeSession } from "./takes";

const HOOK_1 = { lineNo: 6, beat: "hook" };
const HOOK_2 = { lineNo: 7, beat: "hook" };
const CTA_1 = { lineNo: 9, beat: "cta" };
const CTA_2 = { lineNo: 10, beat: "cta" };

function mockCamera(): CameraPort & { calls: string[] } {
  const calls: string[] = [];
  return {
    calls,
    name: "Osmo Action 5 Pro",
    start: vi.fn(async () => void calls.push("start")),
    stop: vi.fn(async () => void calls.push("stop")),
  };
}

function session(opts: Partial<ConstructorParameters<typeof TakeSession>[0]> = {}) {
  const camera = mockCamera();
  const markers: TakeCreate[] = [];
  let tick = 0;
  let id = 0;
  const take = new TakeSession({
    projectId: "7",
    scriptVersion: 2,
    camera,
    submit: async (_project, body) => {
      markers.push(body);
      return "sent";
    },
    now: () => new Date(Date.UTC(2026, 10, 12, 10, 0, tick++)),
    uuid: () => `take-${++id}`,
    ...opts,
  });
  return { take, camera, markers };
}

beforeEach(() => localStorage.clear());
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("TakeSession", () => {
  it("Go starts recording and a beat change stops it with a marker for the beat's lines", async () => {
    const { take, camera, markers } = session();
    await take.go(HOOK_1);
    expect(camera.calls).toEqual(["start"]);
    expect(take.isRecording).toBe(true);
    await take.line(HOOK_2);
    await take.line(CTA_1);
    expect(camera.calls).toEqual(["start", "stop"]);
    expect(markers).toEqual([
      {
        client_id: "take-1",
        script_version: 2,
        beat_slug: "hook",
        line_from: 6,
        line_to: 7,
        retake: false,
        continuous: false,
        started_at: "2026-11-12T10:00:00.000Z",
        stopped_at: "2026-11-12T10:00:01.000Z",
        camera: "Osmo Action 5 Pro",
      },
    ]);
    // the next Go records the next beat
    await take.go(CTA_1);
    await take.stop();
    expect(camera.calls).toEqual(["start", "stop", "start", "stop"]);
    expect(markers.map((m) => [m.beat_slug, m.line_from, m.line_to])).toEqual([
      ["hook", 6, 7],
      ["cta", 9, 9],
    ]);
  });

  it("stop and pause stop the camera in normal mode; a second stop is a no-op", async () => {
    const { take, camera, markers } = session();
    await take.go(HOOK_1);
    await take.pause();
    await take.go(HOOK_2);
    await take.stop();
    await take.stop();
    expect(camera.calls).toEqual(["start", "stop", "start", "stop"]);
    expect(markers).toHaveLength(2);
  });

  it("stamps started_at when the camera acknowledges the start", async () => {
    let ack!: () => void;
    const camera = mockCamera();
    camera.start = () => new Promise<void>((resolve) => (ack = resolve));
    let now = 1000;
    const { take, markers } = session({ camera, now: () => new Date(now) });
    const going = take.go(HOOK_1);
    await vi.waitFor(() => expect(ack).toBeTypeOf("function"));
    now = 5000;
    ack();
    await going;
    await take.stop();
    expect(markers[0]!.started_at).toBe(new Date(5000).toISOString());
  });

  it("continuous mode starts once and marks each beat without stopping", async () => {
    const { take, camera, markers } = session({ continuous: true });
    await take.go(HOOK_1);
    await take.line(HOOK_2);
    await take.line(CTA_1);
    await take.line(CTA_2);
    await take.pause();
    await take.go(CTA_2);
    expect(camera.calls).toEqual(["start"]);
    await take.stop();
    expect(camera.calls).toEqual(["start", "stop"]);
    expect(markers.map((m) => [m.beat_slug, m.line_from, m.line_to, m.continuous])).toEqual([
      ["hook", 6, 7, true],
      ["cta", 9, 10, true],
      ["cta", 10, 10, true],
    ]);
  });

  it("flags the take as a retake when a retake mark was pressed during it", async () => {
    const { take, markers } = session();
    take.retake(); // nothing open: ignored
    await take.go(HOOK_1);
    take.retake();
    await take.line(CTA_1);
    await take.go(CTA_1);
    await take.stop();
    expect(markers.map((m) => m.retake)).toEqual([true, false]);
  });

  it("keeps the marker when the camera fails to stop, and reports the error", async () => {
    const onError = vi.fn();
    const camera = mockCamera();
    camera.stop = vi.fn().mockRejectedValue(new Error("camera disconnected"));
    const { take, markers } = session({ camera, onError });
    await take.go(HOOK_1);
    await take.stop();
    expect(markers).toHaveLength(1);
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ message: "camera disconnected" }));
  });

  it("queues markers while the server is down and flushes them in order when it is back", async () => {
    const calls: { url: string; body: { beat_slug?: string; kind?: string } }[] = [];
    let online = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (!online) throw new TypeError("Failed to fetch");
        calls.push({ url, body: JSON.parse(String(init?.body)) });
        return new Response(JSON.stringify({ id: calls.length }), { status: 201 });
      }),
    );
    const { take } = session({ submit: undefined });
    await take.go(HOOK_1);
    await take.line(CTA_1);
    await submitMark("7", {
      kind: "retake",
      script_version: 2,
      beat_slug: "cta",
      line_no: 9,
      marked_at: "2026-11-12T10:00:05Z",
      offset_ms: 0,
    });
    await take.go(CTA_1);
    await take.stop();
    expect(pendingCount("7")).toBe(3);
    const stored = JSON.parse(localStorage.getItem(PENDING_KEY)!) as { type?: string }[];
    expect(stored.map((e) => e.type)).toEqual(["take", undefined, "take"]);

    online = true;
    expect(await flushPending()).toBe(0);
    expect(calls.map((c) => [c.url, c.body.beat_slug ?? c.body.kind])).toEqual([
      ["/api/projects/7/takes", "hook"],
      ["/api/prompter/7/marks", "cta"],
      ["/api/projects/7/takes", "cta"],
    ]);
  });

  it("still flushes legacy queued retake marks that have no type", async () => {
    const legacy = { kind: "retake", script_version: 1, beat_slug: "hook", line_no: 6, marked_at: "x", offset_ms: 0 };
    localStorage.setItem(PENDING_KEY, JSON.stringify([{ projectId: "7", body: legacy }]));
    const fetchMock = vi.fn(async () => new Response("{}", { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await flushPending()).toBe(0);
    expect(fetchMock).toHaveBeenCalledWith("/api/prompter/7/marks", expect.objectContaining({ method: "POST" }));
  });
});
