// SPDX-License-Identifier: AGPL-3.0-or-later
// DJI Osmo Action commands over a BleLink (#50): connect handshake, record start/stop, mode switch
// and status subscription, plus CameraController, which pairs once, remembers the camera and
// reconnects on its own. Byte layouts follow DJI's Osmo GPS Controller demo (protocol docs).
import {
  type BleDevice,
  type BleLink,
  type DjiFrame,
  FrameReader,
  RESPONSE_BIT,
  bluetooth,
  connectLink,
  encodeFrame,
  requestDjiDevice,
} from "./djiBle";

export const CMD = {
  connect: { set: 0x00, id: 0x19 },
  statusPush: { set: 0x1d, id: 0x02 },
  record: { set: 0x1d, id: 0x03 },
  mode: { set: 0x1d, id: 0x04 },
  subscribe: { set: 0x1d, id: 0x05 },
} as const;

/** CmdType values: a command that wants a result, one that does not, and a response. */
export const CMD_TYPE = { waitResult: 0x02, noResponse: 0x00, response: RESPONSE_BIT } as const;

/** DjiCommandError.code when the user rejected pairing on the camera (no automatic retry). */
export const REJECTED = -1;

export const CAMERA_KEY = "rw.prompter.camera";
export const DEFAULT_TIMEOUT_MS = 5000;

/** temp_over: 0 ok, 1 warm (warning), 2 too hot to record, 3 overheating. */
export type TempWarning = 0 | 1 | 2 | 3;

export interface CameraStatus {
  mode: number;
  recording: boolean;
  recordTimeS: number;
  batteryPct: number;
  remainCapacityMb: number;
  remainTimeS: number;
  tempWarning: TempWarning;
}

export class DjiCommandError extends Error {
  readonly code: number | null;

  constructor(message: string, code: number | null = null) {
    super(message);
    this.name = "DjiCommandError";
    this.code = code;
  }
}

/** Decode a 1D/02 camera status push. */
export function parseStatusPush(payload: Uint8Array): CameraStatus {
  if (payload.length < 38) throw new DjiCommandError(`status push of ${payload.length} bytes is too short`);
  const view = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
  const temp = payload[30]!;
  return {
    mode: payload[0]!,
    recording: payload[1] === 0x03,
    recordTimeS: view.getUint16(5, true),
    remainCapacityMb: view.getUint32(15, true),
    remainTimeS: view.getUint32(23, true),
    tempWarning: (temp <= 3 ? temp : 3) as TempWarning,
    batteryPct: payload[37]!,
  };
}

function u32(value: number): number[] {
  return [value & 0xff, (value >>> 8) & 0xff, (value >>> 16) & 0xff, (value >>> 24) & 0xff];
}

function readU32(bytes: Uint8Array, at: number): number {
  return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(at, true);
}

/** The phone's identity towards the camera, persisted so a paired camera recognises it. */
export interface Identity {
  deviceId: number;
  mac: number[];
}

interface Waiter {
  match(frame: DjiFrame): boolean;
  resolve(frame: DjiFrame): void;
  reject(error: Error): void;
  timer: ReturnType<typeof setTimeout>;
}

export interface DjiCameraOptions {
  identity: Identity;
  timeoutMs?: number;
}

/** One connected camera: sequencing, request/response matching and the command set. */
export class DjiCamera {
  private readonly link: BleLink;
  private readonly identity: Identity;
  private readonly timeoutMs: number;
  private seq = 0;
  private waiters: Waiter[] = [];
  private statusCbs: ((status: CameraStatus) => void)[] = [];
  private disconnectCbs: (() => void)[] = [];
  /** The camera's own device id, learnt from its connection request; record_ctrl targets it. */
  cameraId = 0;

  constructor(link: BleLink, opts: DjiCameraOptions) {
    this.link = link;
    this.identity = opts.identity;
    this.timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    const reader = new FrameReader((frame) => this.receive(frame));
    link.onNotify((bytes) => reader.push(bytes));
    link.onDisconnect(() => {
      for (const w of this.waiters.splice(0)) {
        clearTimeout(w.timer);
        w.reject(new DjiCommandError("camera disconnected"));
      }
      for (const cb of this.disconnectCbs) cb();
    });
  }

