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
