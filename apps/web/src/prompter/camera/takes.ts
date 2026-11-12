// SPDX-License-Identifier: AGPL-3.0-or-later
// Take sessions (#50): turns prompter events (Go, pause, stop, line changes, retake presses) into
// camera start/stop calls and one marker per take, so the rough cut (#11) can join camera files to
// script lines by start time. Markers go through the offline queue in marks.ts.
import type { TakeCreate } from "../../api";
import { submitTake } from "../marks";

/** What the take controller needs from a camera: start/stop that resolve once the camera acks. */
export interface CameraPort {
  start(): Promise<void>;
  stop(): Promise<void>;
  /** Camera name recorded on the marker (e.g. "Osmo Action 5 Pro"). */
  readonly name?: string;
}

/** The part of a prompter line a take cares about. */
export interface TakeLine {
  lineNo: number;
  beat: string;
}

export interface TakeSessionOptions {
  projectId: string;
  scriptVersion: number;
  camera: CameraPort;
  /** Keep one recording running and only mark beats (settings.cameraContinuous). */
  continuous?: boolean;
  submit?: (projectId: string, body: TakeCreate) => Promise<"sent" | "queued">;
  now?: () => Date;
  uuid?: () => string;
  /** Called with each marker as it is queued, and with camera errors. */
  onMarker?: (body: TakeCreate, result: "sent" | "queued") => void;
  onError?: (error: unknown) => void;
}

interface OpenTake {
  beat: string;
  lineFrom: number;
  lineTo: number;
  retake: boolean;
  startedAt: Date;
}

function randomId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
}

export class TakeSession {
  private readonly opts: TakeSessionOptions;
  private open: OpenTake | null = null;
  private recording = false;
  private current: TakeLine | null = null;
  private queue: Promise<void> = Promise.resolve();
  continuous: boolean;

  constructor(opts: TakeSessionOptions) {
    this.opts = opts;
    this.continuous = opts.continuous ?? false;
  }

  get isRecording(): boolean {
    return this.recording;
  }

  get openTake(): Readonly<OpenTake> | null {
    return this.open;
  }

  /** Go pressed at `line`: start recording (normal mode, or the first Go in continuous mode). */
  go(line: TakeLine): Promise<void> {
    return this.enqueue(async () => {
      this.current = line;
      if (this.open) return;
      if (!this.recording) {
        await this.opts.camera.start();
        this.recording = true;
      }
      this.openAt(this.current);
    });
  }

  /** Pause pressed: normal mode stops the camera; continuous mode only closes the marker. */
  pause(): Promise<void> {
    return this.enqueue(() => (this.continuous ? this.close(false) : this.close(true)));
  }

  /** Explicit stop: always stops the camera. */
  stop(): Promise<void> {
    return this.enqueue(() => this.close(true));
  }

  /** The prompter's active line moved. A new beat ends the take (and, normally, the recording). */
  line(line: TakeLine): Promise<void> {
    return this.enqueue(async () => {
      const take = this.open;
      if (take && line.beat !== take.beat) {
        if (this.continuous) {
          await this.close(false);
          this.current = line;
          this.openAt(line);
          return;
        }
        await this.close(true);
      } else if (take) {
        take.lineTo = Math.max(take.lineTo, line.lineNo);
      }
      this.current = line;
    });
  }

  /** A retake mark was pressed during the take. */
  retake(): void {
    if (this.open) this.open.retake = true;
  }

  /** Settle queued work (tests and unmount). */
  idle(): Promise<void> {
    return this.queue;
  }

  private enqueue(task: () => Promise<void>): Promise<void> {
    const next = this.queue.then(task).catch((error: unknown) => this.opts.onError?.(error));
    this.queue = next;
    return next;
  }

  private now(): Date {
    return this.opts.now?.() ?? new Date();
  }

  private openAt(line: TakeLine): void {
    this.open = { beat: line.beat, lineFrom: line.lineNo, lineTo: line.lineNo, retake: false, startedAt: this.now() };
  }

  private async close(stopCamera: boolean): Promise<void> {
    const take = this.open;
    this.open = null;
    const body: TakeCreate | null = take && {
      client_id: (this.opts.uuid ?? randomId)(),
      script_version: this.opts.scriptVersion,
      beat_slug: take.beat,
      line_from: take.lineFrom,
      line_to: take.lineTo,
      retake: take.retake,
      continuous: this.continuous,
      started_at: take.startedAt.toISOString(),
      stopped_at: this.now().toISOString(),
      camera: this.opts.camera.name ?? "",
    };
    try {
      if (stopCamera && this.recording) {
        this.recording = false;
        await this.opts.camera.stop();
      }
    } finally {
      // The marker is queued even when the stop command fails: the recording happened either way.
      if (body) {
        const result = await (this.opts.submit ?? submitTake)(this.opts.projectId, body);
        this.opts.onMarker?.(body, result);
      }
    }
  }
}
