// SPDX-License-Identifier: AGPL-3.0-or-later
// The teleprompter text. Motion is a requestAnimationFrame loop that integrates speed × frame time
// into a translateY transform (no CSS transitions or easing), so the scroll never jumps. With
// prefers-reduced-motion it advances one whole line at a time instead. The transport (playing,
// back one line) comes from the parent. Voice follow (#7) moves the text with jumpToLine, which
// snaps to a line the same way back-one-line does, so manual play resumes from that line.
import {
  type CSSProperties,
  type ReactNode,
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
} from "react";

import type { PrompterSettings } from "./settings";

export interface PrompterLine {
  lineNo: number;
  beat: string;
  text: string;
  /** First spoken line of its beat: drawn with a divider above it. */
  beatStart: boolean;
}

export interface ScrollerHandle {
  backOneLine(): void;
  /** Snap to a line by its index in `lines` (voice follow). */
  jumpToLine(index: number): void;
}

interface ScrollerProps {
  lines: PrompterLine[];
  playing: boolean;
  settings: PrompterSettings;
  reducedMotion: boolean;
  activeIndex: number;
  onActiveIndexChange(index: number): void;
  /** Reached the last line while playing. */
  onEnd?(): void;
  onTap?(): void;
}

/** A frame longer than this (tab hidden, debugger) is treated as this long, so the text never leaps. */
const MAX_FRAME_MS = 50;

const ADLIB_SPLIT = /(\{[^{}]*\})/;

function renderText(text: string): ReactNode[] {
  return text
    .split(ADLIB_SPLIT)
    .filter(Boolean)
    .map((part, i) =>
      part.startsWith("{") && part.endsWith("}") ? (
        <em key={i} className="prompter-adlib">
          {part.slice(1, -1)}
        </em>
      ) : (
        part
      ),
    );
}

export const Scroller = forwardRef<ScrollerHandle, ScrollerProps>(function Scroller(
  { lines, playing, settings, reducedMotion, activeIndex, onActiveIndexChange, onEnd, onTap },
  ref,
) {
  const textRef = useRef<HTMLDivElement>(null);
  const lineRefs = useRef<(HTMLParagraphElement | null)[]>([]);
  const offset = useRef(0);
  const active = useRef(activeIndex);
  active.current = activeIndex;
  const callbacks = useRef({ onActiveIndexChange, onEnd });
  callbacks.current = { onActiveIndexChange, onEnd };

  const top = (index: number) => lineRefs.current[index]?.offsetTop ?? 0;
  const maxOffset = () => top(lines.length - 1);

  const apply = useCallback(() => {
    if (textRef.current) textRef.current.style.transform = `translate3d(0, ${-offset.current}px, 0)`;
  }, []);

  const jumpTo = useCallback(
    (index: number) => {
      const clamped = Math.max(0, Math.min(lines.length - 1, index));
      offset.current = lineRefs.current[clamped]?.offsetTop ?? 0;
      apply();
      if (clamped !== active.current) callbacks.current.onActiveIndexChange(clamped);
    },
    [apply, lines.length],
  );

  useImperativeHandle(
    ref,
    () => ({ backOneLine: () => jumpTo(active.current - 1), jumpToLine: (index: number) => jumpTo(index) }),
    [jumpTo],
  );

  // Text metrics changed (size, margin, font, lines): keep the reader on the same line.
  useLayoutEffect(() => {
    jumpTo(active.current);
  }, [jumpTo, settings.fontSize, settings.margin, settings.dyslexiaFont, settings.lensMode]);

  // Smooth scroll: speed × frame time, linear, clamped at the last line.
  useEffect(() => {
    if (!playing || reducedMotion || lines.length === 0) return;
    let frame = 0;
    let last: number | null = null;
    const step = (now: number) => {
      const dt = last === null ? 0 : Math.min(now - last, MAX_FRAME_MS);
      last = now;
      const end = maxOffset();
      offset.current = Math.min(end, offset.current + (settings.speed * dt) / 1000);
      apply();
      let index = active.current;
      while (index < lines.length - 1 && top(index + 1) <= offset.current + 0.5) index += 1;
      if (index !== active.current) callbacks.current.onActiveIndexChange(index);
      if (offset.current >= end) {
        callbacks.current.onEnd?.();
        return;
      }
      frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [playing, reducedMotion, settings.speed, lines.length, apply]);

  // Reduced motion: whole-line steps, each held as long as the smooth scroll would take to cover it.
  useEffect(() => {
    if (!playing || !reducedMotion || lines.length === 0) return;
    let timer = 0;
    const schedule = () => {
      const index = active.current;
      if (index >= lines.length - 1) {
        callbacks.current.onEnd?.();
        return;
      }
      const distance = Math.max(top(index + 1) - top(index), settings.fontSize);
      timer = window.setTimeout(() => {
        jumpTo(active.current + 1);
        schedule();
      }, (distance / settings.speed) * 1000);
    };
    schedule();
    return () => window.clearTimeout(timer);
  }, [playing, reducedMotion, settings.speed, settings.fontSize, lines.length, jumpTo]);

  const viewportStyle = { "--prompter-margin": `${settings.margin}%` } as CSSProperties;
  const classes = [
    "prompter-viewport",
    `lens-${settings.lensMode}`,
    settings.mirror ? "is-mirrored" : "",
    settings.dyslexiaFont ? "prompter-dyslexia" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={classes} style={viewportStyle} data-testid="prompter-viewport" onClick={onTap}>
      <div className="prompter-reading-line" aria-hidden="true" />
      <div
        ref={textRef}
        className="prompter-text"
        data-testid="prompter-text"
        style={{ fontSize: `${settings.fontSize}px` }}
      >
        {lines.map((line, i) => (
          <p
            key={line.lineNo}
            ref={(el) => {
              lineRefs.current[i] = el;
            }}
            className={line.beatStart && i > 0 ? "prompter-line beat-start" : "prompter-line"}
            data-line={line.lineNo}
            data-beat={line.beat}
            data-active={i === activeIndex ? "true" : undefined}
            aria-current={i === activeIndex ? "true" : undefined}
          >
            {renderText(line.text)}
          </p>
        ))}
      </div>
    </div>
  );
});
