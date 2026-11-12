// SPDX-License-Identifier: AGPL-3.0-or-later
// /prompter/:id — the phone teleprompter (spike § Route/API map, § Runtime topology). Reads the
// locked script with cues hidden, scrolls it, and posts retake marks for the best-take cut.
import { type ChangeEvent, useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, type Script, type TakeMarkCreate, getScript } from "../api";
import { CameraPanel } from "../prompter/camera/CameraPanel";
import { CameraController, type CameraState } from "../prompter/camera/djiCommands";
import { TakeSession } from "../prompter/camera/takes";
import { flushPending, pendingCount, submitMark } from "../prompter/marks";
import { type PrompterLine, Scroller, type ScrollerHandle } from "../prompter/Scroller";
import {
  LIMITS,
  type LensMode,
  type PrompterAction,
  type PrompterSettings,
  actionForKey,
  loadSettings,
  normalizeSettings,
  saveSettings,
} from "../prompter/settings";

export const PROMPTER_EMPTY_MESSAGE = "No locked script — lock one in the editor";
export const LOST_MESSAGE = "Lost connection — keep reading, resyncing";
export const RESYNC_MS = 5000;

type Phase = "loading" | "empty" | "ready" | "unavailable";

interface CachedScript {
  version: number;
  lines: PrompterLine[];
}

const scriptKey = (id: string) => `rw.prompter.script.${id}`;

function readCache(id: string): CachedScript | null {
  try {
    const raw = localStorage.getItem(scriptKey(id));
    return raw ? (JSON.parse(raw) as CachedScript) : null;
  } catch {
    return null;
  }
}

function writeCache(id: string, cached: CachedScript) {
  try {
    localStorage.setItem(scriptKey(id), JSON.stringify(cached));
  } catch {
    // storage full or disabled: offline reading just has no copy
  }
}

/** Only what is read aloud: spoken lines (the parser already stripped their cue tags). */
export function spokenLines(script: Script): PrompterLine[] {
  return script.beats.flatMap((beat) =>
    beat.lines
      .filter((line) => line.kind === "spoken" && line.text.trim() !== "")
      .map((line, i) => ({ lineNo: line.line_no, beat: beat.slug, text: line.text, beatStart: i === 0 })),
  );
}

const REDUCED_MOTION = "(prefers-reduced-motion: reduce)";

function useReducedMotion(): boolean {
  const supported = typeof window !== "undefined" && typeof window.matchMedia === "function";
  const [reduced, setReduced] = useState(() => supported && window.matchMedia(REDUCED_MOTION).matches);
  useEffect(() => {
    if (!supported) return;
    const media = window.matchMedia(REDUCED_MOTION);
    const onChange = () => setReduced(media.matches);
    onChange();
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [supported]);
  return reduced;
}

interface NumberSettingProps {
  label: string;
  unit: string;
  value: number;
  limits: { min: number; max: number; step: number };
  onCommit(value: number): void;
}

/** A number field that lets the reader type freely and commits only values inside the limits. */
function NumberSetting({ label, unit, value, limits, onCommit }: NumberSettingProps) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const onChange = (e: ChangeEvent<HTMLInputElement>) => {
    setDraft(e.target.value);
    const n = Number(e.target.value);
    if (e.target.value !== "" && Number.isFinite(n) && n >= limits.min && n <= limits.max) onCommit(n);
  };
  return (
    <label className="prompter-field">
      <span>
        {label} ({unit})
      </span>
      <input
        type="number"
        inputMode="numeric"
        min={limits.min}
        max={limits.max}
        step={limits.step}
        value={draft}
        onChange={onChange}
        onBlur={() => setDraft(String(value))}
      />
    </label>
  );
}

export interface PrompterRouteProps {
  /** The camera to drive (tests inject one over a mock BLE device); defaults to Web Bluetooth. */
  camera?: CameraController;
}

/** One controller per page: pairing state lives across re-renders, not across projects' scripts. */
function useCamera(injected?: CameraController): [CameraController, CameraState] {
  const [camera] = useState(() => injected ?? new CameraController());
  const [state, setState] = useState<CameraState>(() => camera.state);
  useEffect(() => {
    const unsubscribe = camera.subscribe(setState);
    setState(camera.state);
    // Reconnect to a remembered camera without a chooser; nothing happens when none was paired.
    if (camera.supported && camera.paired && camera.state.phase === "empty") void camera.restore();
    return unsubscribe;
  }, [camera]);
  useEffect(() => () => (injected ? undefined : camera.dispose()), [camera, injected]);
  return [camera, state];
}