  onStatus(cb: (status: CameraStatus) => void): void {
    this.statusCbs.push(cb);
  }

  onDisconnect(cb: () => void): void {
    this.disconnectCbs.push(cb);
  }

  disconnect(): void {
    this.link.disconnect();
  }

  /** Handshake: our 00/19 request, its response, the camera's 00/19 verdict, and our reply. */
  async connect(firstPair: boolean): Promise<void> {
    const verifyData = Math.floor(Math.random() * 0x10000);
    const mac = [...this.identity.mac, ...new Array<number>(16 - this.identity.mac.length).fill(0)];
    const payload = [
      ...u32(this.identity.deviceId),
      this.identity.mac.length,
      ...mac,
      ...u32(0), // fw_version
      0, // conidx
      firstPair ? 1 : 0, // verify_mode
      verifyData & 0xff,
      (verifyData >>> 8) & 0xff,
      0,
      0,
      0,
      0,
    ];
    const verdict = this.waitFor((f) => !f.isResponse && f.cmdSet === CMD.connect.set && f.cmdId === CMD.connect.id);
    verdict.catch(() => undefined);
    const response = await this.request(CMD.connect, payload);
    const code = response.payload.length >= 5 ? response.payload[4]! : (response.payload[0] ?? 0);
    if (code !== 0) throw new DjiCommandError(`camera refused the connection (${code})`, code);
    const request = await verdict;
    // Camera request: device_id u32, mac_len, mac[16], fw u32, conidx, verify_mode @26, verify_data u16 @27.
    const p = request.payload;
    this.cameraId = p.length >= 4 ? readU32(p, 0) : 0;
    const allowed = p[26] === 2 && (p[27] ?? 0) === 0 && (p[28] ?? 0) === 0;
    await this.reply(request, [...u32(this.identity.deviceId), allowed ? 0 : 1, 0, 0, 0, 0]);
    if (!allowed) {
      this.link.disconnect();
      throw new DjiCommandError("pairing was rejected on the camera", REJECTED);
    }
  }

  startRecording(): Promise<void> {
    return this.record(0);
  }

  stopRecording(): Promise<void> {
    return this.record(1);
  }

  async switchMode(mode: number): Promise<void> {
    const response = await this.request(CMD.mode, [...u32(this.cameraId), mode & 0xff, 0x01, 0x47, 0x39, 0x36]);
    this.check(response, "mode switch");
  }

  /** Ask the camera to push 1D/02 status: push_mode 3 (periodic + on change), push_freq 20 (2 Hz). */
  async subscribeStatus(): Promise<void> {
    await this.send(CMD_TYPE.noResponse, CMD.subscribe, [0x03, 0x14, 0, 0, 0, 0]);
  }

  private async record(ctrl: 0 | 1): Promise<void> {
    const response = await this.request(CMD.record, [...u32(this.cameraId), ctrl, 0, 0, 0, 0]);
    this.check(response, ctrl === 0 ? "record start" : "record stop");
  }

  private check(response: DjiFrame, what: string): void {
    const code = response.payload[0] ?? 0;
    if (code !== 0) throw new DjiCommandError(`${what} failed (${code})`, code);
  }

  private nextSeq(): number {
    this.seq = (this.seq + 1) & 0xffff;
    return this.seq;
  }

  private async send(cmdType: number, cmd: { set: number; id: number }, payload: number[], seq = this.nextSeq()) {
    await this.link.write(encodeFrame({ cmdType, seq, cmdSet: cmd.set, cmdId: cmd.id, payload }));
    return seq;
  }

  private async request(cmd: { set: number; id: number }, payload: number[]): Promise<DjiFrame> {
    const seq = this.nextSeq();
    const answer = this.waitFor((f) => f.isResponse && f.seq === seq);
    await this.send(CMD_TYPE.waitResult, cmd, payload, seq);
    return answer;
  }

  private async reply(to: DjiFrame, payload: number[]): Promise<void> {
    await this.link.write(
      encodeFrame({ cmdType: CMD_TYPE.response, seq: to.seq, cmdSet: to.cmdSet, cmdId: to.cmdId, payload }),
    );
  }

