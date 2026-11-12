// SPDX-License-Identifier: AGPL-3.0-or-later
// Voice follow (#7): streams the phone mic to WS /api/prompter/:id/align as PCM16LE mono 16 kHz
// binary frames of ~100 ms and reports the script line the server hears. Audio only ever lives in
// memory. A ping every second measures the round trip; p95Ms() is the latency readout.

export type VoiceState = "idle" | "connecting" | "listening" | "stopped";

export const TARGET_RATE = 16_000;
/** Samples per binary frame: 100 ms at 16 kHz. */
export const FRAME_SAMPLES = 1_600;
export const PING_MS = 1_000;
export const RTT_WINDOW = 50;
const WS_OPEN = 1;
const WS_CLOSED = 3;

interface ServerMessage {
  type: string;
  line_no?: number;
  code?: string;
  t?: number;
}

export interface VoiceFollowOptions {
  projectId: string | number;
  onPosition(lineNo: number): void;
  onError(code: string): void;
  onStateChange?(state: VoiceState): void;
  /** Injected in tests; defaults to the browser's WebSocket. */
  createSocket?(url: string): WebSocket;
  /** Injected in tests; defaults to getUserMedia plus an AudioContext. */
  openMic?(onAudio: (samples: Float32Array, sampleRate: number) => void): Promise<() => void>;
  now?(): number;
}

/** ws(s)://<host>/api/prompter/<id>/align for the page's own origin. */
export function alignUrl(projectId: string | number, loc: Pick<Location, "protocol" | "host"> = window.location): string {
  const scheme = loc.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${loc.host}/api/prompter/${encodeURIComponent(String(projectId))}/align`;
}

/** Box-filter downsample to `outRate` and convert to 16-bit PCM, clipping to [-1, 1]. */
export function downsampleToPcm16(input: Float32Array, inRate: number, outRate = TARGET_RATE): Int16Array {
  const ratio = inRate / outRate;
  if (ratio < 1) throw new Error("input rate below target rate");
  const out = new Int16Array(Math.floor(input.length / ratio));
  for (let i = 0; i < out.length; i += 1) {
    const from = Math.floor(i * ratio);
    const to = Math.max(from + 1, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let k = from; k < to; k += 1) sum += input[k];
    const v = Math.max(-1, Math.min(1, sum / (to - from)));
    out[i] = v < 0 ? Math.round(v * 0x8000) : Math.round(v * 0x7fff);
  }
  return out;
}

/** 95th percentile (nearest rank); null for no samples. */
export function p95(values: readonly number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.ceil(0.95 * sorted.length) - 1)];
}

/** PCM16 little-endian bytes, whatever the platform's byte order. */
function toLittleEndian(samples: Int16Array): ArrayBuffer {
  const view = new DataView(new ArrayBuffer(samples.length * 2));
  samples.forEach((s, i) => view.setInt16(i * 2, s, true));
  return view.buffer;
}

async function browserMic(onAudio: (samples: Float32Array, sampleRate: number) => void): Promise<() => void> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  const context = new AudioContext();
  const source = context.createMediaStreamSource(stream);
  // ScriptProcessor keeps this to one file; at 4096 samples it is ~85 ms per callback at 48 kHz.
  const processor = context.createScriptProcessor(4096, 1, 1);
  processor.onaudioprocess = (event) => onAudio(new Float32Array(event.inputBuffer.getChannelData(0)), context.sampleRate);
  source.connect(processor);
  processor.connect(context.destination);
  return () => {
    processor.onaudioprocess = null;
    source.disconnect();
    processor.disconnect();
    stream.getTracks().forEach((track) => track.stop());
    void context.close();
  };
}

export class VoiceFollow {
  private socket: WebSocket | null = null;
  private closeMic: (() => void) | null = null;
  private pingTimer: ReturnType<typeof setInterval> | undefined;
  private pending: Int16Array[] = [];
  private pendingSamples = 0;
  private rtts: number[] = [];
  private state: VoiceState = "idle";

  constructor(private readonly options: VoiceFollowOptions) {}

  get current(): VoiceState {
    return this.state;
  }

  /** Round-trip p95 over the last RTT_WINDOW pings, in ms. */
  p95Ms(): number | null {
    return p95(this.rtts);
  }

  async start({ scriptVersion, lineNo }: { scriptVersion: number; lineNo: number }): Promise<void> {
    if (this.state === "connecting" || this.state === "listening") return;
    this.setState("connecting");
    const now = this.options.now ?? (() => performance.now());
    const socket = (this.options.createSocket ?? ((url: string) => new WebSocket(url)))(alignUrl(this.options.projectId));
    socket.binaryType = "arraybuffer";
    this.socket = socket;
    socket.onopen = () => {
      socket.send(JSON.stringify({ type: "start", script_version: scriptVersion, line_no: lineNo }));
      this.pingTimer = setInterval(() => this.send(JSON.stringify({ type: "ping", t: now() })), PING_MS);
    };
    socket.onmessage = (event: MessageEvent) => {
      if (typeof event.data !== "string") return;
      let message: ServerMessage;
      try {
        message = JSON.parse(event.data) as ServerMessage;
      } catch {
        return;
      }
      if (message.type === "position" && typeof message.line_no === "number") {
        this.options.onPosition(message.line_no);
      } else if (message.type === "pong" && typeof message.t === "number") {
        this.rtts.push(now() - message.t);
        if (this.rtts.length > RTT_WINDOW) this.rtts.shift();
      } else if (message.type === "error") {
        this.fail(message.code ?? "server_error");
      }
    };
    socket.onclose = () => {
      if (this.socket === socket) this.fail("connection_lost");
    };
    try {
      const close = await (this.options.openMic ?? browserMic)((samples, rate) => this.onAudio(samples, rate));
      if (this.socket !== socket) {
        close(); // stopped while the permission prompt was up
        return;
      }
      this.closeMic = close;
      this.setState("listening");
    } catch {
      this.fail("mic_denied");
    }
  }

  /** The reader moved by hand: listen from the start of this line. */
  seek(lineNo: number): void {
    this.send(JSON.stringify({ type: "seek", line_no: lineNo }));
  }

  stop(): void {
    this.teardown();
    if (this.state !== "idle") this.setState("stopped");
  }

  private onAudio(samples: Float32Array, rate: number): void {
    const pcm = downsampleToPcm16(samples, rate);
    this.pending.push(pcm);
    this.pendingSamples += pcm.length;
    if (this.pendingSamples < FRAME_SAMPLES) return;
    const frame = new Int16Array(this.pendingSamples);
    let at = 0;
    for (const chunk of this.pending) {
      frame.set(chunk, at);
      at += chunk.length;
    }
    this.pending = [];
    this.pendingSamples = 0;
    this.send(toLittleEndian(frame));
  }

  private send(data: string | ArrayBuffer): void {
    if (this.socket && this.socket.readyState === WS_OPEN) this.socket.send(data);
  }

  private fail(code: string): void {
    if (this.state === "stopped" || this.state === "idle") return;
    this.teardown();
    this.setState("stopped");
    this.options.onError(code);
  }

  private teardown(): void {
    clearInterval(this.pingTimer);
    this.pingTimer = undefined;
    this.closeMic?.();
    this.closeMic = null;
    const socket = this.socket;
    this.socket = null;
    this.pending = [];
    this.pendingSamples = 0;
    if (socket && socket.readyState !== WS_CLOSED) socket.close();
  }

  private setState(state: VoiceState): void {
    this.state = state;
    this.options.onStateChange?.(state);
  }
}
