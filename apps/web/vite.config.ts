// SPDX-License-Identifier: AGPL-3.0-or-later
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const api = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  // ws: true carries the voice-follow WebSocket (/api/prompter/:id/align) under pnpm dev.
  server: { proxy: { "/api": { target: api, ws: true }, "/healthz": api } },
  test: { environment: "jsdom", globals: true },
});
