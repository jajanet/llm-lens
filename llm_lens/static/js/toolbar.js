// Persistent toolbar above the content area.
// The search input is kept in the DOM between renders so it keeps focus while typing.

import { state } from "./state.js";

const toolbarArea = document.getElementById("toolbar-area");
const searchBox = document.getElementById("search-box");
const toolbarExtra = document.getElementById("toolbar-extra");

let searchHandler = null;

searchBox.addEventListener("input", (e) => {
  if (searchHandler) searchHandler(e.target.value);
});

export function configureToolbar({ placeholder, extraHtml, onSearch, searchValue }) {
  toolbarArea.style.display = "block";
  searchBox.placeholder = placeholder || "Filter...";
  if (searchValue !== undefined && searchBox.value !== searchValue) {
    searchBox.value = searchValue;
  }
  toolbarExtra.innerHTML = extraHtml || "";
  searchHandler = onSearch || null;
}

export function hideToolbar() {
  toolbarArea.style.display = "none";
  searchHandler = null;
}

export function updateEditButton() {
  const btn = document.getElementById("edit-toggle");
  const editable = state.view === "messages" || state.view === "conversations" || state.view === "projects";
  btn.style.display = editable ? "" : "none";
  const refreshBtn = document.getElementById("refresh-toggle");
  if (refreshBtn) refreshBtn.style.display = state.view === "conversations" ? "" : "none";
  const statsBtn = document.getElementById("stats-toggle");
  if (statsBtn) {
    statsBtn.style.display = state.view === "messages" ? "" : "none";
  }
  const overviewStatsBtn = document.getElementById("overview-stats-toggle");
  if (overviewStatsBtn) {
    // The overview (and its Stats modal) now scopes automatically to the
    // current folder when inside a project, so the same button serves both
    // the global landing page and per-project conversations view.
    overviewStatsBtn.style.display =
      (state.view === "projects" || state.view === "conversations") ? "" : "none";
  }
  const projectStatsBtn = document.getElementById("project-stats-toggle");
  if (projectStatsBtn) projectStatsBtn.style.display = "none";
  document.body.dataset.view = state.view || "";
  if (!editable && state.editMode) {
    state.editMode = false;
    document.body.classList.remove("edit-mode");
    btn.textContent = "Edit";
    btn.classList.remove("active");
  }
}
