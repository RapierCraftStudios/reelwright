// SPDX-License-Identifier: AGPL-3.0-or-later
// DJI R SDK frame codec and Web Bluetooth transport for the Osmo Action camera (#50).
//
// Frame layout (little-endian), from DJI's Osmo GPS Controller demo:
//   SOF 0xAA | Ver/Length u16 (bits 9:0 = total frame length) | CmdType | ENC | RES[3] | SEQ u16
//   | CRC16 over bytes 0..9 | CmdSet | CmdID | payload | CRC32 over SOF..payload
// CmdType bit 5 marks a response frame. Both CRCs are reflected, seeded with 0x3AA3, no final xor.
// Web Bluetooth only exists in Chromium (Chrome on Android for the phone prompter); iOS Safari has
// none, so camera control is simply unavailable there.

export const SOF = 0xaa;
const HEADER_LEN = 12; // SOF..CRC16
const OVERHEAD = HEADER_LEN + 2 + 4; // + CmdSet/CmdID + CRC32
const MAX_LEN = 0x3ff;

export const DJI_SERVICE = 0xfff0;
export const DJI_NOTIFY_CHAR = 0xfff4;
export const DJI_WRITE_CHAR = 0xfff5;

function table(poly: number): Uint32Array {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? (c >>> 1) ^ poly : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
}

const CRC16_TABLE = table(0xa001);
const CRC32_TABLE = table(0xedb88320);

export function crc16(bytes: Uint8Array, init = 0x3aa3): number {
  let crc = init & 0xffff;
  for (const b of bytes) crc = (crc >>> 8) ^ CRC16_TABLE[(crc ^ b) & 0xff]!;
  return crc & 0xffff;
}

export function crc32(bytes: Uint8Array, init = 0x00003aa3): number {
  let crc = init >>> 0;
  for (const b of bytes) crc = (crc >>> 8) ^ CRC32_TABLE[(crc ^ b) & 0xff]!;
  return crc >>> 0;
}

export interface FrameFields {
  cmdType: number;
  seq: number;
  cmdSet: number;
  cmdId: number;
  payload?: Uint8Array | readonly number[];
  enc?: number;
}

export interface DjiFrame {
  cmdType: number;
  /** CmdType bit 5: this frame answers an earlier command. */
  isResponse: boolean;
  seq: number;
  cmdSet: number;
  cmdId: number;
  payload: Uint8Array;
}

export type DjiFrameErrorReason = "sof" | "length" | "crc16" | "crc32";

export class DjiFrameError extends Error {
  readonly reason: DjiFrameErrorReason;

  constructor(reason: DjiFrameErrorReason, message: string) {
    super(message);
    this.reason = reason;
    this.name = "DjiFrameError";
  }
}

export const RESPONSE_BIT = 0x20;

export function encodeFrame({ cmdType, seq, cmdSet, cmdId, payload = [], enc = 0 }: FrameFields): Uint8Array {
  const data = payload instanceof Uint8Array ? payload : Uint8Array.from(payload);
  const length = OVERHEAD + data.length;
  if (length > MAX_LEN) throw new DjiFrameError("length", `frame of ${length} bytes is over ${MAX_LEN}`);
  const frame = new Uint8Array(length);
  const view = new DataView(frame.buffer);
  frame[0] = SOF;
  view.setUint16(1, length & MAX_LEN, true);
  frame[3] = cmdType & 0xff;
  frame[4] = enc & 0xff;
  view.setUint16(8, seq & 0xffff, true);
  view.setUint16(10, crc16(frame.subarray(0, 10)), true);
  frame[12] = cmdSet & 0xff;
  frame[13] = cmdId & 0xff;
  frame.set(data, 14);
  view.setUint32(length - 4, crc32(frame.subarray(0, length - 4)), true);
  return frame;
}

/** Total length declared by a frame header, or null when fewer than 3 bytes are available. */
function declaredLength(bytes: Uint8Array): number | null {
  if (bytes.length < 3) return null;
  return (bytes[1]! | (bytes[2]! << 8)) & MAX_LEN;
}

export function decodeFrame(bytes: Uint8Array): DjiFrame {
  if (bytes[0] !== SOF) throw new DjiFrameError("sof", "frame does not start with 0xAA");
  const length = declaredLength(bytes);
  if (length === null || length < OVERHEAD || length !== bytes.length) {
    throw new DjiFrameError("length", `frame length ${length ?? "?"} does not match ${bytes.length} bytes`);
  }
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (view.getUint16(10, true) !== crc16(bytes.subarray(0, 10))) throw new DjiFrameError("crc16", "header CRC16 mismatch");
  if (view.getUint32(length - 4, true) !== crc32(bytes.subarray(0, length - 4))) {
    throw new DjiFrameError("crc32", "frame CRC32 mismatch");
  }
  const cmdType = bytes[3]!;
  return {
    cmdType,
    isResponse: (cmdType & RESPONSE_BIT) !== 0,
    seq: view.getUint16(8, true),
    cmdSet: bytes[12]!,
    cmdId: bytes[13]!,
    payload: bytes.slice(14, length - 4),
  };
}

