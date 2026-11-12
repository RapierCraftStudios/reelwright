// SPDX-License-Identifier: AGPL-3.0-or-later
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ParseResult, Script, ScriptDoc, ScriptError } from "../api";
import { EMPTY_MESSAGE, ScriptEditorRoute } from "./script-editor";

const SOURCE = `---
format: long
working_title: Test
---

## beat: hook | intent: open loop
Hello there. [broll: desk]
[TODO: punchline]
`;

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
        {
          kind: "spoken",
          text: "Hello there.",
          line_no: 7,
          adlibs: [],
          cues: [
            { type: "broll", payload: "desk", standalone: false, at: 13, line: 7, col: 14, end_col: 27 },
          ],
        },
      ],
    },
  ],
  todos: [],
};

function doc(overrides: Partial<ScriptDoc> = {}, script: Script = SCRIPT): ScriptDoc {
  return {
    version: {
      id: 1,
      project_id: 1,
      version: 1,
      source: SOURCE,
      locked_at: null,
      created_at: "2026-11-12T00:00:00Z",
    },
    locked: false,
    script,
    errors: [],
    ...overrides,
  };
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;

function stubFetch(handler: Handler) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => handler(url, init));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** GET returns `getDoc`, /parse echoes `parse`, everything else is decided by `rest`. */
function api(getDoc: ScriptDoc | null, parse: ParseResult, rest?: Handler): Handler {
  return (url, init) => {
    const method = init?.method ?? "GET";
    if (url.endsWith("/parse")) return json(parse);
    if (method === "GET") return getDoc ? json(getDoc) : json({ detail: "no script yet" }, 404);
    return rest ? rest(url, init) : json({ detail: "unexpected" }, 500);
  };
}

function renderEditor() {
  return render(
    <MemoryRouter initialEntries={["/projects/1/script"]}>
      <Routes>
        <Route path="/projects/:id/script" element={<ScriptEditorRoute />} />
      </Routes>
    </MemoryRouter>,
  );
}

function setOnline(value: boolean) {
  Object.defineProperty(window.navigator, "onLine", { configurable: true, get: () => value });
}

