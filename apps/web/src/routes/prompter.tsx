// SPDX-License-Identifier: AGPL-3.0-or-later
// /prompter/:id — the phone teleprompter (spike § Route/API map, § Runtime topology). Reads the
// locked script with cues hidden, scrolls it, and posts retake marks for the best-take cut. Voice
// follow (#7) moves the text to the line the server hears; switching it off leaves the reader on
// that line, so Start carries on from there.
import { type ChangeEvent, useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, type Script, type TakeMarkCreate, getScript } from "../api";
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
import { VoiceFollow } from "../prompter/voiceFollow";

export const PROMPTER_EMPTY_MESSAGE = "No locked script — lock one in the editor";
export const LOST_MESSAGE = "Lost connection — keep reading, resyncing";
export const RESYNC_MS = 5000;
const LATENCY_POLL_MS = 1000;

const VOICE_ERRORS: Record<string, string> = {
  mic_denied: "Microphone blocked — back to manual scrolling",
  asr_unavailable: "Speech engine unavailable — back to manual scrolling",
  connection_lost: "Voice follow disconnected — back to manual scrolling",
};

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

export function PrompterRoute() {
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
  const [voiceOn, setVoiceOn] = useState(false);
  const [latency, setLatency] = useState<number | null>(null);
  const voice = useRef<VoiceFollow | null>(null);

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

  const stopVoice = useCallback(() => {
    voice.current?.stop();
    voice.current = null;
    setVoiceOn(false);
  }, []);

  useEffect(() => {
    setActiveIndex(0);
    setPlaying(false);
    stopVoice();
  }, [doc?.version, stopVoice]);

  useEffect(() => stopVoice, [stopVoice]);

  useEffect(() => {
    if (!voiceOn) return;
    const timer = window.setInterval(() => setLatency(voice.current?.p95Ms() ?? null), LATENCY_POLL_MS);
    return () => window.clearInterval(timer);
  }, [voiceOn]);

  const update = (patch: Partial<PrompterSettings>) => setSettings((s) => normalizeSettings({ ...s, ...patch }));

  const lines = doc?.lines ?? [];
  const canRead = phase === "ready" && lines.length > 0;

  const linesRef = useRef(lines);
  linesRef.current = lines;

  const toggleVoice = useCallback(() => {
    if (voice.current) {
      stopVoice();
      return;
    }
    if (!doc || doc.lines.length === 0) return;
    setPlaying(false);
    const follow = new VoiceFollow({
      projectId: id,
      onPosition: (lineNo) => {
        const index = linesRef.current.findIndex((line) => line.lineNo === lineNo);
        if (index >= 0) scroller.current?.jumpToLine(index);
      },
      onError: (code) => {
        if (voice.current === follow) {
          voice.current = null;
          setVoiceOn(false);
        }
        setNotice(VOICE_ERRORS[code] ?? `Voice follow stopped (${code}) — back to manual scrolling`);
      },
    });
    voice.current = follow;
    setVoiceOn(true);
    setNotice("");
    const line = doc.lines[Math.min(activeIndex, doc.lines.length - 1)];
    void follow.start({ scriptVersion: doc.version, lineNo: line.lineNo });
  }, [activeIndex, doc, id, stopVoice]);

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
        if (!playing) stopVoice(); // manual play takes over from the line voice follow reached
        setPlaying(!playing);
      } else if (action === "back") {
        scroller.current?.backOneLine();
        const back = lines[Math.max(0, activeIndex - 1)];
        if (voice.current && back) voice.current.seek(back.lineNo);
      } else {
        void markRetake();
      }
    },
    [activeIndex, canRead, lines, markRetake, playing, stopVoice],
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
        <button type="button" onClick={() => run("back")} disabled={!canRead}>
          Back one line
        </button>
        <button type="button" onClick={() => run("retake")} disabled={!canRead}>
          Mark retake
        </button>
        <button
          type="button"
          className="prompter-voice"
          aria-pressed={voiceOn}
          onClick={toggleVoice}
          disabled={!canRead}
        >
          Voice follow
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
          <p className="prompter-latency">
            Voice round trip (p95):{" "}
            <span data-testid="prompter-latency">{latency === null ? "—" : `${Math.round(latency)} ms`}</span>
          </p>
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
          onEnd={() => setPlaying(false)}
          onTap={() => run("toggle")}
        />
      )}
    </main>
  );
}
