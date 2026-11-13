// SPDX-License-Identifier: AGPL-3.0-or-later
// The approval queue: nothing goes out without a recorded human approval of the exact payload shown.
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  approvePublication,
  getPublication,
  listPublications,
  type Publication,
  type PublicationDiff,
  retryPublication,
  updatePublication,
} from "../api";

const CACHE_KEY = "reelwright.approvals";
const APPROVER_KEY = "reelwright.approver";

type GroupId = "waiting" | "scheduled" | "live";

const GROUPS: { id: GroupId; label: string }[] = [
  { id: "waiting", label: "Waiting" },
  { id: "scheduled", label: "Scheduled" },
  { id: "live", label: "Live" },
];

export function groupOf(p: Publication): GroupId {
  if (p.status === "live") return "live";
  if (p.status === "scheduled" || p.status === "approved") return "scheduled";
  return "waiting";
}

function readCache(): Publication[] | null {
  try {
    const raw = window.localStorage.getItem(CACHE_KEY);
    return raw ? (JSON.parse(raw) as Publication[]) : null;
  } catch {
    return null;
  }
}

function writeCache(items: Publication[]) {
  try {
    window.localStorage.setItem(CACHE_KEY, JSON.stringify(items));
  } catch {
    // storage full or disabled: the list still works, it just is not kept offline
  }
}

function useOnline(): boolean {
  const [online, setOnline] = useState(() => (typeof navigator === "undefined" ? true : navigator.onLine));
  useEffect(() => {
    const up = () => setOnline(true);
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, []);
  return online;
}

function text(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) {
    return value.map((v) => (typeof v === "object" ? JSON.stringify(v) : String(v))).join(", ") || "—";
  }
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function change(d: PublicationDiff): "Added" | "Removed" | "Changed" {
  const empty = (v: unknown) => v === null || v === undefined || v === "" || (Array.isArray(v) && v.length === 0);
  if (empty(d.before)) return "Added";
  if (empty(d.after)) return "Removed";
  return "Changed";
}

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "now";
}

interface Pending {
  kind: "approve" | "schedule";
  publication: Publication;
  at: string | null;
}

