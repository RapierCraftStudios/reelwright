// SPDX-License-Identifier: AGPL-3.0-or-later
// Retake marks and camera take markers (#50): posted to the API, queued in localStorage while the
// connection is down and flushed in order once it is back, so nothing pressed mid-take is lost.
import { ApiError, type TakeCreate, type TakeMarkCreate, postTake, postTakeMark } from "../api";

export const PENDING_KEY = "rw.prompter.pendingMarks";

/** A queued retake mark. Entries saved before #50 have no type and are retake marks. */
export interface PendingMark {
  type?: "mark";
  projectId: string;
  body: TakeMarkCreate;
}

/** A queued camera take marker. */
export interface PendingTake {
  type: "take";
  projectId: string;
  body: TakeCreate;
}

export type PendingEntry = PendingMark | PendingTake;

function readQueue(): PendingEntry[] {
  try {
    const raw = localStorage.getItem(PENDING_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? (parsed as PendingEntry[]) : [];
  } catch {
    return [];
  }
}

function writeQueue(queue: PendingEntry[]): void {
  try {
    if (queue.length) localStorage.setItem(PENDING_KEY, JSON.stringify(queue));
    else localStorage.removeItem(PENDING_KEY);
  } catch {
    // storage disabled: the queue lives only as long as this call
  }
}

export function pendingCount(projectId?: string): number {
  const queue = readQueue();
  return projectId === undefined ? queue.length : queue.filter((m) => m.projectId === projectId).length;
}

/** A failure worth retrying: network down, timeouts, rate limits and server errors. */
function retryable(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true;
  return error.status === 0 || error.status === 408 || error.status === 429 || error.status >= 500;
}

let flushing: Promise<number> | null = null;

async function drain(): Promise<number> {
  for (;;) {
    const [head] = readQueue();
    if (!head) return 0;
    try {
      if (head.type === "take") await postTake(head.projectId, head.body);
      else await postTakeMark(head.projectId, head.body);
    } catch (error) {
      if (retryable(error)) return readQueue().length;
      console.warn(`dropping a ${head.type === "take" ? "take marker" : "retake mark"} the API refused`, head, error);
    }
    writeQueue(readQueue().slice(1));
  }
}

/** Send queued marks oldest first; stop at the first retryable failure. Returns how many remain. */
export function flushPending(): Promise<number> {
  // .finally always settles asynchronously, so a drain that finishes at once still clears the lock.
  flushing ??= drain().finally(() => {
    flushing = null;
  });
  return flushing;
}

async function submit(entry: PendingEntry): Promise<"sent" | "queued"> {
  if (flushing) await flushing;
  writeQueue([...readQueue(), entry]);
  const remaining = await flushPending();
  return remaining > 0 ? "queued" : "sent";
}

/** Queue a mark and try to send everything. Resolves "sent" or "queued" (sent when back online). */
export function submitMark(projectId: string, body: TakeMarkCreate): Promise<"sent" | "queued"> {
  return submit({ projectId, body });
}

/** Queue a camera take marker behind any earlier entries and try to send everything. */
export function submitTake(projectId: string, body: TakeCreate): Promise<"sent" | "queued"> {
  return submit({ type: "take", projectId, body });
}
