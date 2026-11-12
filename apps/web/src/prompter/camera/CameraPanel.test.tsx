// SPDX-License-Identifier: AGPL-3.0-or-later
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CONNECTING_MESSAGE,
  CameraPanel,
  type CameraPanelProps,
  DISCONNECTED_MESSAGE,
  NO_CAMERA_MESSAGE,
  UNSUPPORTED_MESSAGE,
  clock,
} from "./CameraPanel";
import type { CameraState, CameraStatus } from "./djiCommands";

afterEach(cleanup);

const STATUS: CameraStatus = {
  mode: 1,
  recording: false,
  recordTimeS: 0,
  batteryPct: 81,
  remainCapacityMb: 61440,
  remainTimeS: 3900,
  tempWarning: 0,
};

function renderPanel(state: Partial<CameraState>, props: Partial<CameraPanelProps> = {}) {
  const handlers = { onPair: vi.fn(), onReconnect: vi.fn(), onContinuousChange: vi.fn() };
  render(
    <CameraPanel
      state={{ phase: "empty", name: "", status: null, error: "", ...state }}
      setup={false}
      continuous={false}
      {...handlers}
      {...props}
    />,
  );
  return handlers;
}

describe("CameraPanel", () => {
  it("empty: says no camera is paired and offers pairing in the settings", () => {
    const { onPair } = renderPanel({ phase: "empty" }, { setup: true });
    expect(screen.getByRole("status").textContent).toContain(NO_CAMERA_MESSAGE);
    fireEvent.click(screen.getByRole("button", { name: "Pair DJI camera" }));
    expect(onPair).toHaveBeenCalledOnce();
  });

  it("empty outside the settings: only the status pill", () => {
    renderPanel({ phase: "empty" });
    expect(screen.getByRole("status").textContent).toContain(NO_CAMERA_MESSAGE);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("connecting: shows a spinner and 'Connecting…'", () => {
    renderPanel({ phase: "connecting" });
    const status = screen.getByRole("status");
    expect(status.textContent).toContain(CONNECTING_MESSAGE);
    expect(status.querySelector(".camera-spinner")).not.toBeNull();
  });

  it("error: warns that the camera may still be recording and offers Reconnect", () => {
    const { onReconnect } = renderPanel({ phase: "error", error: "disconnected" });
    expect(screen.getByRole("status").textContent).toContain(DISCONNECTED_MESSAGE);
    fireEvent.click(screen.getByRole("button", { name: "Reconnect" }));
    expect(onReconnect).toHaveBeenCalledOnce();
  });

  it("connected and recording: the state is text plus a dot, with battery and storage", () => {
    renderPanel({ phase: "connected", name: "OsmoAction5Pro", status: { ...STATUS, recording: true, recordTimeS: 42 } });
    const panel = screen.getByTestId("camera-panel");
    expect(screen.getByRole("status").textContent).toContain("Recording 00:42");
    expect(panel.getAttribute("data-recording")).toBe("true");
    expect(panel.querySelector(".camera-dot")).not.toBeNull();
    expect(panel.textContent).toContain("81%");
    expect(panel.textContent).toContain("1 h 5 min left (60.0 GB)");
    expect(panel.textContent).toContain("OsmoAction5Pro");
  });

  it("connected and idle: says 'Not recording'", () => {
    renderPanel({ phase: "connected", status: STATUS });
    expect(screen.getByRole("status").textContent).toContain("Not recording");
    expect(screen.getByTestId("camera-panel").getAttribute("data-recording")).toBe("false");
  });

  it("shows the temperature warning as text", () => {
    renderPanel({ phase: "connected", status: { ...STATUS, tempWarning: 2 } });
    expect(screen.getByRole("status").textContent).toContain("Camera too hot to record");
  });

  it("unsupported browser: explains why and offers no pair button", () => {
    renderPanel({ phase: "unsupported" }, { setup: true });
    expect(screen.getByRole("status").textContent).toContain(UNSUPPORTED_MESSAGE);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("toggles continuous recording from the settings", () => {
    const { onContinuousChange } = renderPanel({ phase: "connected", status: STATUS }, { setup: true });
    fireEvent.click(screen.getByRole("checkbox", { name: /Continuous recording/ }));
    expect(onContinuousChange).toHaveBeenCalledWith(true);
  });

  it("formats the recording clock", () => {
    expect(clock(42)).toBe("00:42");
    expect(clock(3725)).toBe("1:02:05");
  });
});