export function ApprovalsRoute() {
  const cached = useRef(readCache());
  const [items, setItems] = useState<Publication[]>(cached.current ?? []);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(cached.current?.[0]?.id ?? null);
  const [movedId, setMovedId] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const [scheduleAt, setScheduleAt] = useState("");
  const [approver, setApprover] = useState(() => window.localStorage.getItem(APPROVER_KEY) ?? "");
  const online = useOnline();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);

  const replace = useCallback((next: Publication) => {
    setItems((rows) => {
      const updated = rows.some((r) => r.id === next.id)
        ? rows.map((r) => (r.id === next.id ? next : r))
        : [...rows, next];
      writeCache(updated);
      return updated;
    });
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const rows = await listPublications();
      setItems(rows);
      writeCache(rows);
      setLoadError(null);
      setSelectedId((id) => (id !== null && rows.some((r) => r.id === id) ? id : (rows[0]?.id ?? null)));
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (pending && !dialog.open) {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    } else if (!pending && dialog.open) {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }
  }, [pending]);

  const selected = items.find((p) => p.id === selectedId) ?? null;
  const readOnly = !online || busy;

  function ask(kind: Pending["kind"], publication: Publication) {
    openerRef.current = document.activeElement as HTMLElement | null;
    const at = kind === "schedule" && scheduleAt ? new Date(scheduleAt).toISOString() : publication.publish_at;
    setActionError(null);
    setPending({ kind, publication, at });
  }

  function cancel() {
    setPending(null);
    openerRef.current?.focus();
  }

  async function confirm(e: FormEvent) {
    e.preventDefault();
    if (!pending || !approver.trim()) return;
    const { kind, publication, at } = pending;
    window.localStorage.setItem(APPROVER_KEY, approver.trim());
    setPending(null);
    setBusy(true);
    try {
      let target = publication;
      if (kind === "schedule") {
        // Re-hash with the new time first; the approval then signs the payload that holds it.
        target = await updatePublication(publication.id, { publish_at: at });
        replace(target);
      }
      const done = await approvePublication(target.id, approver.trim(), target.payload_hash);
      setMovedId(done.id);
      replace(done);
      setScheduleAt("");
    } catch (err) {
      setActionError(err instanceof ApiError || err instanceof Error ? err.message : String(err));
      try {
        replace(await getPublication(publication.id));
      } catch {
        // keep the row as it was; the banner already says what went wrong
      }
    } finally {
      setBusy(false);
    }
  }

  async function retry(publication: Publication) {
    setBusy(true);
    setActionError(null);
    try {
      const done = await retryPublication(publication.id);
      setMovedId(done.id);
      replace(done);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
      try {
        replace(await getPublication(publication.id));
      } catch {
        // as above
      }
    } finally {
      setBusy(false);
    }
  }

  const waiting = items.filter((p) => groupOf(p) === "waiting");
  const showSkeleton = loading && items.length === 0;

  return (
    <main className="approvals-page">
      <header className="approvals-header">
        <h1>Approvals</h1>
        <a href="/">Projects</a>
      </header>
      {!online && (
        <div className="banner banner-offline" role="status">
          You are offline — approvals are read-only
        </div>
      )}
      {loadError && (
        <div className="banner banner-error" role="alert">
          Could not load the queue: {loadError}
          <button type="button" onClick={() => void load()} disabled={!online}>
            Reload
          </button>
        </div>
      )}
      {actionError && (
        <div className="banner banner-error" role="alert">
          {actionError}
        </div>
      )}
      <div className="approvals">
        <section className="approvals-list" aria-label="Publications" aria-busy={showSkeleton}>
          {showSkeleton ? (
            <div data-testid="approvals-skeleton">
              {[0, 1, 2].map((i) => (
                <div key={i} className="skeleton skeleton-card" />
              ))}
            </div>
          ) : (
            GROUPS.map((group) => {
              const rows = items.filter((p) => groupOf(p) === group.id);
              return (
                <section key={group.id} className="approvals-group" aria-labelledby={`group-${group.id}`}>
                  <h2 id={`group-${group.id}`}>
                    {group.label} <span className="approvals-count">({rows.length})</span>
                  </h2>
                  {group.id === "waiting" && waiting.length === 0 && (
                    <p className="empty-state">Nothing waiting for approval</p>
                  )}
                  <ul className="approvals-cards">
                    {rows.map((p) => (
                      <li
                        key={p.id}
                        className={[
                          "approval-card",
                          p.id === selectedId ? "approval-card-selected" : "",
                          p.id === movedId ? "card-moving" : "",
                        ]
                          .join(" ")
                          .trim()}
                        data-testid={`publication-${p.id}`}
                      >
                        <button
                          type="button"
                          className="approval-card-select"
                          aria-pressed={p.id === selectedId}
                          onClick={() => setSelectedId(p.id)}
                        >
                          <span className="approval-card-title">{text(p.payload.title)}</span>
                          <span className="approval-card-meta">
                            {p.platform} · {p.status}
                            {p.remote_id ? ` · ${p.remote_id}` : ""}
                            {p.publish_at ? ` · ${when(p.publish_at)}` : ""}
                          </span>
                        </button>
                        {p.status === "failed" && (
                          <div className="approval-card-error" role="alert">
                            <span>Failed: {p.error ?? "unknown error"}</span>
                            <button type="button" onClick={() => void retry(p)} disabled={readOnly}>
                              Retry
                            </button>
                          </div>
                        )}
                      </li>
                    ))}
                  </ul>
                </section>
              );
            })
          )}
        </section>

        {selected && (
          <section className="approvals-preview" aria-label="Preview">
            <h2>Preview</h2>
            {selected.preview_url ? (
              <video className="approvals-video" src={selected.preview_url} controls preload="metadata" />
            ) : (
              <p className="approvals-muted">No preview yet</p>
            )}
            <article className="approvals-card-preview">
              <p className="approvals-muted">
                Thumbnail: {text(selected.payload.thumbnail_path)}
              </p>
              <h3>{text(selected.payload.title)}</h3>
              <pre className="approvals-description">{text(selected.payload.description)}</pre>
            </article>
          </section>
        )}

        {selected && (
          <section className="approvals-meta" aria-label="Metadata">
            <h2>Metadata</h2>
            <dl className="approvals-facts">
              <dt>Platform</dt>
              <dd>{selected.platform}</dd>
              <dt>Status</dt>
              <dd>{selected.status}</dd>
              <dt>Privacy</dt>
              <dd>{text(selected.payload.privacy)}</dd>
              <dt>Paid promotion</dt>
              <dd>{selected.payload.paid_promotion ? "Yes" : "No"}</dd>
              <dt>Payload hash</dt>
              <dd className="approvals-hash">{selected.payload_hash.slice(0, 12)}</dd>
            </dl>
            <table className="diff-table">
              <caption>Changes since the last published version</caption>
              <thead>
                <tr>
                  <th scope="col">Field</th>
                  <th scope="col">Change</th>
                  <th scope="col">Before</th>
                  <th scope="col">After</th>
                </tr>
              </thead>
              <tbody>
                {selected.diff.length === 0 ? (
                  <tr>
                    <td colSpan={4}>No changes</td>
                  </tr>
                ) : (
                  selected.diff.map((d) => (
                    <tr key={d.field} className={`diff-row diff-${change(d).toLowerCase()}`}>
                      <th scope="row">{d.field}</th>
                      <td>{change(d)}</td>
                      <td>{text(d.before)}</td>
                      <td>{text(d.after)}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
            {groupOf(selected) === "waiting" && (
              <div className="approvals-actions">
                <button type="button" onClick={() => ask("approve", selected)} disabled={readOnly}>
                  {selected.publish_at ? "Approve schedule" : "Approve"}
                </button>
                <label>
                  Schedule for
                  <input
                    type="datetime-local"
                    value={scheduleAt}
                    onChange={(e) => setScheduleAt(e.target.value)}
                    disabled={readOnly}
                  />
                </label>
                <button
                  type="button"
                  onClick={() => ask("schedule", selected)}
                  disabled={readOnly || !scheduleAt}
                >
                  Schedule
                </button>
              </div>
            )}
          </section>
        )}
      </div>

      <dialog
        ref={dialogRef}
        className="confirm-dialog"
        aria-labelledby="confirm-title"
        onCancel={(e) => {
          e.preventDefault();
          cancel();
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.preventDefault();
            cancel();
          }
        }}
      >
        {pending && (
          <form onSubmit={(e) => void confirm(e)}>
            <h2 id="confirm-title">
              {pending.at ? "Schedule" : "Publish"} on {pending.publication.platform === "youtube" ? "YouTube" : pending.publication.platform}?
            </h2>
            <p>
              “{text(pending.publication.payload.title)}” will be{" "}
              {pending.at ? `scheduled for ${when(pending.at)}` : "published now"}.
            </p>
            <label>
              Approved by
              <input
                value={approver}
                onChange={(e) => setApprover(e.target.value)}
                required
                maxLength={64}
                autoFocus={!approver.trim()}
              />
            </label>
            <div className="confirm-actions">
              <button type="button" onClick={cancel}>
                Cancel
              </button>
              <button type="submit" autoFocus={!!approver.trim()} disabled={!approver.trim()}>
                {pending.at ? "Confirm schedule" : "Confirm publish"}
              </button>
            </div>
          </form>
        )}
      </dialog>
    </main>
  );
}
