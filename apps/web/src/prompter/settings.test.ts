// SPDX-License-Identifier: AGPL-3.0-or-later
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_SETTINGS,
  KEYMAP,
  LIMITS,
  SETTINGS_KEY,
  actionForKey,
  loadSettings,
  normalizeSettings,
  saveSettings,
} from "./settings";

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("prompter settings", () => {
  it("loads the defaults when nothing is stored", () => {
    expect(loadSettings()).toEqual(DEFAULT_SETTINGS);
    expect(DEFAULT_SETTINGS.mirror).toBe(false);
    expect(DEFAULT_SETTINGS.lensMode).toBe("beam-splitter");
  });

  it("clamps numbers into their limits and rejects bad values", () => {
    const s = normalizeSettings({ fontSize: 9999, speed: -5, margin: Number.NaN, mirror: "yes", lensMode: "sideways" });
    expect(s.fontSize).toBe(LIMITS.fontSize.max);
    expect(s.speed).toBe(LIMITS.speed.min);
    expect(s.margin).toBe(DEFAULT_SETTINGS.margin);
    expect(s.mirror).toBe(DEFAULT_SETTINGS.mirror);
    expect(s.lensMode).toBe(DEFAULT_SETTINGS.lensMode);
  });

  it("round-trips through localStorage per device", () => {
    const custom = { fontSize: 72, speed: 120, margin: 12, mirror: true, lensMode: "above-lens", dyslexiaFont: true, cameraContinuous: true } as const;
    saveSettings(custom);
    expect(JSON.parse(localStorage.getItem(SETTINGS_KEY) ?? "null")).toEqual(custom);
    expect(loadSettings()).toEqual(custom);
  });

  it("falls back to defaults on corrupt JSON or disabled storage", () => {
    localStorage.setItem(SETTINGS_KEY, "{not json");
    expect(loadSettings()).toEqual(DEFAULT_SETTINGS);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(() => saveSettings(DEFAULT_SETTINGS)).not.toThrow();
  });

  it("maps keyboard and pedal keys to start/pause, back and retake", () => {
    for (const key of [" ", "PageDown", "ArrowRight", "ArrowDown"]) expect(KEYMAP[key]).toBe("toggle");
    for (const key of ["ArrowUp", "PageUp", "ArrowLeft"]) expect(KEYMAP[key]).toBe("back");
    for (const key of ["r", "R", "Enter"]) expect(KEYMAP[key]).toBe("retake");
    expect(actionForKey({ key: "r", ctrlKey: true, metaKey: false, altKey: false })).toBeNull();
    expect(actionForKey({ key: "x", ctrlKey: false, metaKey: false, altKey: false })).toBeNull();
  });

  it("defaults cameraContinuous to off and keeps older saved settings loading", () => {
    expect(DEFAULT_SETTINGS.cameraContinuous).toBe(false);
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({ fontSize: 60, mirror: true }));
    const loaded = loadSettings();
    expect(loaded).toMatchObject({ fontSize: 60, mirror: true, cameraContinuous: false });
    expect(normalizeSettings({ cameraContinuous: true }).cameraContinuous).toBe(true);
    expect(normalizeSettings({ cameraContinuous: "yes" }).cameraContinuous).toBe(false);
  });

  it("maps s, S and Escape to stop for pedals and keyboards", () => {
    for (const key of ["s", "S", "Escape"]) expect(KEYMAP[key]).toBe("stop");
    expect(actionForKey({ key: "Escape", ctrlKey: false, metaKey: false, altKey: false })).toBe("stop");
    expect(actionForKey({ key: "s", ctrlKey: true, metaKey: false, altKey: false })).toBeNull();
  });
});
