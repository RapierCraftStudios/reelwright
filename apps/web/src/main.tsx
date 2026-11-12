// SPDX-License-Identifier: AGPL-3.0-or-later
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";

import "./theme.css";
import { ProjectsRoute } from "./routes/projects";
import { ScriptEditorRoute } from "./routes/script-editor";

const root = document.getElementById("root");
if (!root) throw new Error("#root element missing");

createRoot(root).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<ProjectsRoute />} />
        <Route path="/projects/:id/script" element={<ScriptEditorRoute />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
