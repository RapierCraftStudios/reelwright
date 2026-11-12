// SPDX-License-Identifier: AGPL-3.0-or-later
// Camera panel on the prompter (#50): pairing, connection state and the camera's own status
// (recording, battery, storage, temperature). A status pill on phones, a side panel on desktop.
import type { CameraState, CameraStatus, TempWarning } from "./djiCommands";

export const NO_CAMERA_MESSAGE = "No camera paired";
export const PAIR_LABEL = "Pair DJI camera";
export const CONNECTING_MESSAGE = "Connecting…";
export const DISCONNECTED_MESSAGE = "Camera disconnected — recording may still be running on the camera";
export const UNSUPPORTED_MESSAGE = "Camera control needs Chrome on Android";

const TEMP_MESSAGES: Record<Exclude<TempWarning, 0>, string> = {
  1: "Camera is getting warm",
  2: "Camera too hot to record",
  3: "Camera overheating — let it cool",
};

export function clock(totalS: number): string {
  const s = Math.max(0, Math.floor(totalS));
  const hh = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return hh > 0 ? `${hh}:${mm}:${ss}` : `${mm}:${ss}`;
}

function storageText(status: CameraStatus): string {
  const gb = (status.remainCapacityMb / 1024).toFixed(1);
  const minutes = Math.floor(status.remainTimeS / 60);
  const time = minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : `${minutes} min`;
  return `${time} left (${gb} GB)`;
}

export function recordingText(status: CameraStatus | null): string {
  if (!status) return "Not recording";
  return status.recording ? `Recording ${clock(status.recordTimeS)}` : "Not recording";
}

/** One line for the pill and the live region. */
export function summary(state: CameraState): string {
  switch (state.phase) {
    case "unsupported":
      return UNSUPPORTED_MESSAGE;
    case "empty":
      return NO_CAMERA_MESSAGE;
    case "connecting":
      return CONNECTING_MESSAGE;
    case "error":
      return DISCONNECTED_MESSAGE;
    case "connected":
      return recordingText(state.status);
  }
}

export interface CameraPanelProps {
  state: CameraState;
  /** Show pairing and the continuous toggle (the prompter settings are open). */
  setup: boolean;
  continuous: boolean;
  onContinuousChange(value: boolean): void;
  onPair(): void;
  onReconnect(): void;
}

export function CameraPanel({ state, setup, continuous, onContinuousChange, onPair, onReconnect }: CameraPanelProps) {
  const { phase, status } = state;
  const recording = phase === "connected" && status?.recording === true;
  const temp = status && status.tempWarning > 0 ? TEMP_MESSAGES[status.tempWarning as 1 | 2 | 3] : "";
  return (
    <section
      className="camera-panel"
      aria-label="Camera"
      data-phase={phase}
      data-recording={recording}
      data-setup={setup}
      data-testid="camera-panel"
    >
      <p className="camera-pill" role="status" aria-live="polite" data-testid="camera-status">
        {phase === "connecting" && <span className="camera-spinner" aria-hidden="true" />}
        {phase === "connected" && <span className="camera-dot" aria-hidden="true" />}
        <span>{summary(state)}</span>
        {temp && <span className="camera-temp">{temp}</span>}
      </p>

      {phase === "connected" && status && (
        <dl className="camera-details">
          <div>
            <dt>Camera</dt>
            <dd>{state.name || "DJI camera"}</dd>
          </div>
          <div>
            <dt>Battery</dt>
            <dd>{status.batteryPct}%</dd>
          </div>
          <div>
            <dt>Storage</dt>
            <dd>{storageText(status)}</dd>
          </div>
        </dl>
      )}

      {phase === "error" && (
        <button type="button" className="camera-button" onClick={onReconnect}>
          Reconnect
        </button>
      )}

      {setup && phase === "empty" && (
        <button type="button" className="camera-button" onClick={onPair}>
          {PAIR_LABEL}
        </button>
      )}

      {setup && phase !== "unsupported" && (
        <label className="prompter-field prompter-check camera-continuous">
          <input type="checkbox" checked={continuous} onChange={(e) => onContinuousChange(e.target.checked)} />
          <span>Continuous recording (one file, beats marked)</span>
        </label>
      )}
    </section>
  );
}
