// SPDX-License-Identifier: AGPL-3.0-or-later
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CAMERA_DEVICE_ID, MockDjiCamera, statusPayload } from "./__tests__/mockDji";
import type { BleDevice, BluetoothApi } from "./djiBle";
import {
  BACKOFF_MS,
  CAMERA_KEY,
  CameraController,
  type CameraState,
  DjiCamera,
  parseStatusPush,
} from "./djiCommands";

const IDENTITY = { deviceId: 0x12345678, mac: [0x38, 0x34, 0x56, 0x78, 0x9a, 0xbc] };
const hex = (b: Uint8Array) =>
  Array.from(b, (x) => x.toString(16).padStart(2, "0"))
    .join("")
    .toUpperCase();

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("DjiCamera over a mock BLE device", () => {
  it("sends DJI's exact connection request and completes the handshake", async () => {
    // verify_data comes from Math.random; pin it to DJI's first test vector (0x1FFE).
    vi.spyOn(Math, "random").mockReturnValue(0x1ffe / 0x10000);
    const link = new MockDjiCamera();
    const camera = new DjiCamera(link, { identity: IDENTITY });
    await camera.connect(false);
    const [request, reply] = link.written;
    expect(request).toMatchObject({ cmdType: 0x02, seq: 1, cmdSet: 0x00, cmdId: 0x19 });
    expect(hex(request!.payload)).toBe("7856341206383456789ABC00000000000000000000000000000000FE1F00000000");
    expect(reply).toMatchObject({ isResponse: true, cmdSet: 0x00, cmdId: 0x19 });
    expect(reply!.payload[4]).toBe(0); // ret_code 0: connection accepted
    expect(camera.cameraId).toBe(CAMERA_DEVICE_ID);
  });

  it("sets verify_mode 1 on the first pairing", async () => {
    const link = new MockDjiCamera();
    await new DjiCamera(link, { identity: IDENTITY }).connect(true);
    expect(link.written[0]!.payload[26]).toBe(1);
  });

  it("disconnects when the camera rejects pairing", async () => {
    const link = new MockDjiCamera();
    link.reject = true;
    await expect(new DjiCamera(link, { identity: IDENTITY }).connect(true)).rejects.toThrow(/rejected/);
    expect(link.connected).toBe(false);
    expect(link.written.at(-1)!.payload[4]).toBe(1);
  });

  it("starts and stops recording with 1D/03 record_ctrl 0/1 to the camera's device id", async () => {
    const link = new MockDjiCamera();
    const camera = new DjiCamera(link, { identity: IDENTITY });
    await camera.connect(false);
    await camera.startRecording();
    expect(link.recording).toBe(true);
    await camera.stopRecording();
    expect(link.recording).toBe(false);
    const records = link.written.filter((f) => f.cmdSet === 0x1d && f.cmdId === 0x03);
    expect(records.map((f) => [new DataView(f.payload.buffer).getUint32(0, true), f.payload[4]])).toEqual([
      [CAMERA_DEVICE_ID, 0],
      [CAMERA_DEVICE_ID, 1],
    ]);
    // SEQ increases with every command
    const seqs = link.written.filter((f) => !f.isResponse).map((f) => f.seq);
    expect(seqs).toEqual([...seqs].sort((a, b) => a - b));
  });

  it("switches mode with 1D/04 and subscribes to status with 1D/05 03 14", async () => {
    const link = new MockDjiCamera();
    const camera = new DjiCamera(link, { identity: IDENTITY });
    await camera.connect(false);
    await camera.switchMode(0x0a);
    await camera.subscribeStatus();
    expect(link.commands().slice(-2)).toEqual(["1D/04", "1D/05"]);
    expect(link.written.at(-2)!.payload[4]).toBe(0x0a);
    expect(Array.from(link.written.at(-1)!.payload.subarray(0, 2))).toEqual([0x03, 0x14]);
  });

  it("times out when the camera does not answer", async () => {
    const link = new MockDjiCamera();
    link.silent = true;
    const camera = new DjiCamera(link, { identity: IDENTITY, timeoutMs: 10 });
    await expect(camera.startRecording()).rejects.toThrow(/in time/);
  });

  it("parses a 1D/02 status push: battery, storage, recording and temperature", async () => {
    expect(
      parseStatusPush(
        statusPayload({
          recording: true,
          recordTimeS: 42,
          batteryPct: 73,
          remainCapacityMb: 51200,
          remainTimeS: 3900,
          tempOver: 1,
        }),
      ),
    ).toEqual({
      mode: 1,
      recording: true,
      recordTimeS: 42,
      batteryPct: 73,
      remainCapacityMb: 51200,
      remainTimeS: 3900,
      tempWarning: 1,
    });
    expect(() => parseStatusPush(new Uint8Array(10))).toThrow(/too short/);

    const link = new MockDjiCamera();
    const camera = new DjiCamera(link, { identity: IDENTITY });
    const seen: boolean[] = [];
    camera.onStatus((s) => seen.push(s.recording));
    link.pushStatus({ recording: false });
    link.pushStatus({ recording: true, tempOver: 2 });
    expect(seen).toEqual([false, true]);
  });
});

