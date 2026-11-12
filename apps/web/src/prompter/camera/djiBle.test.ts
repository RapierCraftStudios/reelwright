// SPDX-License-Identifier: AGPL-3.0-or-later
// Byte vectors from dji-sdk/Osmo-GPS-Controller-Demo@92fe23e: docs/protocol_data_segment.md (mode
// switch example) and test/connect_cmd_frame_builder/connect_cmd_frame.txt.
import { describe, expect, it, vi } from "vitest";

import {
  type DjiFrame,
  DjiFrameError,
  FrameReader,
  crc16,
  crc32,
  decodeFrame,
  encodeFrame,
  isBluetoothSupported,
  requestDjiDevice,
} from "./djiBle";

const hex = (s: string) => Uint8Array.from(s.replace(/\s+/g, "").match(/../g)!.map((h) => parseInt(h, 16)));
const toHex = (b: Uint8Array) =>
  Array.from(b, (x) => x.toString(16).padStart(2, "0"))
    .join("")
    .toUpperCase();

const MODE_SWITCH = "AA 1B 00 01 00 00 00 00 05 00 57 EE 1D 04 00 00 33 FF 0A 01 47 39 36 F4 FA E1 D0";
const CONNECT_PREFIX = "AA330002000000000100982F00197856341206383456789ABC00000000000000000000000000000000";
const CONNECT_FRAMES = [
  `${CONNECT_PREFIX}FE1F00000000796C667C`,
  `${CONNECT_PREFIX}191A0000000033C8C7BF`,
  `${CONNECT_PREFIX}8F1200000000AC768844`,
];

describe("DJI frame codec", () => {
  it("encodes the Hyperlapse mode-switch frame byte for byte and decodes it back", () => {
    const payload = hex("00 00 33 FF 0A 01 47 39 36");
    const frame = encodeFrame({ cmdType: 0x01, seq: 5, cmdSet: 0x1d, cmdId: 0x04, payload });
    expect(toHex(frame)).toBe(MODE_SWITCH.replace(/ /g, ""));
    const decoded = decodeFrame(frame);
    expect(decoded).toMatchObject({ cmdType: 0x01, isResponse: false, seq: 5, cmdSet: 0x1d, cmdId: 0x04 });
    expect(toHex(decoded.payload)).toBe("000033FF0A01473936");
  });

  it("decodes DJI's connection request frames and re-encodes them identically", () => {
    for (const vector of CONNECT_FRAMES) {
      const decoded = decodeFrame(hex(vector));
      expect(decoded).toMatchObject({ cmdType: 0x02, seq: 1, cmdSet: 0x00, cmdId: 0x19, isResponse: false });
      expect(decoded.payload).toHaveLength(33);
      expect(toHex(encodeFrame(decoded))).toBe(vector);
    }
  });

  it("uses CRC16 and CRC32 seeded with 0x3AA3", () => {
    const header = hex(MODE_SWITCH).subarray(0, 10);
    expect(crc16(header)).toBe(0xee57);
    const body = hex(MODE_SWITCH);
    expect(crc32(body.subarray(0, body.length - 4))).toBe(0xd0e1faf4);
  });

  it("flags the response bit", () => {
    const frame = decodeFrame(encodeFrame({ cmdType: 0x20, seq: 9, cmdSet: 0x1d, cmdId: 0x03, payload: [0] }));
    expect(frame.isResponse).toBe(true);
  });

  it("rejects a bad SOF, length, CRC16 or CRC32 with a typed error", () => {
    const good = hex(MODE_SWITCH);
    const reason = (bytes: Uint8Array) => {
      try {
        decodeFrame(bytes);
      } catch (error) {
        expect(error).toBeInstanceOf(DjiFrameError);
        return (error as DjiFrameError).reason;
      }
      return "ok";
    };
    const sof = good.slice();
    sof[0] = 0xab;
    expect(reason(sof)).toBe("sof");
    expect(reason(good.slice(0, good.length - 1))).toBe("length");
    const header = good.slice();
    header[8] = 0x06;
    expect(reason(header)).toBe("crc16");
    const body = good.slice();
    body[16] = 0x34;
    expect(reason(body)).toBe("crc32");
  });
});

describe("FrameReader", () => {
  it("reassembles a frame split across chunks and splits two frames in one chunk", () => {
    const frames: DjiFrame[] = [];
    const reader = new FrameReader((f) => frames.push(f));
    const a = hex(MODE_SWITCH);
    const b = hex(CONNECT_FRAMES[1]!);
    reader.push(a.subarray(0, 2));
    reader.push(a.subarray(2, 15));
    expect(frames).toHaveLength(0);
    reader.push(a.subarray(15));
    expect(frames.map((f) => f.cmdId)).toEqual([0x04]);

    const both = new Uint8Array([0x00, 0x13, ...b, ...a]);
    reader.push(both);
    expect(frames.map((f) => [f.cmdSet, f.cmdId])).toEqual([
      [0x1d, 0x04],
      [0x00, 0x19],
      [0x1d, 0x04],
    ]);
  });

  it("resyncs past a corrupt frame", () => {
    const frames: DjiFrame[] = [];
    const reader = new FrameReader((f) => frames.push(f));
    const bad = hex(MODE_SWITCH);
    bad[20] ^= 0xff;
    reader.push(new Uint8Array([...bad, ...hex(MODE_SWITCH)]));
    expect(frames).toHaveLength(1);
  });
});

describe("Web Bluetooth feature detection", () => {
  it("reports no support without navigator.bluetooth (iOS Safari, Firefox)", async () => {
    expect(isBluetoothSupported()).toBe(false);
    await expect(requestDjiDevice()).rejects.toThrow(/not available/);
  });

  it("asks the chooser for the DJI service 0xFFF0", async () => {
    const requestDevice = vi.fn().mockResolvedValue({ id: "cam" });
    vi.stubGlobal("navigator", { ...navigator, bluetooth: { requestDevice } });
    try {
      expect(isBluetoothSupported()).toBe(true);
      await requestDjiDevice();
      expect(requestDevice).toHaveBeenCalledWith({ filters: [{ services: [0xfff0] }], optionalServices: [0xfff0] });
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
