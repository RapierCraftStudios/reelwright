// SPDX-License-Identifier: AGPL-3.0-or-later
// Retake marks: posted to the API, queued in localStorage while the connection is down and flushed
// in order once it is back, so a mark pressed mid-take is never lost.
import { ApiError, type TakeMarkCreate, postTakeMark } from "../api";

export const PENDING_KEY = "rw.prompter.pendingMarks";

export interface PendingMark {
  projectId: string;
  body: TakeMarkCreate;
}

function readQueue(): PendingMark[] {
  try {
    const raw = localStorage.getItem(PENDING_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? (parsed as PendingMark[]) : [];
  } catch {
    return [];
  }
}

function writeQueue(queue: PendingMark[]): void {
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

/** Send queued marks oldest first; stop at the first retryable failure. Returns how many remain. */
export function flushPending(): Promise<number> {
  flushing ??= (async () => {
    try {
      for (;;) {
        const [head] = readQueue();
        if (!head) return 0;
        try {
          await postTakeMark(head.projectId, head.body);
        } catch (error) {
          if (retryable(error)) return readQueue().length;
          console.warn("dropping a retake mark the API refused", head, error);
        }
        writeQueue(readQueue().slice(1));
      }
    } finally {
      flushing = null;
    }
  })();
  return flushing;
}

/** Queue a mark and try to send everything. Resolves "sent" or "queued" (sent when back online). */
export async function submitMark(projectId: string, body: TakeMarkCreate): Promise<"sent" | "queued"> {
  if (flushing) await flushing;
  writeQueue([...readQueue(), { projectId, body }]);
  const remaining = await flushPending();
  return remaining > 0 ? "queued" : "sent";
}
