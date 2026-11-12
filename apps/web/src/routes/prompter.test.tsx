// SPDX-License-Identifier: AGPL-3.0-or-later
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Script, ScriptDoc } from "../api";
import { MockDjiCamera } from "../prompter/camera/__tests__/mockDji";
import { UNSUPPORTED_MESSAGE } from "../prompter/camera/CameraPanel";
import { CameraController } from "../prompter/camera/djiCommands";
import { PENDING_KEY } from "../prompter/marks";
import { PROMPTER_EMPTY_MESSAGE, LOST_MESSAGE, PrompterRoute } from "./prompter";

const SCRIPT: Script = {
  front_matter: { format: "long", working_title: "Test" },
  beats: [
    {
      slug: "hook",
      type: "hook",
      intent: "open loop",
      index: 0,
      line: 6,
      lines: [
        { kind: "cue", text: "", line_no: 7, adlibs: [], cues: [] },
        {
          kind: "spoken",
          text: "Hello there.",
          line_no: 8,
          adlibs: [],
          cues: [{ type: "broll", payload: "desk", standalone: false, at: 13, line: 8, col: 14, end_col: 27 }],
        },
        { kind: "todo", text: "TODO: punchline", line_no: 9, adlibs: [], cues: [] },
      ],
    },
    {
      slug: "cta",
      type: "cta",
      intent: "subscribe",
      index: 1,
      line: 11,
      lines: [{ kind: "spoken", text: "See you next time.", line_no: 12, adlibs: [], cues: [] }],
    },
  ],
  todos: [],
};