function fakeDevice(id = "cam-1"): BleDevice {
  return Object.assign(new EventTarget(), { id, name: "OsmoAction5Pro" });
}

function setup(links: MockDjiCamera[], device = fakeDevice()) {
  const timers: { cb: () => void; ms: number }[] = [];
  const api: BluetoothApi = {
    requestDevice: vi.fn().mockResolvedValue(device),
    getDevices: vi.fn().mockResolvedValue([device]),
  };
  const connectLink = vi.fn(async () => {
    const link = new MockDjiCamera();
    links.push(link);
    return link;
  });
  const controller = new CameraController({
    bluetooth: () => api,
    requestDevice: () => api.requestDevice({ filters: [] }),
    connectLink,
    setTimer: (cb, ms) => timers.push({ cb, ms }),
    clearTimer: () => undefined,
    timeoutMs: 50,
  });
  return { controller, api, connectLink, timers };
}

describe("CameraController", () => {
  it("is unsupported without Web Bluetooth and never touches the camera", async () => {
    const connectLink = vi.fn();
    const controller = new CameraController({ bluetooth: () => null, connectLink });
    expect(controller.state.phase).toBe("unsupported");
    await controller.pair();
    await controller.restore();
    expect(connectLink).not.toHaveBeenCalled();
  });

  it("pairs once, subscribes to status and remembers the camera", async () => {
    const links: MockDjiCamera[] = [];
    const { controller } = setup(links);
    const phases: CameraState["phase"][] = [];
    controller.subscribe((s) => phases.push(s.phase));
    expect(controller.state.phase).toBe("empty");
    await controller.pair();
    expect(phases).toEqual(["connecting", "connected"]);
    expect(links[0]!.commands()).toEqual(["00/19", "00/19", "1D/05"]);
    expect(links[0]!.written[0]!.payload[26]).toBe(1); // first pair: verify_mode 1
    const saved = JSON.parse(localStorage.getItem(CAMERA_KEY) ?? "null");
    expect(saved).toMatchObject({ bleId: "cam-1", name: "OsmoAction5Pro" });
    expect(controller.paired).toBe(true);

    links[0]!.pushStatus({ recording: true, batteryPct: 55 });
    expect(controller.state.status).toMatchObject({ recording: true, batteryPct: 55 });
    await controller.start();
    expect(links[0]!.recording).toBe(true);
    await controller.stop();
    expect(links[0]!.recording).toBe(false);
  });

  it("reconnects to the remembered camera on load without a chooser", async () => {
    const first: MockDjiCamera[] = [];
    const a = setup(first);
    await a.controller.pair();
    const identity = JSON.parse(localStorage.getItem(CAMERA_KEY)!).deviceId;

    const links: MockDjiCamera[] = [];
    const { controller, api } = setup(links);
    await controller.restore();
    expect(api.requestDevice).not.toHaveBeenCalled();
    expect(controller.state.phase).toBe("connected");
    expect(links[0]!.written[0]!.payload[26]).toBe(0); // known phone: verify_mode 0
    expect(new DataView(links[0]!.written[0]!.payload.buffer).getUint32(0, true)).toBe(identity);
  });

  it("reconnects automatically after a disconnect with capped backoff", async () => {
    const links: MockDjiCamera[] = [];
    const { controller, timers, connectLink } = setup(links);
    await controller.pair();
    links[0]!.disconnect();
    expect(controller.state.phase).toBe("error");
    expect(timers.map((t) => t.ms)).toEqual([1000]);

    // The camera stays out of range for a while: each attempt times out and backs off further.
    connectLink.mockImplementation(async () => {
      const link = new MockDjiCamera();
      link.silent = true;
      links.push(link);
      return link;
    });
    for (let i = 0; i < 7; i++) {
      timers.shift()!.cb();
      await vi.waitFor(() => expect(timers).toHaveLength(1));
    }
    expect(BACKOFF_MS.at(-1)).toBe(30000);
    expect(timers[0]!.ms).toBe(30000);

    connectLink.mockImplementation(async () => {
      const link = new MockDjiCamera();
      links.push(link);
      return link;
    });
    timers.shift()!.cb();
    await vi.waitFor(() => expect(controller.state.phase).toBe("connected"));
    links.at(-1)!.disconnect();
    expect(timers.map((t) => t.ms)).toEqual([1000]); // backoff resets after a good connection
  });

  it("stays in the error state without retrying when pairing is rejected", async () => {
    const links: MockDjiCamera[] = [];
    const { controller, timers, connectLink } = setup(links);
    connectLink.mockImplementation(async () => {
      const link = new MockDjiCamera();
      link.reject = true;
      links.push(link);
      return link;
    });
    await controller.pair();
    expect(controller.state.phase).toBe("error");
    expect(timers).toHaveLength(0);
    expect(localStorage.getItem(CAMERA_KEY)).toBeNull();
  });
});
