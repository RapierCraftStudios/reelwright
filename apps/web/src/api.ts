// SPDX-License-Identifier: AGPL-3.0-or-later
export const PROJECT_FORMATS = ["long", "short", "vlog"] as const;
export type ProjectFormat = (typeof PROJECT_FORMATS)[number];

export interface Project {
  id: number;
  slug: string;
  title: string;
  format: ProjectFormat;
  status: string;
  target_publish_date: string | null;
  created_at: string;
}

export interface ProjectCreate {
  title: string;
  format: ProjectFormat;
}

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

export async function listProjects(): Promise<Project[]> {
  return json<Project[]>(await fetch("/api/projects"));
}

export async function createProject(body: ProjectCreate): Promise<Project> {
  return json<Project>(
    await fetch("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export const CUE_TYPES = ["broll", "anim", "screen", "fact", "sfx", "music", "cut", "short"] as const;
export type CueType = (typeof CUE_TYPES)[number];

export interface Cue {
  type: CueType;
  payload: string;
  standalone: boolean;
  at: number;
  line: number;
  col: number;
  end_col: number;
}

export interface ScriptLine {
  kind: "spoken" | "cue" | "todo";
  text: string;
  line_no: number;
  cues: Cue[];
  adlibs: string[];
}

export interface Beat {
  slug: string;
  type: string;
  intent: string;
  index: number;
  line: number;
  lines: ScriptLine[];
}

export interface Todo {
  text: string;
  line: number;
  col: number;
}

export interface Script {
  front_matter: Record<string, unknown> | null;
  beats: Beat[];
  todos: Todo[];
}

export interface ScriptError {
  line: number;
  col: number;
  message: string;
}

export interface ScriptVersion {
  id: number;
  project_id: number;
  version: number;
  source: string;
  locked_at: string | null;
  created_at: string;
}

export interface ScriptDoc {
  version: ScriptVersion;
  locked: boolean;
  script: Script;
  errors: ScriptError[];
}

export interface ParseResult {
  script: Script;
  errors: ScriptError[];
}

/** A failed API call: HTTP status plus the parsed `detail` body (null for network failures). */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: unknown,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function call<T>(url: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, init);
  } catch (e) {
    throw new ApiError(0, null, `network error: ${String(e)}`);
  }
  if (!response.ok) {
    let detail: unknown = null;
    try {
      detail = ((await response.json()) as { detail?: unknown }).detail ?? null;
    } catch {
      detail = null;
    }
    const message =
      typeof detail === "string"
        ? detail
        : ((detail as { message?: string } | null)?.message ?? `${response.status} ${response.statusText}`);
    throw new ApiError(response.status, detail, message);
  }
  return (await response.json()) as T;
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

const scriptUrl = (projectId: number | string) => `/api/projects/${projectId}/script`;

export function getScript(projectId: number | string): Promise<ScriptDoc> {
  return call<ScriptDoc>(scriptUrl(projectId));
}

export function putScript(projectId: number | string, source: string): Promise<ScriptDoc> {
  return call<ScriptDoc>(scriptUrl(projectId), jsonInit("PUT", { source }));
}

export function parseScript(projectId: number | string, source: string): Promise<ParseResult> {
  return call<ParseResult>(`${scriptUrl(projectId)}/parse`, jsonInit("POST", { source }));
}

export function lockScript(projectId: number | string): Promise<ScriptDoc> {
  return call<ScriptDoc>(`${scriptUrl(projectId)}/lock`, jsonInit("POST"));
}

export function listScriptVersions(projectId: number | string): Promise<ScriptVersion[]> {
  return call<ScriptVersion[]>(`${scriptUrl(projectId)}/versions`);
}

/** Script writer endpoint, implemented by issue #5. */
export function generateScript(projectId: number | string, topic: string): Promise<ScriptDoc> {
  return call<ScriptDoc>(`${scriptUrl(projectId)}/generate`, jsonInit("POST", { topic }));
}

export interface TakeMarkCreate {
  kind: "retake";
  /** Version number of the locked script being read. */
  script_version: number;
  beat_slug: string;
  line_no: number;
  /** Device wall clock, ISO 8601 with timezone. */
  marked_at: string;
  /** Milliseconds since the reader pressed start. */
  offset_ms: number;
}

export interface TakeMark extends TakeMarkCreate {
  id: number;
  project_id: number;
  script_version_id: number;
  created_at: string;
}

const prompterUrl = (projectId: number | string) => `/api/prompter/${projectId}`;

export function postTakeMark(projectId: number | string, body: TakeMarkCreate): Promise<TakeMark> {
  return call<TakeMark>(`${prompterUrl(projectId)}/marks`, jsonInit("POST", body));
}

export function listTakeMarks(projectId: number | string): Promise<TakeMark[]> {
  return call<TakeMark[]>(`${prompterUrl(projectId)}/marks`);
}
