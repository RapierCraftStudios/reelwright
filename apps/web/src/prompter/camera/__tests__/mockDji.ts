// SPDX-License-Identifier: AGPL-3.0-or-later
// Test-only mock DJI camera behind a BleLink: decodes what the prompter writes and answers like an
// Osmo Action (connection handshake, record control, mode switch, status pushes).
import { type BleLink, type DjiFrame, decodeFrame, encodeFrame } from "../djiBle";

export const CAMERA_DEVICE_ID = 0x0a0b0c0d;

export interface MockStatus {
  mode?: number;
  recording?: boolean;
  recordTimeS?: number;
  remainCapacityMb?: number;
  remainTimeS?: number;
  tempOver?: number;
  batteryPct?: number;
}

export function statusPayload(s: MockStatus = {}): Uint8Array {
  const p = new Uint8Array(38);
  const v = new DataView(p.buffer);
  p[0] = s.mode ?? 0x01;
  p[1] = s.recording ? 0x03 : 0x01;
  v.setUint16(5, s.recordTimeS ?? 0, true);
  v.setUint32(15, s.remainCapacityMb ?? 0, true);
  v.setUint32(23, s.remainTimeS ?? 0, true);
  p[30] = s.tempOver ?? 0;
  p[37] = s.batteryPct ?? 100;
  return p;
}

export class MockDjiCamera implements BleLink {
  written: DjiFrame[] = [];
  recording = false;
  /** Answer the handshake with verify_data 1 (the user tapped reject on the camera). */
  reject = false;
  /** Stop answering (simulates a camera out of range). */
  silent = false;
  connected = true;
  private notify: ((bytes: Uint8Array) => void)[] = [];
  private gone: (() => void)[] = [];
  private seq = 0x100;

  async write(bytes: Uint8Array): Promise<void> {
    const frame = decodeFrame(bytes);
    this.written.push(frame);
    if (this.silent) return;
    // answer asynchronously, like a notification arriving later
    queueMicrotask(() => this.answer(frame));
  }

  onNotify(cb: (bytes: Uint8Array) => void): void {
    this.notify.push(cb);
  }

  onDisconnect(cb: () => void): void {
    this.gone.push(cb);
  }

  disconnect(): void {
    if (!this.connected) return;
    this.connected = false;
    for (const cb of this.gone) cb();
  }

  /** Commands the prompter sent, as "SS/II" hex pairs (handshake replies included). */
  commands(): string[] {
    const h = (n: number) => n.toString(16).padStart(2, "0").toUpperCase();
    return this.written.map((f) => `${h(f.cmdSet)}/${h(f.cmdId)}`);
  }

  pushStatus(status: MockStatus): void {
    this.emit({ cmdType: 0x00, seq: this.seq++, cmdSet: 0x1d, cmdId: 0x02, payload: statusPayload(status) });
  }

  /** Emit raw bytes split into two notifications, as BLE MTU limits often do. */
  private emit(fields: Parameters<typeof encodeFrame>[0]): void {
    const bytes = encodeFrame(fields);
    const cut = Math.min(7, bytes.length);
    for (const cb of this.notify) {
      cb(bytes.slice(0, cut));
      cb(bytes.slice(cut));
    }
  }

  private answer(frame: DjiFrame): void {
    if (frame.isResponse) return;
    const reply = (payload: number[]) =>
      this.emit({ cmdType: 0x20, seq: frame.seq, cmdSet: frame.cmdSet, cmdId: frame.cmdId, payload });
    if (frame.cmdSet === 0x00 && frame.cmdId === 0x19) {
      reply([0x0d, 0x0c, 0x0b, 0x0a, 0, 0, 0, 0, 0]);
      const req = new Uint8Array(33);
      new DataView(req.buffer).setUint32(0, CAMERA_DEVICE_ID, true);
      req[4] = 6;
      req[26] = 2; // verify_mode 2: verdict
      req[27] = this.reject ? 1 : 0;
      this.emit({ cmdType: 0x02, seq: this.seq++, cmdSet: 0x00, cmdId: 0x19, payload: req });
    } else if (frame.cmdSet === 0x1d && frame.cmdId === 0x03) {
      this.recording = frame.payload[4] === 0;
      reply([0]);
    } else if (frame.cmdSet === 0x1d && frame.cmdId === 0x04) {
      reply([0]);
    }
  }
}