beforeEach(() => {
  localStorage.clear();
  setOnline(true);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("ScriptEditorRoute", () => {
  it("shows the empty state with both actions", async () => {
    stubFetch(api(null, { script: SCRIPT, errors: [] }));
    renderEditor();
    expect(await screen.findByText(EMPTY_MESSAGE)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Write a script" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Generate from a topic" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Write a script" }));
    expect((screen.getByLabelText("Script") as HTMLTextAreaElement).value).toContain("## beat: hook");
  });

  it("shows the editor skeleton while loading", () => {
    stubFetch(() => new Promise<Response>(() => {}));
    renderEditor();
    expect(screen.getByTestId("editor-skeleton")).toBeTruthy();
  });

  it("highlights beats and cues with icon and type label, not colour only", async () => {
    stubFetch(api(doc(), { script: SCRIPT, errors: [] }));
    renderEditor();
    const layer = await screen.findByTestId("highlight");
    const beat = layer.querySelector('[data-line="6"] .hl-beat');
    expect(beat?.textContent).toContain("## beat: hook");
    expect(layer.querySelector('[data-line="6"]')?.textContent).toContain("¶ beat");
    const cueLine = layer.querySelector('[data-line="7"]');
    expect(cueLine?.querySelector(".hl-cue.cue-broll")?.textContent).toBe("[broll: desk]");
    expect(cueLine?.textContent).toContain("🎞 broll");
  });

  it("renders a parse error inline on its own line", async () => {
    const error: ScriptError = { line: 7, col: 14, message: "unknown cue type 'zoom'" };
    const bad = SOURCE.replace("[broll: desk]", "[zoom: x]");
    stubFetch(api(doc({ errors: [error], version: { ...doc().version, source: bad } }), { script: SCRIPT, errors: [error] }));
    renderEditor();
    const inline = await screen.findByTestId("inline-error");
    expect(inline.closest("[data-line]")?.getAttribute("data-line")).toBe("7");
    expect(inline.textContent).toContain("unknown cue type 'zoom'");
    expect(screen.getByText(/Line 7, col 14: unknown cue type 'zoom'/)).toBeTruthy();
  });

  it("shows a save failure banner and Retry re-PUTs", async () => {
    let puts = 0;
    const fetchMock = stubFetch(
      api(doc(), { script: SCRIPT, errors: [] }, () => {
        puts += 1;
        return puts === 1 ? json({ detail: "disk full" }, 500) : json(doc({ version: { ...doc().version, version: 2 } }), 201);
      }),
    );
    renderEditor();
    const area = (await screen.findByLabelText("Script")) as HTMLTextAreaElement;
    fireEvent.change(area, { target: { value: `${SOURCE}More words.\n` } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Save failed: disk full");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Saved version 2")).toBeTruthy();
    const putCalls = fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT");
    expect(putCalls).toHaveLength(2);
    expect(JSON.parse(String(putCalls[1][1]?.body))).toEqual({ source: `${SOURCE}More words.\n` });
  });

  it("saves with Ctrl+S", async () => {
    const fetchMock = stubFetch(api(doc(), { script: SCRIPT, errors: [] }, () => json(doc(), 201)));
    renderEditor();
    const area = (await screen.findByLabelText("Script")) as HTMLTextAreaElement;
    fireEvent.change(area, { target: { value: `${SOURCE}Extra.\n` } });
    fireEvent.keyDown(area, { key: "s", ctrlKey: true });
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true));
  });

  it("offline shows the cached copy read-only with an Offline banner", async () => {
    localStorage.setItem("rw.script.1", JSON.stringify({ source: SOURCE, version: 3, locked: false }));
    setOnline(false);
    const fetchMock = stubFetch(() => json({}));
    renderEditor();
    expect(await screen.findByText(/Offline — showing the last saved copy, read-only/)).toBeTruthy();
    const area = screen.getByLabelText("Script") as HTMLTextAreaElement;
    expect(area.value).toBe(SOURCE);
    expect(area.readOnly).toBe(true);
    expect(screen.getByTestId("script-state").textContent).toContain("Version 3");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("a network failure falls back to the cached copy", async () => {
    localStorage.setItem("rw.script.1", JSON.stringify({ source: SOURCE, version: 2, locked: false }));
    stubFetch(() => Promise.reject(new TypeError("Failed to fetch")));
    renderEditor();
    expect(await screen.findByText(/Offline — showing the last saved copy/)).toBeTruthy();
    expect((screen.getByLabelText("Script") as HTMLTextAreaElement).readOnly).toBe(true);
  });

  it("disables Lock while a TODO remains and enables it when clean", async () => {
    const withTodo: Script = { ...SCRIPT, todos: [{ text: "[TODO: punchline]", line: 8, col: 1 }] };
    stubFetch(api(doc({}, withTodo), { script: withTodo, errors: [] }));
    renderEditor();
    const lock = await screen.findByRole("button", { name: "Lock" });
    expect((lock as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("script-state").textContent).toContain("1 TODO left");
    cleanup();

    stubFetch(api(doc(), { script: SCRIPT, errors: [] }, () => json(doc({ locked: true }))));
    renderEditor();
    const cleanLock = await screen.findByRole("button", { name: "Lock" });
    expect((cleanLock as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(cleanLock);
    expect(await screen.findByText("Locked version 1")).toBeTruthy();
    expect((screen.getByLabelText("Script") as HTMLTextAreaElement).readOnly).toBe(true);
  });

  it("outline items toggle from the keyboard and jump to the beat", async () => {
    stubFetch(api(doc(), { script: SCRIPT, errors: [] }));
    renderEditor();
    const outline = await screen.findByRole("navigation", { name: "Beat outline" });
    const toggle = await waitFor(() => {
      const button = outline.querySelector<HTMLButtonElement>(".outline-toggle");
      if (!button) throw new Error("no outline item");
      return button;
    });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    toggle.focus();
    expect(document.activeElement).toBe(toggle);
    // Native <button>: Enter/Space activate it as a click.
    act(() => toggle.click());
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Go to line 6" }));
    const area = screen.getByLabelText("Script") as HTMLTextAreaElement;
    expect(document.activeElement).toBe(area);
    expect(area.value.slice(0, area.selectionStart).split("\n").length).toBe(6);
  });
});
