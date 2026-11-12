// SPDX-License-Identifier: AGPL-3.0-or-later
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Project } from "../api";
import { ProjectsRoute } from "./projects";

function project(id: number, title: string): Project {
  return {
    id,
    slug: title.toLowerCase().replace(/\s+/g, "-"),
    title,
    format: "long",
    status: "draft",
    target_publish_date: null,
    created_at: "2026-11-12T00:00:00Z",
  };
}

function stubApi(initial: Project[]) {
  const rows = [...initial];
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as { title: string };
      const created = project(rows.length + 1, body.title);
      rows.unshift(created);
      return new Response(JSON.stringify(created), { status: 201 });
    }
    return new Response(JSON.stringify(rows), { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ProjectsRoute", () => {
  it("lists existing projects", async () => {
    stubApi([project(1, "Existing One")]);
    render(<ProjectsRoute />);
    expect(await screen.findByText("Existing One")).toBeTruthy();
  });

  it("creates a project and shows it", async () => {
    const fetchMock = stubApi([]);
    render(<ProjectsRoute />);
    expect(await screen.findByText("No projects yet.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Brand New" } });
    fireEvent.change(screen.getByLabelText("Format"), { target: { value: "vlog" } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByText("Brand New")).toBeTruthy();
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ title: "Brand New", format: "vlog" });
  });
});