function doc(locked = true): ScriptDoc {
  return {
    version: {
      id: 3,
      project_id: 1,
      version: 2,
      source: "## beat: hook\n[music: low pulse]\nHello there. [broll: desk]\n",
      locked_at: locked ? "2026-11-12T00:00:00Z" : null,
      created_at: "2026-11-12T00:00:00Z",
    },
    locked,
    script: SCRIPT,
    errors: [],
  };
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;

function stubFetch(handler: Handler) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => handler(url, init));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderPrompter(camera?: CameraController) {
  return render(
    <MemoryRouter initialEntries={["/prompter/1"]}>
      <Routes>
        <Route path="/prompter/:id" element={<PrompterRoute camera={camera} />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => localStorage.clear());

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("PrompterRoute", () => {
  it("renders only spoken lines, with cue text and cue-only/todo lines hidden", async () => {
    stubFetch(() => json(doc()));
    renderPrompter();
    const text = await screen.findByTestId("prompter-text");
    expect(text.textContent).toContain("Hello there.");
    expect(text.textContent).toContain("See you next time.");
    expect(text.textContent).not.toMatch(/broll|desk|music|TODO|\[/);
    expect([...text.querySelectorAll("[data-line]")].map((el) => el.getAttribute("data-line"))).toEqual(["8", "12"]);
    expect(text.querySelector('[data-line="12"]')?.getAttribute("data-beat")).toBe("cta");
  });

  it("shows the empty state when there is no script", async () => {
    stubFetch(() => json({ detail: "no script yet" }, 404));
    renderPrompter();
    expect(await screen.findByText(PROMPTER_EMPTY_MESSAGE)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open the script editor" }).getAttribute("href")).toBe("/projects/1/script");
  });

  it("shows the empty state when the script is not locked", async () => {
    stubFetch(() => json(doc(false)));
    renderPrompter();
    expect(await screen.findByText(PROMPTER_EMPTY_MESSAGE)).toBeTruthy();
    expect(screen.queryByTestId("prompter-text")).toBeNull();
  });

  it("shows a busy spinner while loading", () => {
    stubFetch(() => new Promise<Response>(() => {}));
    renderPrompter();
    expect(screen.getByTestId("prompter-loading").getAttribute("aria-busy")).toBe("true");
  });

  it("posts a retake mark with an ISO marked_at when 'r' is pressed", async () => {
    const posted: unknown[] = [];
    stubFetch((url, init) => {
      if (url.endsWith("/marks") && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        posted.push({ url, body });
        return json({ id: 1, project_id: 1, script_version_id: 3, created_at: "2026-11-12T00:00:00Z", ...body }, 201);
      }
      return json(doc());
    });
    renderPrompter();
    await screen.findByTestId("prompter-text");
    fireEvent.keyDown(window, { key: "r" });
    await waitFor(() => expect(posted).toHaveLength(1));
    const { url, body } = posted[0] as { url: string; body: Record<string, unknown> };
    expect(url).toBe("/api/prompter/1/marks");
    expect(body).toMatchObject({ kind: "retake", script_version: 2, beat_slug: "hook", line_no: 8, offset_ms: 0 });
    expect(new Date(String(body.marked_at)).toISOString()).toBe(body.marked_at);
    expect(await screen.findByText(/Retake marked at line 8/)).toBeTruthy();
    expect(localStorage.getItem(PENDING_KEY)).toBeNull();
  });

  it("shows the lost-connection banner and queues the mark when the post fails", async () => {
    stubFetch((url, init) => {
      if (url.endsWith("/marks") && init?.method === "POST") throw new TypeError("Failed to fetch");
      return json(doc());
    });
    renderPrompter();
    await screen.findByTestId("prompter-text");
    expect(screen.queryByText(LOST_MESSAGE)).toBeNull();
    fireEvent.keyDown(window, { key: "r" });
    expect(await screen.findByText(LOST_MESSAGE)).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain(LOST_MESSAGE);
    const queue = JSON.parse(localStorage.getItem(PENDING_KEY) ?? "[]") as { body: { line_no: number } }[];
    expect(queue.map((m) => m.body.line_no)).toEqual([8]);
    // The text keeps rendering while out of sync.
    expect(screen.getByTestId("prompter-text").textContent).toContain("Hello there.");
  });

  it("flushes a queued mark once the connection is back", async () => {
    let online = false;
    const posts: string[] = [];
    stubFetch((url, init) => {
      if (url.endsWith("/marks") && init?.method === "POST") {
        if (!online) throw new TypeError("Failed to fetch");
        posts.push(String(init.body));
        return json({ id: 1, project_id: 1, script_version_id: 3, created_at: "x", ...JSON.parse(String(init.body)) }, 201);
      }
      return json(doc());
    });
    renderPrompter();
    await screen.findByTestId("prompter-text");
    fireEvent.keyDown(window, { key: "Enter" });
    expect(await screen.findByText(LOST_MESSAGE)).toBeTruthy();
    online = true;
    window.dispatchEvent(new Event("online"));
    await waitFor(() => expect(posts).toHaveLength(1));
    await waitFor(() => expect(screen.queryByText(LOST_MESSAGE)).toBeNull());
    expect(localStorage.getItem(PENDING_KEY)).toBeNull();
  });

  it("renders the last cached script offline", async () => {
    stubFetch(() => json(doc()));
    renderPrompter();
    await screen.findByTestId("prompter-text");
    cleanup();
    stubFetch(() => {
      throw new TypeError("Failed to fetch");
    });
    renderPrompter();
    expect(await screen.findByText(LOST_MESSAGE)).toBeTruthy();
    expect(screen.getByTestId("prompter-text").textContent).toContain("See you next time.");
  });

  it("ignores keys typed into a settings field", async () => {
    const fetchMock = stubFetch(() => json(doc()));
    renderPrompter();
    await screen.findByTestId("prompter-text");
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    const field = screen.getByLabelText(/Font size/);
    fireEvent.keyDown(field, { key: "r" });
    await new Promise((r) => setTimeout(r, 20));
    expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith("/marks"))).toBe(false);
  });

  it("drives a paired camera: Go records, stop stops, and a take marker is posted", async () => {
    const link = new MockDjiCamera();
    const device = Object.assign(new EventTarget(), { id: "cam-1", name: "OsmoAction5Pro" });
    const camera = new CameraController({
      bluetooth: () => ({ requestDevice: async () => device }),
      requestDevice: async () => device,
      connectLink: async () => link,
      storage: null,
    });
    const fetchMock = stubFetch((url) => (url.endsWith("/takes") ? json({ id: 1 }, 201) : json(doc())));
    // jsdom lays nothing out, so a running scroll would reach the end on its first frame; hold it.
    vi.stubGlobal("requestAnimationFrame", () => 0);
    vi.stubGlobal("cancelAnimationFrame", () => undefined);
    renderPrompter(camera);
    await screen.findByText("Hello there.");

    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.click(screen.getByRole("button", { name: "Pair DJI camera" }));
    await waitFor(() => expect(screen.getByTestId("camera-status").textContent).toContain("Not recording"));

    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(link.recording).toBe(true));
    const records = () => link.written.filter((f) => f.cmdSet === 0x1d && f.cmdId === 0x03).map((f) => f.payload[4]);
    expect(records()).toEqual([0]);

    fireEvent.keyDown(window, { key: "s" });
    await waitFor(() => expect(link.recording).toBe(false));
    expect(records()).toEqual([0, 1]);
    await waitFor(() => expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith("/takes"))).toBe(true));
    const [url, init] = fetchMock.mock.calls.find(([u]) => String(u).endsWith("/takes"))!;
    expect(url).toBe("/api/projects/1/takes");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toMatchObject({
      script_version: 2,
      beat_slug: "hook",
      line_from: 8,
      line_to: 8,
      retake: false,
      continuous: false,
      camera: "OsmoAction5Pro",
    });
  });

  it("works unchanged without Web Bluetooth: no camera calls, no extra requests", async () => {
    const fetchMock = stubFetch(() => json(doc()));
    renderPrompter();
    await screen.findByText("Hello there.");
    expect(screen.getByTestId("camera-status").textContent).toContain(UNSUPPORTED_MESSAGE);
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    expect(screen.getByRole("button", { name: "Pause" })).toBeTruthy();
    fireEvent.keyDown(window, { key: "s" });
    expect(screen.getByRole("button", { name: "Start" })).toBeTruthy();
    await new Promise((r) => setTimeout(r, 20));
    expect(fetchMock.mock.calls.every(([u]) => String(u) === "/api/projects/1/script")).toBe(true);
  });
});