  private waitFor(match: (frame: DjiFrame) => boolean): Promise<DjiFrame> {
    return new Promise((resolve, reject) => {
      const waiter: Waiter = {
        match,
        resolve,
        reject,
        timer: setTimeout(() => {
          this.waiters = this.waiters.filter((w) => w !== waiter);
          reject(new DjiCommandError("camera did not answer in time"));
        }, this.timeoutMs),
      };
      this.waiters.push(waiter);
    });
  }

  private receive(frame: DjiFrame): void {
    if (frame.cmdSet === CMD.statusPush.set && frame.cmdId === CMD.statusPush.id && !frame.isResponse) {
      try {
        const status = parseStatusPush(frame.payload);
        for (const cb of this.statusCbs) cb(status);
      } catch {
        // a truncated push from older firmware: wait for the next one
      }
      return;
    }
    const waiter = this.waiters.find((w) => w.match(frame));
    if (!waiter) return;
    this.waiters = this.waiters.filter((w) => w !== waiter);
    clearTimeout(waiter.timer);
    waiter.resolve(frame);
  }
}

// --- Pairing, persistence and automatic reconnect ----------------------------------------------

export type CameraPhase = "unsupported" | "empty" | "connecting" | "connected" | "error";

export interface CameraState {
  phase: CameraPhase;
  name: string;
  status: CameraStatus | null;
  error: string;
}

interface Remembered extends Identity {
  bleId?: string;
  name?: string;
}

export interface CameraControllerDeps {
  bluetooth?: typeof bluetooth;
  requestDevice?: () => Promise<BleDevice>;
  connectLink?: (device: BleDevice) => Promise<BleLink>;
  storage?: Pick<Storage, "getItem" | "setItem" | "removeItem"> | null;
  setTimer?: (cb: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
  timeoutMs?: number;
}

export const BACKOFF_MS = [1000, 2000, 4000, 8000, 16000, 30000] as const;

function backoff(attempt: number): number {
  return BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)]!;
}

function defaultStorage(): Pick<Storage, "getItem" | "setItem" | "removeItem"> | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

function randomIdentity(): Identity {
  const bytes = new Uint8Array(10);
  if (typeof crypto !== "undefined" && crypto.getRandomValues) crypto.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  const [a = 0, b = 0, c = 0, d = 0, ...mac] = bytes;
  return { deviceId: (a | (b << 8) | (c << 16) | (d << 24)) >>> 0, mac };
}

/** The prompter's one camera: pair, remember, reconnect, and expose start/stop for TakeSession. */
export class CameraController {
  private readonly deps: Required<Omit<CameraControllerDeps, "storage">> & { storage: CameraControllerDeps["storage"] };
  private listeners = new Set<(state: CameraState) => void>();
  private device: BleDevice | null = null;
  private camera: DjiCamera | null = null;
  private attempt = 0;
  private timer: unknown = null;
  private closed = false;
  state: CameraState;

  constructor(deps: CameraControllerDeps = {}) {
    this.deps = {
      bluetooth: deps.bluetooth ?? bluetooth,
      requestDevice: deps.requestDevice ?? requestDjiDevice,
      connectLink: deps.connectLink ?? connectLink,
      storage: deps.storage === undefined ? defaultStorage() : deps.storage,
      setTimer: deps.setTimer ?? ((cb, ms) => setTimeout(cb, ms)),
      clearTimer: deps.clearTimer ?? ((h) => clearTimeout(h as ReturnType<typeof setTimeout>)),
      timeoutMs: deps.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    };
    const supported = this.deps.bluetooth() !== null;
    this.state = { phase: supported ? "empty" : "unsupported", name: this.remembered()?.name ?? "", status: null, error: "" };
  }

  get supported(): boolean {
    return this.state.phase !== "unsupported";
  }

  get connected(): boolean {
    return this.state.phase === "connected" && this.camera !== null;
  }

  /** A camera was paired on this device before. */
  get paired(): boolean {
    return this.remembered()?.bleId !== undefined;
  }

  get name(): string {
    return this.state.name || "DJI camera";
  }

