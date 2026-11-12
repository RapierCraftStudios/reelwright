// SPDX-License-Identifier: AGPL-3.0-or-later
// Script editor: live beat/cue highlighting from the server parser, inline parse errors, outline,
// cue inspector, save (new version) and lock. Grammar: docs/script-format.md.
import {
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Link, useParams } from "react-router-dom";

import {
  ApiError,
  type Beat,
  type Cue,
  type CueType,
  type ParseResult,
  type ScriptDoc,
  type ScriptError,
  generateScript,
  getScript,
  lockScript,
  parseScript,
  putScript,
} from "../api";

export const CUE_ICONS: Record<CueType, string> = {
  broll: "🎞",
  anim: "✨",
  screen: "🖥",
  fact: "✔",
  sfx: "🔊",
  music: "♪",
  cut: "✂",
  short: "▮",
};
export const BEAT_ICON = "¶";
export const PARSE_DEBOUNCE_MS = 250;
export const EMPTY_MESSAGE = "No script yet — write one or generate from a topic";
export const NEW_SCRIPT_TEMPLATE = `---
format: long
working_title: Untitled
---

## beat: hook | intent: why they should keep watching
`;

type Phase = "loading" | "empty" | "editing" | "offline" | "load-error";

interface Cached {
  source: string;
  version: number | null;
  locked: boolean;
}

interface Banner {
  message: string;
  retry?: () => void;
}

const cacheKey = (id: string) => `rw.script.${id}`;

function readCache(id: string): Cached | null {
  try {
    const raw = localStorage.getItem(cacheKey(id));
    return raw ? (JSON.parse(raw) as Cached) : null;
  } catch {
    return null;
  }
}

function writeCache(id: string, cached: Cached) {
  try {
    localStorage.setItem(cacheKey(id), JSON.stringify(cached));
  } catch {
    // storage full or disabled: offline mode just has no copy
  }
}

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function lineOfOffset(text: string, offset: number): number {
  return text.slice(0, offset).split("\n").length;
}

function offsetOfLine(text: string, line: number): number {
  let offset = 0;
  for (let n = 1; n < line; n++) {
    const next = text.indexOf("\n", offset);
    if (next === -1) return text.length;
    offset = next + 1;
  }
  return offset;
}

interface Range {
  start: number;
  end: number;
  className: string;
}

function lineRanges(text: string, cues: Cue[], errors: ScriptError[], todoCols: number[]): Range[] {
  const ranges: Range[] = cues.map((c) => ({
    start: c.col - 1,
    end: c.end_col - 1,
    className: `hl-cue cue-${c.type}`,
  }));
  for (const col of todoCols) {
    const end = text.indexOf("]", col - 1);
    ranges.push({ start: col - 1, end: end === -1 ? text.length : end + 1, className: "hl-todo" });
  }
  for (const e of errors) {
    if (e.col <= 1 && !text.startsWith("[")) continue;
    const end = text.indexOf("]", e.col - 1);
    ranges.push({
      start: e.col - 1,
      end: end === -1 ? text.length : end + 1,
      className: "hl-error-text",
    });
  }
  for (const m of text.matchAll(/\{[^{}]*\}/g)) {
    ranges.push({ start: m.index, end: m.index + m[0].length, className: "hl-adlib" });
  }
  ranges.sort((a, b) => a.start - b.start);
  const kept: Range[] = [];
  for (const r of ranges) {
    if (kept.length && r.start < kept[kept.length - 1].end) continue;
    kept.push(r);
  }
  return kept;
}

