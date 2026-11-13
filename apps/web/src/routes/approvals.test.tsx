// SPDX-License-Identifier: AGPL-3.0-or-later
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Publication } from "../api";
import { ApprovalsRoute } from "./approvals";

function publication(over: Partial<Publication> = {}): Publication {
  return {
    id: 1,
    project_id: 1,
    render_id: 1,
    platform: "youtube",
    status: "draft",
    payload: { title: "My video", description: "Body\n\nChapters\n0:00 Intro", privacy: "public", paid_promotion: false },
    payload_hash: "a".repeat(64),
    remote_id: null,
    publish_at: null,
    error: null,
    created_at: "2026-11-12T00:00:00Z",
    updated_at: "2026-11-12T00:00:00Z",
    approvals: [],
    diff: [
      { field: "title", before: null, after: "My video" },
      { field: "tags", before: ["a"], after: ["a", "b"] },
    ],
    preview_url: "/api/publish/1/preview",
    ...over,
  };
}

type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;

function stubFetch(handler: Handler) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => handler(url, init));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const ok = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

function calls(fetchMock: ReturnType<typeof stubFetch>) {
  return fetchMock.mock.calls.map(([url, init]) => `${init?.method ?? "GET"} ${url}`);
}

function setOnline(value: boolean) {
  Object.defineProperty(window.navigator, "onLine", { configurable: true, get: () => value });
}

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem("reelwright.approver", "yash");
  setOnline(true);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("ApprovalsRoute", () => {
  it("shows the empty state when nothing is waiting", async () => {
    stubFetch(() => ok([]));
    render(<ApprovalsRoute />);
    expect(await screen.findByText("Nothing waiting for approval")).toBeTruthy();
  });

  it("shows a loading skeleton while the list loads", () => {
    stubFetch(() => new Promise<Response>(() => {}));
    render(<ApprovalsRoute />);
    expect(screen.getByTestId("approvals-skeleton")).toBeTruthy();
    expect(screen.getByRole("region", { name: "Publications" }).getAttribute("aria-busy")).toBe("true");
  });

  it("renders the preview and the metadata diff with text labels", async () => {
    stubFetch(() => ok([publication()]));
    render(<ApprovalsRoute />);
    const meta = await screen.findByRole("region", { name: "Metadata" });
    const rows = within(meta).getAllByRole("row");
    expect(rows.map((r) => r.textContent)).toEqual([
      "FieldChangeBeforeAfter",
      "titleAdded—My video",
      "tagsChangeda" + "a, b",
    ]);
    const preview = screen.getByRole("region", { name: "Preview" });
    expect(preview.querySelector("video")?.getAttribute("src")).toBe("/api/publish/1/preview");
    expect(within(preview).getByRole("heading", { name: "My video" })).toBeTruthy();
  });

  it("only approves after the confirm dialog, then moves the card to Live", async () => {
    const live = publication({ status: "live", remote_id: "mock-1", diff: [] });
    const fetchMock = stubFetch((url) => (url.endsWith("/approve") ? ok(live) : ok([publication()])));
    render(<ApprovalsRoute />);
    fireEvent.click(await screen.findByRole("button", { name: "Approve" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("heading").textContent).toBe("Publish on YouTube?");
    expect(calls(fetchMock)).toEqual(["GET /api/publish"]); // nothing sent yet

    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(calls(fetchMock)).toEqual(["GET /api/publish"]);

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm publish" }));
    await waitFor(() => expect(calls(fetchMock)).toContain("POST /api/publish/1/approve"));
    const body = JSON.parse(String(fetchMock.mock.calls[1][1]?.body)) as Record<string, string>;
    expect(body).toEqual({ approved_by: "yash", payload_hash: "a".repeat(64) });
    const liveGroup = screen.getByRole("region", { name: /^Live/ });
    const card = await within(liveGroup).findByTestId("publication-1");
    expect(card.className).toContain("card-moving");
    expect(card.textContent).toContain("mock-1");
    expect(screen.getByText("Nothing waiting for approval")).toBeTruthy();
  });

  it("schedules by PUTting publish_at first, then approving the returned hash", async () => {
    const rehashed = publication({ payload_hash: "b".repeat(64), publish_at: "2030-01-01T10:00:00Z" });
    const scheduled = { ...rehashed, status: "scheduled", remote_id: "mock-1" };
    const fetchMock = stubFetch((url, init) => {
      if (init?.method === "PUT") return ok(rehashed);
      if (url.endsWith("/approve")) return ok(scheduled);
      return ok([publication()]);
    });
    render(<ApprovalsRoute />);
    const input = (await screen.findByLabelText("Schedule for")) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "2030-01-01T10:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Schedule" }));
    expect(within(screen.getByRole("dialog")).getByRole("heading").textContent).toBe("Schedule on YouTube?");
    fireEvent.click(screen.getByRole("button", { name: "Confirm schedule" }));
    await waitFor(() => expect(calls(fetchMock)).toContain("POST /api/publish/1/approve"));
    expect(calls(fetchMock).slice(1)).toEqual(["PUT /api/publish/1", "POST /api/publish/1/approve"]);
    const put = JSON.parse(String(fetchMock.mock.calls[1][1]?.body)) as { publish_at: string };
    expect(put.publish_at).toBe(new Date("2030-01-01T10:00").toISOString());
    const approve = JSON.parse(String(fetchMock.mock.calls[2][1]?.body)) as { payload_hash: string };
    expect(approve.payload_hash).toBe("b".repeat(64));
    const group = screen.getByRole("region", { name: /^Scheduled/ });
    expect((await within(group).findByTestId("publication-1")).className).toContain("card-moving");
  });

  it("shows the platform error per item and retries it", async () => {
    const failed = publication({ status: "failed", error: "quotaExceeded: daily upload limit reached" });
    const live = publication({ status: "live", remote_id: "mock-1" });
    const fetchMock = stubFetch((url) => (url.endsWith("/retry") ? ok(live) : ok([failed])));
    render(<ApprovalsRoute />);
    const card = await screen.findByTestId("publication-1");
    expect(within(card).getByRole("alert").textContent).toContain("quotaExceeded: daily upload limit reached");
    fireEvent.click(within(card).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(calls(fetchMock)).toContain("POST /api/publish/1/retry"));
    const liveGroup = screen.getByRole("region", { name: /^Live/ });
    expect(await within(liveGroup).findByTestId("publication-1")).toBeTruthy();
  });

  it("goes read-only with a banner when offline and keeps the last list", async () => {
    window.localStorage.setItem("reelwright.approvals", JSON.stringify([publication()]));
    setOnline(false);
    stubFetch(() => Promise.reject(new TypeError("Failed to fetch")));
    render(<ApprovalsRoute />);
    expect(screen.getByRole("status").textContent).toBe("You are offline — approvals are read-only");
    expect(screen.getByTestId("publication-1")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Approve" }) as HTMLButtonElement).disabled).toBe(true);
    await screen.findByRole("alert"); // the failed refresh is reported, the cached list stays
    expect(screen.getByTestId("publication-1")).toBeTruthy();

    setOnline(true);
    act(() => {
      window.dispatchEvent(new Event("online"));
    });
    expect(screen.queryByText("You are offline — approvals are read-only")).toBeNull();
    expect((screen.getByRole("button", { name: "Approve" }) as HTMLButtonElement).disabled).toBe(false);
  });
});
