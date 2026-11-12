// SPDX-License-Identifier: AGPL-3.0-or-later
// Teleprompter settings, stored per device (not per project) in localStorage, plus the key map
// shared by the keyboard and Bluetooth page-turner pedals (which send PageUp/PageDown or arrows).

export type LensMode = "beam-splitter" | "above-lens";

export interface PrompterSettings {
  /** Text size in CSS px. */
  fontSize: number;
  /** Scroll speed in CSS px per second. */
  speed: number;
  /** Side margin as a percentage of the screen width. */
  margin: number;
  /** Flip horizontally for a beam-splitter glass. */
  mirror: boolean;
  /** Where the reading line sits: mid-upper for a beam splitter, near the lens edge above the lens. */
  lensMode: LensMode;
  dyslexiaFont: boolean;
}

export const SETTINGS_KEY = "rw.prompter.settings";

export const LIMITS = {
  fontSize: { min: 24, max: 120, step: 2 },
  speed: { min: 10, max: 300, step: 5 },
  margin: { min: 0, max: 30, step: 1 },
} as const;

export const DEFAULT_SETTINGS: PrompterSettings = {
  fontSize: 48,
  speed: 60,
  margin: 6,
  mirror: false,
  lensMode: "beam-splitter",
  dyslexiaFont: false,
};

const LENS_MODES: readonly LensMode[] = ["beam-splitter", "above-lens"];

function clamp(value: unknown, key: keyof typeof LIMITS): number {
  const { min, max } = LIMITS[key];
  const n = typeof value === "number" && Number.isFinite(value) ? value : DEFAULT_SETTINGS[key];
  return Math.min(max, Math.max(min, n));
}

/** Coerce anything (stored JSON, partial updates) into valid settings. */
export function normalizeSettings(raw: unknown): PrompterSettings {
  const r = (raw && typeof raw === "object" ? raw : {}) as Partial<Record<keyof PrompterSettings, unknown>>;
  return {
    fontSize: clamp(r.fontSize, "fontSize"),
    speed: clamp(r.speed, "speed"),
    margin: clamp(r.margin, "margin"),
    mirror: typeof r.mirror === "boolean" ? r.mirror : DEFAULT_SETTINGS.mirror,
    lensMode: LENS_MODES.includes(r.lensMode as LensMode) ? (r.lensMode as LensMode) : DEFAULT_SETTINGS.lensMode,
    dyslexiaFont: typeof r.dyslexiaFont === "boolean" ? r.dyslexiaFont : DEFAULT_SETTINGS.dyslexiaFont,
  };
}

function storage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

export function loadSettings(): PrompterSettings {
  try {
    const raw = storage()?.getItem(SETTINGS_KEY);
    return normalizeSettings(raw ? JSON.parse(raw) : null);
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}

export function saveSettings(settings: PrompterSettings): void {
  try {
    storage()?.setItem(SETTINGS_KEY, JSON.stringify(normalizeSettings(settings)));
  } catch {
    // storage full or disabled: settings just won't survive a reload
  }
}

export type PrompterAction = "toggle" | "back" | "retake";

/** Keyboard and pedal keys → action. One place so voice-follow (#7) and camera control (#50) can extend it. */
export const KEYMAP: Readonly<Record<string, PrompterAction>> = {
  " ": "toggle",
  PageDown: "toggle",
  ArrowRight: "toggle",
  ArrowDown: "toggle",
  ArrowUp: "back",
  PageUp: "back",
  ArrowLeft: "back",
  r: "retake",
  R: "retake",
  Enter: "retake",
};

export function actionForKey(event: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "altKey">): PrompterAction | null {
  if (event.ctrlKey || event.metaKey || event.altKey) return null;
  return KEYMAP[event.key] ?? null;
}
