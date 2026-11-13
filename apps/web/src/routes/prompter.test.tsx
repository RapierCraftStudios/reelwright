// SPDX-License-Identifier: AGPL-3.0-or-later
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Script, ScriptDoc } from "../api";
import { PENDING_KEY } from "../prompter/marks";
import { PROMPTER_EMPTY_MESSAGE, LOST_MESSAGE, PrompterRoute } from "./prompter";

const voice = vi.hoisted(() => ({
  instances: [] as { options: { onPosition(lineNo: number): void; onError(code: string): void }; start: unknown; stop: unknown; seek: unknown }[],
}));

vi.mock("../prompter/voiceFollow", () => ({
  VoiceFollow: vi.fn().mockImplementation(function (this: Record<string, unknown>, options: unknown) {
    Object.assign(this, {
      options,
      start: vi.fn(async () => {}),
      stop: vi.fn(),
      seek: vi.fn(),
      p95Ms: () => 12,
    });
    voice.instances.push(this as never);
  }),
}));

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

function renderPrompter() {
  return render(
    <MemoryRouter initialEntries={["/prompter/1"]}>
      <Routes>
        <Route path="/prompter/:id" element={<PrompterRoute />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  localStorage.clear();
  voice.instances.length = 0;
});

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

  it("disables the voice follow toggle without a script", async () => {
    stubFetch(() => json({ detail: "no script yet" }, 404));
    renderPrompter();
    await screen.findByText(PROMPTER_EMPTY_MESSAGE);
    const toggle = screen.getByRole("button", { name: "Voice follow" });
    expect(toggle.hasAttribute("disabled")).toBe(true);
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
  });

  it("keeps the active line when voice follow is switched on and off", async () => {
    stubFetch(() => json(doc()));
    renderPrompter();
    const text = await screen.findByTestId("prompter-text");
    const active = () => text.querySelector("[data-active]")?.getAttribute("data-line");
    const toggle = screen.getByRole("button", { name: "Voice follow" });
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-pressed")).toBe("true");
    expect(voice.instances).toHaveLength(1);
    const follow = voice.instances[0];
    expect(follow.start).toHaveBeenCalledWith({ scriptVersion: 2, lineNo: 8 });
    act(() => follow.options.onPosition(12));
    expect(active()).toBe("12");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    expect(follow.stop).toHaveBeenCalled();
    expect(active()).toBe("12");
    // Back on: listening resumes from the current line, not the top.
    fireEvent.click(toggle);
    expect(voice.instances[1].start).toHaveBeenCalledWith({ scriptVersion: 2, lineNo: 12 });
    fireEvent.keyDown(window, { key: "ArrowUp" });
    expect(voice.instances[1].seek).toHaveBeenCalledWith(8);
  });

  it("falls back to manual with a notice when voice follow fails", async () => {
    stubFetch(() => json(doc()));
    renderPrompter();
    await screen.findByTestId("prompter-text");
    const toggle = screen.getByRole("button", { name: "Voice follow" });
    fireEvent.click(toggle);
    act(() => voice.instances[0].options.onError("mic_denied"));
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByTestId("prompter-status").textContent).toMatch(/Microphone blocked/);
  });
});
