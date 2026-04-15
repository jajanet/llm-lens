// App entry point. Wires up routing, theme, and the delegated click handler.

import { state, setTheme, setFilter, setMode } from "./state.js";
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
function applyMode() {
  const sel = document.getElementById("mode-select");
  if (sel) sel.value = state.mode;
}
applyMode();


// --- Edit mode (messages view only) ---

function toggleEditMode() {
  state.editMode = !state.editMode;
  document.body.classList.toggle("edit-mode", state.editMode);
  const btn = document.getElementById("edit-toggle");
  btn.textContent = state.editMode ? "Done" : "Edit";
  btn.classList.toggle("active", state.editMode);
  if (!state.editMode) {
    state.msgSelected.clear();
    state.selected.clear();
  }
  if (state.view === "messages") Messages.render();
  else if (state.view === "conversations") Conversations.render();
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
  "set-mode":         (e)  => {
    // Global Active/Archived switch. Invalidate project cache so the list
    // re-fetches (counts per-mode depend on fresh data), then re-render.
    setMode(e.target.value);
    if (state.projectsCache) state.projectsCache = null;
    if (state.view === "projects") Projects.show();
    else if (state.view === "conversations") Conversations.refreshAndRender();
    else if (state.view === "messages") navigate(`/p/${encodeURIComponent(state.folder)}`);
  },

  // Projects view
  "sort-projects":    (_e, el) => Projects.sortBy(el.dataset.col),
  "delete-project":   (_e, el) => Projects.deleteProject(el.dataset.folder, el.dataset.name),
  "set-overview-range":(e)     => Projects.setOverviewRange(e.target.value),
  "overview-nav-prev":()       => Projects.navOverview(-1),
  "overview-nav-next":()       => Projects.navOverview(+1),
  "set-overview-mode":(e)      => Projects.setOverviewMode(e.target.value),
  "set-overview-size":(e)      => Projects.setOverviewSize(e.target.value),
  "set-overview-group":(e)     => Projects.setOverviewGroupBy(e.target.value),
  "open-overview-stats":()     => Projects.openOverviewStats(),
  "toggle-filter":    (e, el)  => {
    // One of three stat-inclusion toggles (active/archived/deleted). Refuse
    // to uncheck the last enabled one — with zero sources selected the graph
    // and stats are empty, so flip it back on immediately.
    if (!e.target.checked) {
      const on = ["active", "archived", "deleted"].filter((k) => state.filters[k]).length;
      if (on <= 1) { e.target.checked = true; return; }
    }
    setFilter(el.dataset.filter, e.target.checked);
    if (state.view === "projects") Projects.render();
    else if (state.view === "conversations") Conversations.render();
    else if (state.view === "messages") Messages.render();
  },

  // Conversations view
  "sort-convos":      (_e, el) => Conversations.sortBy(el.dataset.col),
  "toggle-convo-sel": (_e, el) => Conversations.toggleSel(el.dataset.id),
  "toggle-all-convos":(e)      => Conversations.toggleAll(e.target.checked),
  "delete-convo":     (_e, el) => Conversations.deleteConvo(el.dataset.id),
  "duplicate-convo":  (_e, el) => Conversations.duplicateConvo(el.dataset.id),
  "archive-convo":    (_e, el) => Conversations.archiveConvo(el.dataset.id),
  "unarchive-convo":  (_e, el) => Conversations.unarchiveConvo(el.dataset.id),
  "bulk-delete-convos":()      => Conversations.bulkDelete(),
  "bulk-archive-convos":()     => Conversations.bulkArchive(),
  "bulk-unarchive-convos":()   => Conversations.bulkUnarchive(),
  "load-more-convos": ()       => Conversations.loadMore(),
  "refresh-convos":   ()       => Conversations.refreshCache(),
  "open-project-stats":()      => Conversations.openProjectStats(),
  "copy-resume":      (_e, el) => Conversations.copyResume(el.dataset.id),

  // Messages view
  "load-earlier-msgs":()       => Messages.loadEarlier(),
  "open-stats-modal": ()       => {
    if (state.view === "conversations") Conversations.showStats();
    else Messages.showStats();
  },
  "toggle-tool-group":(_e, el) => Messages.toggleToolGroup(el.dataset.groupId),
  "toggle-side":      ()       => Messages.toggleSide(),

  "toggle-whitespace": ()      => Messages.toggleWhitespace(),
  "toggle-msg-sel":   (_e, el) => Messages.toggleMsgSel(el.dataset.uuid),
  "copy-msg":         (_e, el) => Messages.copyMsg(el.dataset.uuid),
  "delete-msg":       (_e, el) => Messages.deleteMsg(el.dataset.uuid),

  "transform-msg":    (_e, el) => Messages.transformMsg(el.dataset.uuid, el.dataset.kind || "scrub"),
  "open-transform-menu": (_e, el) => Messages.openTransformMenu(el.dataset.uuid, el),
  "bulk-transform":   (_e, el) => Messages.bulkTransform(el.dataset.kind || "scrub"),
  "open-bulk-transform-menu": (_e, el) => Messages.openBulkTransformMenu(el),
  "toggle-all-msgs":  (_e, el) => Messages.toggleAllMsgs(),

  "open-word-lists":  ()       => Messages.openWordListsModal(),
  "copy-selected":    ()       => Messages.copySelected(),
  "save-selected":    ()       => Messages.saveSelected(),
  "delete-selected":  ()       => Messages.deleteSelected(),
  "clear-selection":  ()       => Messages.clearSelection(),
  "set-stats-view":   (_e, el) => {
    const modal = el.closest(".modal");
    if (!modal) return;
    const v = el.value;
    modal.querySelectorAll(".stats-view").forEach((n) => {
      if (n.classList.contains(`stats-view-${v}`)) n.removeAttribute("hidden");
      else n.setAttribute("hidden", "");
    });
  },
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

// <select> and radio/checkbox inputs fire `change`, not `click` — delegate
// both so the actions map stays the single source of truth.
document.body.addEventListener("change", (e) => {
  const el = e.target;
  if (!el.matches || !el.matches("select[data-action], input[data-action]")) return;
  const handler = actions[el.dataset.action];
  if (handler) handler(e, el);
});

// Instant tooltip for the overview chart. SVG <title> has a browser-imposed
// ~500ms delay; this mimics the behavior via a div shown on mouseover.
document.body.addEventListener("mouseover", (e) => {
  const group = e.target.closest && e.target.closest(".ov-bar-group");
  if (!group) return;
  const graph = group.closest(".overview-graph");
  if (!graph) return;
  const tip = graph.querySelector(":scope > .ov-tip");
  if (!tip) return;
  tip.innerHTML = group.dataset.tip || "";
  tip.style.display = "block";
});
document.body.addEventListener("mousemove", (e) => {
  const group = e.target.closest && e.target.closest(".ov-bar-group");
  if (!group) return;
  const graph = group.closest(".overview-graph");
  if (!graph) return;
  const tip = graph.querySelector(":scope > .ov-tip");
  if (!tip || tip.style.display === "none") return;
  const rect = graph.getBoundingClientRect();
  const tipW = tip.offsetWidth || 180;
  let x = e.clientX - rect.left + 14;
  // Flip to the left side of the cursor if we'd overflow the graph's right edge.
  if (x + tipW > rect.width - 4) x = e.clientX - rect.left - tipW - 14;
  const y = e.clientY - rect.top + 14;
  tip.style.left = Math.max(4, x) + "px";
  tip.style.top = y + "px";
});
document.body.addEventListener("mouseout", (e) => {
  const group = e.target.closest && e.target.closest(".ov-bar-group");
  if (!group) return;
  // Check if we're moving to another bar-group — if so, keep tip visible;
  // the mouseover handler will repopulate.
  const to = e.relatedTarget;
  if (to && to.closest && to.closest(".ov-bar-group")) return;
  const graph = group.closest(".overview-graph");
  const tip = graph && graph.querySelector(":scope > .ov-tip");
  if (tip) tip.style.display = "none";
});

document.body.addEventListener("click", (e) => {
  // closest() finds the nearest [data-action] ancestor-or-self, so clicks on
  // inner buttons never fall through to their enclosing row/card action.
  const actionEl = e.target.closest("[data-action]");
  if (!actionEl) return;
  const handler = actions[actionEl.dataset.action];
  if (handler) handler(e, actionEl);
});

initRouter();