  subscribe(listener: (state: CameraState) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** On page load: reconnect to the remembered camera without a chooser, where the browser allows. */
  async restore(): Promise<void> {
    const remembered = this.remembered();
    const api = this.deps.bluetooth();
    if (!remembered?.bleId || !api?.getDevices) return;
    const devices = await api.getDevices().catch(() => [] as BleDevice[]);
    const device = devices.find((d) => d.id === remembered.bleId);
    if (!device) return;
    this.device = device;
    await this.open(false);
  }

  /** Pair from the prompter settings (runs the browser's chooser; call from a click). */
  async pair(): Promise<void> {
    if (!this.supported) return;
    try {
      this.device = await this.deps.requestDevice();
    } catch (error) {
      // dismissing the chooser is not an error worth showing
      if ((error as { name?: string }).name !== "NotFoundError") this.set({ phase: "error", error: String(error) });
      return;
    }
    await this.open(true);
  }

  /** Reconnect now (the error state's button), resetting the backoff. */
  async reconnect(): Promise<void> {
    this.attempt = 0;
    if (this.timer !== null) this.deps.clearTimer(this.timer);
    this.timer = null;
    if (this.device) await this.open(false);
    else await this.restore();
  }

  forget(): void {
    this.closed = true;
    this.deps.storage?.removeItem(CAMERA_KEY);
    this.camera?.disconnect();
    this.set({ phase: "empty", name: "", status: null, error: "" });
  }

  dispose(): void {
    this.closed = true;
    if (this.timer !== null) this.deps.clearTimer(this.timer);
    this.camera?.disconnect();
    this.listeners.clear();
  }

  async start(): Promise<void> {
    if (!this.camera) throw new DjiCommandError("no camera connected");
    await this.camera.startRecording();
  }

  async stop(): Promise<void> {
    if (!this.camera) throw new DjiCommandError("no camera connected");
    await this.camera.stopRecording();
  }

  async switchMode(mode: number): Promise<void> {
    if (!this.camera) throw new DjiCommandError("no camera connected");
    await this.camera.switchMode(mode);
  }

  private async open(firstPair: boolean): Promise<void> {
    const device = this.device;
    if (!device) return;
    this.closed = false;
    this.set({ phase: "connecting", error: "" });
    const saved = this.remembered();
    const identity: Remembered = saved ?? { ...randomIdentity() };
    try {
      const link = await this.deps.connectLink(device);
      const camera = new DjiCamera(link, { identity, timeoutMs: this.deps.timeoutMs });
      camera.onStatus((status) => this.set({ status }));
      camera.onDisconnect(() => this.lost(camera));
      await camera.connect(firstPair || !saved);
      await camera.subscribeStatus();
      this.camera = camera;
      this.attempt = 0;
      const name = device.name ?? "";
      this.deps.storage?.setItem(CAMERA_KEY, JSON.stringify({ ...identity, bleId: device.id, name }));
      this.set({ phase: "connected", name, error: "" });
    } catch (error) {
      this.camera = null;
      this.set({ phase: "error", error: error instanceof Error ? error.message : String(error) });
      if (!(error instanceof DjiCommandError && error.code === REJECTED)) this.schedule();
    }
  }

  private lost(camera: DjiCamera): void {
    if (this.camera !== camera) return;
    this.camera = null;
    if (this.closed) return;
    this.set({ phase: "error", status: null, error: "disconnected" });
    this.schedule();
  }

  private schedule(): void {
    if (this.closed || !this.device || this.timer !== null) return;
    const delay = backoff(this.attempt);
    this.attempt += 1;
    this.timer = this.deps.setTimer(() => {
      this.timer = null;
      void this.open(false);
    }, delay);
  }

  private remembered(): Remembered | null {
    try {
      const raw = this.deps.storage?.getItem(CAMERA_KEY);
      const parsed = raw ? (JSON.parse(raw) as Remembered) : null;
      return parsed && typeof parsed.deviceId === "number" && Array.isArray(parsed.mac) ? parsed : null;
    } catch {
      return null;
    }
  }

  private set(patch: Partial<CameraState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener(this.state);
  }
}