function highlight(source: string, parsed: ParseResult | null): ReactNode[] {
  const beatLines = new Set(parsed?.script.beats.map((b) => b.line) ?? []);
  const cues = new Map<number, Cue[]>();
  for (const beat of parsed?.script.beats ?? []) {
    for (const line of beat.lines) {
      for (const cue of line.cues) cues.set(cue.line, [...(cues.get(cue.line) ?? []), cue]);
    }
  }
  const errors = new Map<number, ScriptError[]>();
  for (const e of parsed?.errors ?? []) errors.set(e.line, [...(errors.get(e.line) ?? []), e]);
  const todos = new Map<number, number[]>();
  for (const t of parsed?.script.todos ?? []) todos.set(t.line, [...(todos.get(t.line) ?? []), t.col]);

  return source.split("\n").map((text, i) => {
    const n = i + 1;
    const lineErrors = errors.get(n) ?? [];
    const lineCues = cues.get(n) ?? [];
    const body: ReactNode[] = [];
    if (beatLines.has(n)) {
      body.push(
        <span key="beat" className="hl-beat">
          {text}
        </span>,
      );
    } else {
      let pos = 0;
      for (const r of lineRanges(text, lineCues, lineErrors, todos.get(n) ?? [])) {
        if (r.start > pos) body.push(text.slice(pos, r.start));
        body.push(
          <span key={r.start} className={r.className}>
            {text.slice(r.start, r.end)}
          </span>,
        );
        pos = r.end;
      }
      if (pos < text.length) body.push(text.slice(pos));
    }
    const annotations: ReactNode[] = [];
    if (beatLines.has(n)) annotations.push(<span key="b">{`  ${BEAT_ICON} beat`}</span>);
    for (const cue of lineCues) {
      annotations.push(
        <span key={`c${cue.col}`} className={`cue-${cue.type}`}>
          {`  ${CUE_ICONS[cue.type]} ${cue.type}`}
        </span>,
      );
    }
    for (const e of lineErrors) {
      annotations.push(
        <span key={`e${e.col}${e.message}`} className="hl-annot-error" data-testid="inline-error">
          {`  ⚠ ${e.message}`}
        </span>,
      );
    }
    return (
      <div
        key={n}
        data-line={n}
        className={lineErrors.length ? "hl-line hl-error-line" : "hl-line"}
      >
        {body.length ? body : " "}
        {annotations.length > 0 && <span className="hl-annot">{annotations}</span>}
      </div>
    );
  });
}

function CueLabel({ cue }: { cue: Cue }) {
  return (
    <span className={`cue-${cue.type}`}>
      <span aria-hidden="true">{CUE_ICONS[cue.type]} </span>
      <strong>{cue.type}</strong>
    </span>
  );
}

function OutlineItem({ beat, onGo }: { beat: Beat; onGo: (line: number) => void }) {
  const [open, setOpen] = useState(false);
  const bodyId = `beat-${beat.index}-details`;
  const cues = beat.lines.flatMap((l) => l.cues);
  return (
    <li className="outline-item">
      <button
        type="button"
        className="outline-toggle"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen((o) => !o)}
      >
        <span aria-hidden="true">{open ? "▾" : "▸"} </span>
        <span className="hl-beat">
          <span aria-hidden="true">{BEAT_ICON} </span>
          {beat.slug}
        </span>{" "}
        <span className="hl-annot">({beat.type})</span>
      </button>
      <div id={bodyId} className="outline-body" data-open={open} inert={!open}>
        <div>
          <p>{beat.intent}</p>
          <p>
            {cues.length} {cues.length === 1 ? "cue" : "cues"}
          </p>
          <button type="button" onClick={() => onGo(beat.line)}>
            Go to line {beat.line}
          </button>
        </div>
      </div>
    </li>
  );
}