/** Reassembles frames from BLE notification chunks (a frame may span chunks, or a chunk hold two). */
export class FrameReader {
  private buffer = new Uint8Array(0);
  private readonly onFrame: (frame: DjiFrame) => void;

  constructor(onFrame: (frame: DjiFrame) => void) {
    this.onFrame = onFrame;
  }

  push(chunk: Uint8Array): void {
    const next = new Uint8Array(this.buffer.length + chunk.length);
    next.set(this.buffer);
    next.set(chunk, this.buffer.length);
    this.buffer = next;
    for (;;) {
      const start = this.buffer.indexOf(SOF);
      if (start < 0) {
        this.buffer = new Uint8Array(0);
        return;
      }
      if (start > 0) this.buffer = this.buffer.slice(start);
      const length = declaredLength(this.buffer);
      if (length === null) return;
      if (length < OVERHEAD) {
        this.buffer = this.buffer.slice(1); // not a real header: resync on the next 0xAA
        continue;
      }
      if (this.buffer.length < length) return;
      const candidate = this.buffer.slice(0, length);
      let frame: DjiFrame;
      try {
        frame = decodeFrame(candidate);
      } catch {
        this.buffer = this.buffer.slice(1);
        continue;
      }
      this.buffer = this.buffer.slice(length);
      this.onFrame(frame);
    }
  }
}

// --- Web Bluetooth transport -------------------------------------------------------------------
// Minimal local declarations for the members used, so no @types/web-bluetooth dependency.

export interface BleCharacteristic extends EventTarget {
  value?: DataView | null;
  startNotifications(): Promise<BleCharacteristic>;
  stopNotifications(): Promise<BleCharacteristic>;
  writeValueWithResponse(value: BufferSource): Promise<void>;
}

export interface BleService {
  getCharacteristic(uuid: number | string): Promise<BleCharacteristic>;
}

export interface BleGattServer {
  connected: boolean;
  connect(): Promise<BleGattServer>;
  disconnect(): void;
  getPrimaryService(uuid: number | string): Promise<BleService>;
}

export interface BleDevice extends EventTarget {
  id: string;
  name?: string | null;
  gatt?: BleGattServer | null;
}

export interface BleRequestOptions {
  filters: { services?: (number | string)[]; namePrefix?: string }[];
  optionalServices?: (number | string)[];
}

export interface BluetoothApi {
  requestDevice(options: BleRequestOptions): Promise<BleDevice>;
  getDevices?(): Promise<BleDevice[]>;
}

/** A connected byte pipe to the camera. The mock device in tests implements the same interface. */
export interface BleLink {
  write(bytes: Uint8Array): Promise<void>;
  onNotify(cb: (bytes: Uint8Array) => void): void;
  onDisconnect(cb: () => void): void;
  disconnect(): void;
}

export function bluetooth(): BluetoothApi | null {
  if (typeof navigator === "undefined") return null;
  const api = (navigator as Navigator & { bluetooth?: BluetoothApi }).bluetooth;
  return api && typeof api.requestDevice === "function" ? api : null;
}

/** Web Bluetooth is available (Chrome on Android/desktop; never iOS Safari). */
export function isBluetoothSupported(): boolean {
  return bluetooth() !== null;
}

/** Open the browser's device chooser for a DJI camera. Must run inside a user gesture. */
export function requestDjiDevice(): Promise<BleDevice> {
  const api = bluetooth();
  if (!api) return Promise.reject(new Error("Web Bluetooth is not available in this browser"));
  return api.requestDevice({ filters: [{ services: [DJI_SERVICE] }], optionalServices: [DJI_SERVICE] });
}

export async function connectLink(device: BleDevice): Promise<BleLink> {
  if (!device.gatt) throw new Error("device has no GATT server");
  const server = await device.gatt.connect();
  const service = await server.getPrimaryService(DJI_SERVICE);
  const notify = await service.getCharacteristic(DJI_NOTIFY_CHAR);
  const write = await service.getCharacteristic(DJI_WRITE_CHAR);
  const notifyCbs: ((bytes: Uint8Array) => void)[] = [];
  const disconnectCbs: (() => void)[] = [];
  const onValue = () => {
    const value = notify.value;
    if (!value) return;
    const bytes = new Uint8Array(value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength));
    for (const cb of notifyCbs) cb(bytes);
  };
  const onGone = () => {
    notify.removeEventListener("characteristicvaluechanged", onValue);
    device.removeEventListener("gattserverdisconnected", onGone);
    for (const cb of disconnectCbs) cb();
  };
  notify.addEventListener("characteristicvaluechanged", onValue);
  device.addEventListener("gattserverdisconnected", onGone);
  await notify.startNotifications();
  return {
    write: (bytes) => write.writeValueWithResponse(bytes as Uint8Array<ArrayBuffer>),
    onNotify: (cb) => void notifyCbs.push(cb),
    onDisconnect: (cb) => void disconnectCbs.push(cb),
    disconnect: () => server.disconnect(),
  };
}
