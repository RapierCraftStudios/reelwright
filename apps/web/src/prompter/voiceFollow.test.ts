// SPDX-License-Identifier: AGPL-3.0-or-later
import { afterEach, describe, expect, it, vi } from "vitest";

import { FRAME_SAMPLES, VoiceFollow, type VoiceFollowOptions, alignUrl, downsampleToPcm16, p95 } from "./voiceFollow";

class FakeSocket {
  static last: FakeSocket | null = null;
  readyState = 0;
  binaryType = "blob";
  sent: (string | ArrayBuffer)[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  closed = false;

  constructor(public url: string) {
    FakeSocket.last = this;
  }

  send(data: string | ArrayBuffer) {
    this.sent.push(data);
  }

  close() {
    this.closed = true;
    this.readyState = 3;
  }

  open() {
    this.readyState = 1;
    this.onopen?.();
  }

  receive(message: unknown) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  json(): Record<string, unknown>[] {
    return this.sent.filter((d): d is string => typeof d === "string").map((d) => JSON.parse(d) as Record<string, unknown>);
  }
}

function setup(overrides: Partial<VoiceFollowOptions> = {}) {
  const onPosition = vi.fn();
  const onError = vi.fn();
  const micStop = vi.fn();
  let feed: ((samples: Float32Array, rate: number) => void) | null = null;
  const follow = new VoiceFollow({
    projectId: 7,
    onPosition,
    onError,
    createSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
    openMic: async (onAudio) => {
      feed = onAudio;
      return micStop;
    },
    ...overrides,
  });
  return { follow, onPosition, onError, micStop, feed: (s: Float32Array, r: number) => feed?.(s, r) };
}

afterEach(() => {
  vi.useRealTimers();
  FakeSocket.last = null;
});

describe("downsampleToPcm16", () => {
  it("turns 48 kHz into a third as many 16 kHz samples", () => {
    const out = downsampleToPcm16(new Float32Array(4800).fill(0.5), 48_000);
    expect(out).toHaveLength(1600);
    expect(out[0]).toBe(Math.round(0.5 * 0x7fff));
  });

  it("clips out-of-range samples to full scale", () => {
    const out = downsampleToPcm16(Float32Array.from([2, 2, 2, -3, -3, -3]), 48_000);
    expect([...out]).toEqual([32767, -32768]);
  });

  it("keeps 16 kHz input as is", () => {
    expect(downsampleToPcm16(new Float32Array(160), 16_000)).toHaveLength(160);
  });
});

describe("p95", () => {
  it("is null without samples and the nearest-rank 95th percentile otherwise", () => {
    expect(p95([])).toBeNull();
    expect(p95([5])).toBe(5);
    expect(p95(Array.from({ length: 100 }, (_, i) => 100 - i))).toBe(95);
  });
});

describe("alignUrl", () => {
  it("uses ws for http and wss for https on the page's host", () => {
    expect(alignUrl(3, { protocol: "http:", host: "127.0.0.1:8765" })).toBe("ws://127.0.0.1:8765/api/prompter/3/align");
    expect(alignUrl("3", { protocol: "https:", host: "studio.ts.net" })).toBe("wss://studio.ts.net/api/prompter/3/align");
  });
});

describe("VoiceFollow", () => {
  it("sends start with the line, then audio as PCM16 frames, and reports positions", async () => {
    const { follow, onPosition, feed } = setup();
    const started = follow.start({ scriptVersion: 2, lineNo: 12 });
    const socket = FakeSocket.last!;
    expect(socket.url).toMatch(/\/api\/prompter\/7\/align$/);
    socket.open();
    await started;
    expect(follow.current).toBe("listening");
    expect(socket.json()[0]).toEqual({ type: "start", script_version: 2, line_no: 12 });
    feed(new Float32Array(4096), 48_000); // 1365 samples: under one frame
    expect(socket.sent.filter((d) => typeof d !== "string")).toHaveLength(0);
    feed(new Float32Array(4096), 48_000);
    const frames = socket.sent.filter((d): d is ArrayBuffer => typeof d !== "string");
    expect(frames).toHaveLength(1);
    expect(frames[0].byteLength).toBe(2 * 2730);
    expect(frames[0].byteLength).toBeGreaterThanOrEqual(2 * FRAME_SAMPLES);
    socket.receive({ type: "position", line_no: 14, word: 2, confidence: 1, server_ms: 1 });
    expect(onPosition).toHaveBeenCalledWith(14);
  });

  it("sends seek, pings every second and keeps the round trip p95", async () => {
    vi.useFakeTimers();
    let clock = 1000;
    const { follow } = setup({ now: () => clock });
    const started = follow.start({ scriptVersion: 1, lineNo: 8 });
    const socket = FakeSocket.last!;
    socket.open();
    await started;
    follow.seek(10);
    expect(socket.json()[1]).toEqual({ type: "seek", line_no: 10 });
    vi.advanceTimersByTime(1000);
    const ping = socket.json()[2];
    expect(ping).toEqual({ type: "ping", t: 1000 });
    clock = 1042;
    socket.receive({ type: "pong", t: ping.t });
    expect(follow.p95Ms()).toBe(42);
    follow.stop();
  });

  it("reports a server error, then stops the mic and the socket", async () => {
    const { follow, onError, micStop } = setup();
    const started = follow.start({ scriptVersion: 1, lineNo: 8 });
    const socket = FakeSocket.last!;
    socket.open();
    await started;
    socket.receive({ type: "error", code: "asr_unavailable" });
    expect(onError).toHaveBeenCalledWith("asr_unavailable");
    expect(micStop).toHaveBeenCalled();
    expect(socket.closed).toBe(true);
    expect(follow.current).toBe("stopped");
  });

  it("reports mic_denied when the microphone is refused", async () => {
    const { follow, onError } = setup({ openMic: () => Promise.reject(new DOMException("denied", "NotAllowedError")) });
    await follow.start({ scriptVersion: 1, lineNo: 8 });
    expect(onError).toHaveBeenCalledWith("mic_denied");
    expect(FakeSocket.last!.closed).toBe(true);
  });

  it("stop closes the socket and the mic without reporting an error", async () => {
    const { follow, onError, micStop } = setup();
    const started = follow.start({ scriptVersion: 1, lineNo: 8 });
    const socket = FakeSocket.last!;
    socket.open();
    await started;
    follow.stop();
    socket.onclose?.();
    expect(socket.closed).toBe(true);
    expect(micStop).toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

  it("reports connection_lost when the server closes the socket", async () => {
    const { follow, onError } = setup();
    const started = follow.start({ scriptVersion: 1, lineNo: 8 });
    const socket = FakeSocket.last!;
    socket.open();
    await started;
    socket.onclose?.();
    expect(onError).toHaveBeenCalledWith("connection_lost");
  });
});
