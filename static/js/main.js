// App entry point. Wires up routing, theme, and the delegated click handler.

import { state, setTheme } from "./state.js";
import { defineRoute, initRouter, navigate } from "./router.js";
import { updateEditButton } from "./toolbar.js";

import * as Projects from "./views/projects.js";
import * as Conversations from "./views/conversations.js";
import * as Messages from "./views/messages.js";

// --- Theme ---

function applyTheme() {
  document.body.classList.toggle("light", state.theme === "light");
  document.getElementById("theme-toggle").textContent = state.theme === "dark" ? "Light" : "Dark";
}
applyTheme();

// --- Edit mode (messages view only) ---

function toggleEditMode() {
  state.editMode = !state.editMode;
  document.body.classList.toggle("edit-mode", state.editMode);
  const btn = document.getElementById("edit-toggle");
  btn.textContent = state.editMode ? "Done" : "Edit";
  btn.classList.toggle("active", state.editMode);
  if (!state.editMode) state.msgSelected.clear();
  if (state.view === "messages") Messages.render();
}

// --- Routes ---

defineRoute(/^\/?$/, async () => {
  await Projects.show();
  updateEditButton();
});

defineRoute(/^\/p\/([^/]+)$/, async (folder) => {
  await Conversations.show(decodeURIComponent(folder));
  updateEditButton();
});

defineRoute(/^\/p\/([^/]+)\/c\/([^/]+)$/, async (folder, convoId) => {
  await Messages.show(decodeURIComponent(folder), decodeURIComponent(convoId));
  updateEditButton();
});

// --- Delegated click handler ---
// All interactive elements are marked with data-action="...".
// This keeps view HTML free of inline handlers and avoids global functions.

const actions = {
  // Navigation
  "nav-projects":     () => navigate("/"),
  "nav-folder":       (_e, el) => navigate(`/p/${encodeURIComponent(el.dataset.folder)}`),
  "open-project":     (_e, el) => Projects.openProject(el.dataset.folder, el.dataset.path),
  "open-convo":       (_e, el) => Conversations.openConvo(el.dataset.id),

  // Header buttons
  "toggle-theme":     () => { setTheme(state.theme === "dark" ? "light" : "dark"); applyTheme(); },
  "toggle-edit":      () => toggleEditMode(),

  // Projects view
  "sort-projects":    (_e, el) => Projects.sortBy(el.dataset.col),
  "delete-project":   (_e, el) => Projects.deleteProject(el.dataset.folder, el.dataset.name),

  // Conversations view
  "sort-convos":      (_e, el) => Conversations.sortBy(el.dataset.col),
  "toggle-convo-sel": (_e, el) => Conversations.toggleSel(el.dataset.id),
  "toggle-all-convos":(e)      => Conversations.toggleAll(e.target.checked),
  "delete-convo":     (_e, el) => Conversations.deleteConvo(el.dataset.id),
  "duplicate-convo":  (_e, el) => Conversations.duplicateConvo(el.dataset.id),
  "bulk-delete-convos":()      => Conversations.bulkDelete(),
  "load-more-convos": ()       => Conversations.loadMore(),

  // Messages view
  "load-earlier-msgs":()       => Messages.loadEarlier(),
  "toggle-side":      ()       => Messages.toggleSide(),
  "toggle-msg-sel":   (_e, el) => Messages.toggleMsgSel(el.dataset.uuid),
  "copy-msg":         (_e, el) => Messages.copyMsg(el.dataset.uuid),
  "delete-msg":       (_e, el) => Messages.deleteMsg(el.dataset.uuid),
  "copy-selected":    ()       => Messages.copySelected(),
  "save-selected":    ()       => Messages.saveSelected(),
  "delete-selected":  ()       => Messages.deleteSelected(),
  "clear-selection":  ()       => Messages.clearSelection(),
  "toggle-thinking":  (_e, el) => {
    const t = document.getElementById(el.dataset.target);
    if (t) t.style.display = t.style.display === "none" ? "block" : "none";
  },

  // View mode — delegates to the current view
  "set-view-mode":    (_e, el) => {
    const mode = el.dataset.mode;
    if (state.view === "projects") Projects.setMode(mode);
    else if (state.view === "conversations") Conversations.setMode(mode);
  },
};

document.body.addEventListener("click", (e) => {
  // closest() finds the nearest [data-action] ancestor-or-self, so clicks on
  // inner buttons never fall through to their enclosing row/card action.
  const actionEl = e.target.closest("[data-action]");
  if (!actionEl) return;
  const handler = actions[actionEl.dataset.action];
  if (handler) handler(e, actionEl);
});

initRouter();