export function ScriptEditorRoute() {
  const { id = "" } = useParams();
  const [phase, setPhase] = useState<Phase>("loading");
  const [source, setSource] = useState("");
  const [savedSource, setSavedSource] = useState("");
  const [version, setVersion] = useState<number | null>(null);
  const [locked, setLocked] = useState(false);
  const [parsed, setParsed] = useState<ParseResult | null>(null);
  const [banner, setBanner] = useState<Banner | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [caretLine, setCaretLine] = useState(1);
  const [topicOpen, setTopicOpen] = useState(false);
  const [topic, setTopic] = useState("");
  const [hasCache, setHasCache] = useState(true);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const highlightRef = useRef<HTMLDivElement>(null);
  const parseSeq = useRef(0);
  const sourceRef = useRef(source);
  sourceRef.current = source;

  const goOffline = useCallback(
    (keepCurrent: boolean) => {
      if (!keepCurrent) {
        const cached = readCache(id);
        setHasCache(cached !== null);
        setSource(cached?.source ?? "");
        setSavedSource(cached?.source ?? "");
        setVersion(cached?.version ?? null);
        setLocked(cached?.locked ?? false);
      }
      setPhase("offline");
    },
    [id],
  );

  const applyDoc = useCallback(
    (doc: ScriptDoc, replaceSource: boolean) => {
      if (replaceSource) setSource(doc.version.source);
      setSavedSource(doc.version.source);
      setVersion(doc.version.version);
      setLocked(doc.locked);
      if (replaceSource) setParsed({ script: doc.script, errors: doc.errors });
      writeCache(id, { source: doc.version.source, version: doc.version.version, locked: doc.locked });
    },
    [id],
  );

  const load = useCallback(async (): Promise<void> => {
    if (!navigator.onLine) {
      goOffline(false);
      return;
    }
    setPhase("loading");
    setBanner(null);
    try {
      applyDoc(await getScript(id), true);
      setPhase("editing");
    } catch (e) {
      if (e instanceof ApiError && e.status === 404 && e.message === "no script yet") {
        setPhase("empty");
      } else if (e instanceof ApiError && e.status === 0) {
        goOffline(false);
      } else {
        setPhase("load-error");
        setBanner({ message: `Could not load the script: ${errorText(e)}`, retry: () => void load() });
      }
    }
  }, [id, applyDoc, goOffline]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const offline = () => setPhase((p) => (p === "editing" ? "offline" : p));
    const online = () => {
      if (sourceRef.current) setPhase((p) => (p === "offline" ? "editing" : p));
      else void load();
    };
    window.addEventListener("offline", offline);
    window.addEventListener("online", online);
    return () => {
      window.removeEventListener("offline", offline);
      window.removeEventListener("online", online);
    };
  }, [load]);

  useEffect(() => {
    if (phase !== "editing") return;
    const seq = ++parseSeq.current;
    const timer = setTimeout(() => {
      parseScript(id, source)
        .then((result) => {
          if (seq === parseSeq.current) setParsed(result);
        })
        .catch(() => {
          // keep the last highlights; save reports real failures
        });
    }, PARSE_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [id, source, phase]);

  const save = useCallback(async (): Promise<void> => {
    if (phase !== "editing" || locked) return;
    const toSave = sourceRef.current;
    setBusy(true);
    setBanner(null);
    try {
      const doc = await putScript(id, toSave);
      applyDoc(doc, false);
      setNotice(`Saved version ${doc.version.version}`);
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) {
        const errors = (e.detail as { errors?: ScriptError[] } | null)?.errors ?? [];
        setParsed((p) => ({ script: p?.script ?? { front_matter: null, beats: [], todos: [] }, errors }));
        setBanner({ message: "Not saved: fix the parse errors marked in the editor." });
      } else {
        setBanner({ message: `Save failed: ${errorText(e)}`, retry: () => void saveRef.current() });
      }
    } finally {
      setBusy(false);
    }
  }, [id, phase, locked, applyDoc]);
  const saveRef = useRef(save);
  saveRef.current = save;

  async function lock() {
    setBusy(true);
    setBanner(null);
    try {
      const doc = await lockScript(id);
      applyDoc(doc, false);
      setNotice(`Locked version ${doc.version.version}`);
    } catch (e) {
      const todos = (e instanceof ApiError
        ? ((e.detail as { todos?: { line: number }[] } | null)?.todos ?? [])
        : []
      ).map((t) => t.line);
      const where = todos.length ? ` (TODO on line ${todos.join(", ")})` : "";
      setBanner({ message: `Lock refused: ${errorText(e)}${where}` });
    } finally {
      setBusy(false);
    }
  }

  async function generate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const wanted = topic.trim();
    if (!wanted) return;
    setBusy(true);
    setBanner(null);
    try {
      applyDoc(await generateScript(id, wanted), true);
      setPhase("editing");
    } catch (e) {
      setBanner({
        message: `Could not generate a script: ${errorText(e)}`,
        retry: () => textareaRef.current?.form?.requestSubmit(),
      });
    } finally {
      setBusy(false);
    }
  }

  function startWriting() {
    setSource(NEW_SCRIPT_TEMPLATE);
    setPhase("editing");
    requestAnimationFrame(() => textareaRef.current?.focus());
  }

  function goToLine(line: number) {
    const area = textareaRef.current;
    if (!area) return;
    const offset = offsetOfLine(area.value, line);
    area.focus();
    area.setSelectionRange(offset, offset);
    const lineHeight = parseFloat(getComputedStyle(area).lineHeight) || 24;
    area.scrollTop = Math.max(0, (line - 2) * lineHeight);
    setCaretLine(line);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      void save();
    }
  }

  function trackCaret() {
    const area = textareaRef.current;
    if (area) setCaretLine(lineOfOffset(area.value, area.selectionStart));
  }

  function syncScroll() {
    const area = textareaRef.current;
    const layer = highlightRef.current;
    if (area && layer) {
      layer.scrollTop = area.scrollTop;
      layer.scrollLeft = area.scrollLeft;
    }
  }

  const script = parsed?.script;
  const errors = parsed?.errors ?? [];
  const todos = script?.todos ?? [];
  const dirty = source !== savedSource;
  const readOnly = phase !== "editing" || locked;
  const canLock =
    phase === "editing" && !locked && !dirty && version !== null && !errors.length && !todos.length;
  const highlighted = useMemo(() => highlight(source, parsed), [source, parsed]);
  const currentBeat = useMemo(() => {
    const beats = script?.beats ?? [];
    return [...beats].reverse().find((b) => b.line <= caretLine) ?? beats[0];
  }, [script, caretLine]);
  const currentCues = currentBeat?.lines.flatMap((l) => l.cues) ?? [];

  return (
    <main className="script-page">
      <p>
        <Link to="/">← Projects</Link>
      </p>
      <h1>Script</h1>

      {phase === "offline" && (
        <div className="banner banner-offline" role="status">
          <span aria-hidden="true">⚡</span>
          {hasCache || source
            ? "Offline — showing the last saved copy, read-only."
            : "Offline — no saved copy of this script on this device."}
        </div>
      )}
      {banner && (
        <div className="banner banner-error" role="alert">
          <span aria-hidden="true">⚠</span>
          <span>{banner.message}</span>
          {banner.retry && (
            <button type="button" onClick={banner.retry}>
              Retry
            </button>
          )}
        </div>
      )}
      <p role="status" className="hl-annot">
        {notice}
      </p>

      {phase === "loading" && (
        <div className="panel" role="status" aria-busy="true" data-testid="editor-skeleton">
          <span>Loading script…</span>
          <div className="skeleton" style={{ width: "40%" }} />
          <div className="skeleton" style={{ width: "90%" }} />
          <div className="skeleton" style={{ width: "75%" }} />
          <div className="skeleton" style={{ width: "85%" }} />
        </div>
      )}

      {phase === "empty" && (
        <section className="panel empty-state" aria-labelledby="empty-title">
          <p id="empty-title">{EMPTY_MESSAGE}</p>
          <div className="empty-actions">
            <button type="button" onClick={startWriting}>
              Write a script
            </button>
            <button type="button" aria-expanded={topicOpen} onClick={() => setTopicOpen(true)}>
              Generate from a topic
            </button>
          </div>
          {topicOpen && (
            <form onSubmit={generate} aria-label="Generate from a topic" className="empty-actions">
              <label>
                Topic <input value={topic} onChange={(e) => setTopic(e.target.value)} required />
              </label>
              <button type="submit" disabled={busy}>
                Generate
              </button>
            </form>
          )}
        </section>
      )}

      {(phase === "editing" || (phase === "offline" && (hasCache || source))) && (
        <>
          <div className="script-toolbar">
            <button type="button" onClick={() => void save()} disabled={readOnly || busy || !dirty}>
              Save
            </button>
            <button type="button" onClick={() => void lock()} disabled={!canLock || busy}>
              Lock
            </button>
            <span className="hl-annot" data-testid="script-state">
              {version === null ? "Not saved yet" : `Version ${version}`}
              {locked ? " · 🔒 Locked" : ""}
              {dirty && !locked ? " · Unsaved changes" : ""}
              {todos.length ? ` · ${todos.length} TODO left` : ""}
            </span>
          </div>
          <div className="script-layout">
            <nav className="script-outline panel" aria-label="Beat outline">
              <h2>Outline</h2>
              {script?.beats.length ? (
                <ul className="outline-list">
                  {script.beats.map((beat) => (
                    <OutlineItem key={`${beat.index}-${beat.slug}`} beat={beat} onGo={goToLine} />
                  ))}
                </ul>
              ) : (
                <p>No beats yet.</p>
              )}
            </nav>

            <div className="script-main">
              <div className="editor">
                <div className="editor-highlight" aria-hidden="true" ref={highlightRef} data-testid="highlight">
                  {highlighted}
                </div>
                <textarea
                  ref={textareaRef}
                  className="editor-input"
                  aria-label="Script"
                  aria-describedby="script-errors"
                  spellCheck={false}
                  value={source}
                  readOnly={readOnly}
                  onChange={(e) => setSource(e.target.value)}
                  onKeyDown={onKeyDown}
                  onKeyUp={trackCaret}
                  onClick={trackCaret}
                  onSelect={trackCaret}
                  onScroll={syncScroll}
                />
              </div>
              <div id="script-errors" role="status">
                {errors.length > 0 && (
                  <ul className="error-list" aria-label="Parse errors">
                    {errors.map((e) => (
                      <li key={`${e.line}:${e.col}:${e.message}`}>
                        <span aria-hidden="true">⚠ </span>
                        <button type="button" className="outline-toggle" onClick={() => goToLine(e.line)}>
                          Line {e.line}, col {e.col}: {e.message}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>

            <aside className="script-inspector panel" aria-label="Cue inspector">
              <h2>Cues{currentBeat ? ` in ${currentBeat.slug}` : ""}</h2>
              {currentCues.length ? (
                <ul className="cue-list">
                  {currentCues.map((cue) => (
                    <li key={`${cue.line}:${cue.col}`}>
                      <CueLabel cue={cue} /> {cue.payload || "—"}{" "}
                      <button type="button" onClick={() => goToLine(cue.line)}>
                        Line {cue.line}
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p>No cues in this beat.</p>
              )}
            </aside>
          </div>
        </>
      )}
    </main>
  );
}