export function PrompterRoute({ camera: injectedCamera }: PrompterRouteProps = {}) {
  const { id = "" } = useParams();
  const [phase, setPhase] = useState<Phase>("loading");
  const [doc, setDoc] = useState<CachedScript | null>(null);
  const [lost, setLost] = useState(false);
  const [pending, setPending] = useState(() => pendingCount(id));
  const [settings, setSettings] = useState<PrompterSettings>(loadSettings);
  const [playing, setPlaying] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [notice, setNotice] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const reducedMotion = useReducedMotion();
  const scroller = useRef<ScrollerHandle>(null);
  const startedAt = useRef<number | null>(null);
  const [camera, cameraState] = useCamera(injectedCamera);
  const cameraOn = cameraState.phase === "connected";
  const session = useRef<{ key: string; take: TakeSession } | null>(null);

  const load = useCallback(async () => {
    try {
      const fetched = await getScript(id);
      setLost(false);
      if (!fetched.locked) {
        setDoc(null);
        setPhase("empty");
        return;
      }
      const next: CachedScript = { version: fetched.version.version, lines: spokenLines(fetched.script) };
      writeCache(id, next);
      setDoc((prev) => (prev && prev.version === next.version ? prev : next));
      setPhase("ready");
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        setLost(false);
        setDoc(null);
        setPhase("empty");
        return;
      }
      const cached = readCache(id);
      setLost(true);
      setDoc((prev) => prev ?? cached);
      setPhase((prev) => (prev === "ready" || cached ? "ready" : "unavailable"));
    }
  }, [id]);

  const resync = useCallback(async () => {
    await Promise.all([load(), flushPending()]);
    setPending(pendingCount(id));
  }, [id, load]);

  useEffect(() => {
    void resync();
  }, [resync]);

  // Keep retrying while anything is out of sync, and immediately when the browser says it is back online.
  const outOfSync = lost || pending > 0;
  useEffect(() => {
    const onOnline = () => void resync();
    window.addEventListener("online", onOnline);
    const timer = outOfSync ? window.setInterval(onOnline, RESYNC_MS) : undefined;
    return () => {
      window.removeEventListener("online", onOnline);
      window.clearInterval(timer);
    };
  }, [outOfSync, resync]);

  useEffect(() => saveSettings(settings), [settings]);

  useEffect(() => {
    setActiveIndex(0);
    setPlaying(false);
  }, [doc?.version]);

  const update = (patch: Partial<PrompterSettings>) => setSettings((s) => normalizeSettings({ ...s, ...patch }));

  const lines = doc?.lines ?? [];
  const canRead = phase === "ready" && lines.length > 0;
  const activeLine = lines.length ? lines[Math.min(activeIndex, lines.length - 1)] : undefined;

  /** The take session for this script, only while a camera is connected (no camera calls otherwise). */
  const takes = useCallback((): TakeSession | null => {
    if (!cameraOn || !doc) return null;
    const key = `${id}:${doc.version}`;
    if (session.current?.key !== key) {
      const take = new TakeSession({
        projectId: id,
        scriptVersion: doc.version,
        camera,
        continuous: settings.cameraContinuous,
        onMarker: (body, result) => {
          setPending(pendingCount(id));
          if (result === "queued") setNotice(`Take ${body.beat_slug} saved here, will sync`);
        },
        onError: (error) => setNotice(`Camera: ${error instanceof Error ? error.message : String(error)}`),
      });
      session.current = { key, take };
    }
    session.current.take.continuous = settings.cameraContinuous;
    return session.current.take;
  }, [camera, cameraOn, doc, id, settings.cameraContinuous]);

  // Feed line changes to the take: a new beat ends it (and, normally, the recording).
  useEffect(() => {
    if (activeLine && session.current) void session.current.take.line(activeLine);
  }, [activeLine]);

  const markRetake = useCallback(async () => {
    if (!doc || doc.lines.length === 0) return;
    const line = doc.lines[Math.min(activeIndex, doc.lines.length - 1)];
    const now = Date.now();
    const body: TakeMarkCreate = {
      kind: "retake",
      script_version: doc.version,
      beat_slug: line.beat,
      line_no: line.lineNo,
      marked_at: new Date(now).toISOString(),
      offset_ms: startedAt.current === null ? 0 : Math.max(0, now - startedAt.current),
    };
    session.current?.take.retake();
    setNotice(`Retake marked at line ${line.lineNo}`);
    const result = await submitMark(id, body);
    setPending(pendingCount(id));
    if (result === "queued") setNotice(`Retake marked at line ${line.lineNo} — saved here, will sync`);
  }, [activeIndex, doc, id]);

  const run = useCallback(
    (action: PrompterAction) => {
      if (!canRead) return;
      if (action === "toggle") {
        if (!playing && startedAt.current === null) startedAt.current = Date.now();
        const take = takes();
        if (take && activeLine) void (playing ? take.pause() : take.go(activeLine));
        setPlaying(!playing);
      } else if (action === "stop") {
        setPlaying(false);
        void takes()?.stop();
      } else if (action === "back") {
        scroller.current?.backOneLine();
      } else {
        void markRetake();
      }
    },
    [activeLine, canRead, markRetake, playing, takes],
  );

  const runRef = useRef(run);
  runRef.current = run;
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest?.("input, select, textarea, [contenteditable='true']")) return;
      const action = actionForKey(event);
      if (!action) return;
      event.preventDefault(); // no page scroll, no activating a focused button twice
      if (event.repeat) return;
      runRef.current(action);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <main className="prompter-page" data-phase={phase}>
      <header className="prompter-controls" aria-label="Prompter controls">
        <Link to={`/projects/${id}/script`} className="prompter-back-link">
          ← Script
        </Link>
        <button type="button" onClick={() => run("toggle")} disabled={!canRead}>
          {playing ? "Pause" : "Start"}
        </button>
        {cameraOn && (
          <button type="button" onClick={() => run("stop")} disabled={!canRead}>
            Stop
          </button>
        )}
        <button type="button" onClick={() => run("back")} disabled={!canRead}>
          Back one line
        </button>
        <button type="button" onClick={() => run("retake")} disabled={!canRead}>
          Mark retake
        </button>
        <button
          type="button"
          aria-expanded={settingsOpen}
          aria-controls="prompter-settings"
          onClick={() => setSettingsOpen((o) => !o)}
        >
          Settings
        </button>
        <p className="prompter-notice" role="status" data-testid="prompter-status">
          {notice}
        </p>
      </header>

      <CameraPanel
        state={cameraState}
        setup={settingsOpen}
        continuous={settings.cameraContinuous}
        onContinuousChange={(cameraContinuous) => update({ cameraContinuous })}
        onPair={() => void camera.pair()}
        onReconnect={() => void camera.reconnect()}
      />

      {outOfSync && phase !== "loading" && (
        <div className="banner banner-error prompter-banner" role="alert">
          <span aria-hidden="true">⚠</span>
          <span>{LOST_MESSAGE}</span>
        </div>
      )}

      {settingsOpen && (
        <section id="prompter-settings" className="prompter-settings" aria-label="Prompter settings">
          <NumberSetting
            label="Font size"
            unit="px"
            value={settings.fontSize}
            limits={LIMITS.fontSize}
            onCommit={(fontSize) => update({ fontSize })}
          />
          <NumberSetting
            label="Speed"
            unit="px/s"
            value={settings.speed}
            limits={LIMITS.speed}
            onCommit={(speed) => update({ speed })}
          />
          <NumberSetting
            label="Margin"
            unit="%"
            value={settings.margin}
            limits={LIMITS.margin}
            onCommit={(margin) => update({ margin })}
          />
          <label className="prompter-field prompter-check">
            <input type="checkbox" checked={settings.mirror} onChange={(e) => update({ mirror: e.target.checked })} />
            <span>Mirror for beam splitter</span>
          </label>
          <label className="prompter-field">
            <span>Lens mode</span>
            <select
              value={settings.lensMode}
              onChange={(e) => update({ lensMode: e.target.value as LensMode })}
            >
              <option value="beam-splitter">Beam splitter (glass in front of lens)</option>
              <option value="above-lens">Phone above lens</option>
            </select>
          </label>
          <label className="prompter-field prompter-check">
            <input
              type="checkbox"
              checked={settings.dyslexiaFont}
              onChange={(e) => update({ dyslexiaFont: e.target.checked })}
            />
            <span>Dyslexia-friendly font</span>
          </label>
        </section>
      )}

      {phase === "loading" && (
        <div className="prompter-loading" role="status" aria-busy="true" data-testid="prompter-loading">
          <div className="prompter-spinner" aria-hidden="true" />
          <span className="prompter-sr-only">Loading script…</span>
        </div>
      )}

      {phase === "empty" && (
        <section className="prompter-empty" aria-labelledby="prompter-empty-title">
          <p id="prompter-empty-title">{PROMPTER_EMPTY_MESSAGE}</p>
          <Link to={`/projects/${id}/script`}>Open the script editor</Link>
        </section>
      )}

      {phase === "unavailable" && (
        <section className="prompter-empty">
          <p>No copy of this script on this device yet.</p>
        </section>
      )}

      {phase === "ready" && (
        <Scroller
          ref={scroller}
          lines={lines}
          playing={playing}
          settings={settings}
          reducedMotion={reducedMotion}
          activeIndex={activeIndex}
          onActiveIndexChange={setActiveIndex}
          onEnd={() => {
            setPlaying(false);
            void session.current?.take.pause();
          }}
          onTap={() => run("toggle")}
        />
      )}
    </main>
  );
}
