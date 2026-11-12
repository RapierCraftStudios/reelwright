// SPDX-License-Identifier: AGPL-3.0-or-later
import { type FormEvent, useCallback, useEffect, useState } from "react";

import {
  createProject,
  listProjects,
  PROJECT_FORMATS,
  type Project,
  type ProjectFormat,
} from "../api";

export function ProjectsRoute() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [title, setTitle] = useState("");
  const [format, setFormat] = useState<ProjectFormat>("long");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setProjects(await listProjects());
    } catch (e) {
      setError(`Could not load projects: ${String(e)}`);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!title.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await createProject({ title: title.trim(), format });
      setTitle("");
      await refresh();
    } catch (e) {
      setError(`Could not create project: ${String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main>
      <h1>Projects</h1>
      <form onSubmit={onSubmit} aria-label="New project">
        <label>
          Title
          <input value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={200} />
        </label>
        <label>
          Format
          <select value={format} onChange={(e) => setFormat(e.target.value as ProjectFormat)}>
            {PROJECT_FORMATS.map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" disabled={busy}>
          Create project
        </button>
      </form>
      {error && <p role="alert">{error}</p>}
      {projects.length === 0 ? (
        <p>No projects yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">Title</th>
              <th scope="col">Format</th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {projects.map((p) => (
              <tr key={p.id} data-testid="project-row">
                <td>{p.title}</td>
                <td>{p.format}</td>
                <td>{p.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  );
}
