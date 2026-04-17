// App entry point. Wires up routing, theme, and the delegated click handler.

import { state, setTheme, setFilter, setMode, setPreviewEnabled, persistSelections, restoreSelections } from "./state.js";
import { defineRoute, initRouter, navigate } from "./router.js";
import { api } from "./api.js";
import { updateEditButton } from "./toolbar.js";

import * as Projects from "./views/projects.js";
import * as Conversations from "./views/conversations.js";
import * as Messages from "./views/messages.js";
import * as Tags from "./tag_components.js";
import { convoScope, projectScope, scopeFor } from "./scopes.js";

// Bind each scope's onChange to its owning view's render. Has to
// happen after the view imports so the functions exist.
convoScope.onChange = () => {
  if (state.view === "conversations") Conversations.render();
  else if (state.view === "messages") Messages.renderBreadcrumb();
};
projectScope.onChange = () => {
  if (state.view === "projects") Projects.render();
  else if (state.view === "conversations") Conversations.renderBreadcrumb();
};

// --- Theme ---

function applyTheme() {
  document.body.classList.toggle("light", state.theme === "light");
  document.getElementById("theme-toggle").textContent = state.theme === "dark" ? "Light" : "Dark";
}
applyTheme();

// Rehydrate edit mode + selections from localStorage so a hard reload
// while editing doesn't wipe progress. Views' show() handlers decide
// whether to keep or discard the restored selection based on folder.
const _restored = restoreSelections();
if (_restored) {
  if (_restored.editMode) {
    state.editMode = true;
    document.body.classList.add("edit-mode");
  }
  if (Array.isArray(_restored.selected)) state.selected = new Set(_restored.selected);
  if (Array.isArray(_restored.projectSelected)) state.projectSelected = new Set(_restored.projectSelected);
  if (_restored.folder) state._restoredFolder = _restored.folder;
}

function applyMode() {
  const sel = document.getElementById("mode-select");
  if (sel) sel.value = state.mode;
}
applyMode();


// --- Edit mode (messages view only) ---

function toggleEditMode() {
  state.editMode = !state.editMode;
  document.body.classList.toggle("edit-mode", state.editMode);
  persistSelections();
  const btn = document.getElementById("edit-toggle");
  btn.textContent = state.editMode ? "Done" : "Edit";
  btn.classList.toggle("active", state.editMode);
  if (!state.editMode) {
    state.msgSelected.clear();
    state.selected.clear();
    if (state.projectSelected) state.projectSelected.clear();
  }
  // Refresh header-button visibility (download-raw-convo is gated on edit mode).
  updateEditButton();
  if (state.view === "messages") Messages.render();
  else if (state.view === "conversations") Conversations.render();
  else if (state.view === "projects") Projects.render();
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

defineRoute(/^\/p\/([^/]+)\/c\/([^/]+)\/a\/([^/]+)$/, async (folder, convoId, toolUseId) => {
  await Messages.showAgent(
    decodeURIComponent(folder),
    decodeURIComponent(convoId),
    decodeURIComponent(toolUseId),
  );
  updateEditButton();
});

// --- Delegated click handler ---
// All interactive elements are marked with data-action="...".
// This keeps view HTML free of inline handlers and avoids global functions.

const actions = {
  // Navigation
  "nav-projects":     () => navigate("/"),
  "nav-folder":       (_e, el) => navigate(`/p/${encodeURIComponent(el.dataset.folder)}`),
  "nav-convo":        (_e, el) => navigate(`/p/${encodeURIComponent(state.folder)}/c/${encodeURIComponent(state.convoId)}`),
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
  "toggle-project-sel": (e, el) => {
    e.stopPropagation();
    const f = el.dataset.folder;
    if (!state.projectSelected) state.projectSelected = new Set();
    if (state.projectSelected.has(f)) state.projectSelected.delete(f);
    else state.projectSelected.add(f);
    persistSelections();
    Projects.render();
  },
  "toggle-all-projects": (_e) => {
    // Partial → full, full → clear. Operates only on currently-visible
    // projects (what the user can actually see) so filters/search are
    // respected — otherwise a "select all" click would quietly also
    // grab projects the filter is hiding.
    if (!state.projectSelected) state.projectSelected = new Set();
    const visible = Array.from(
      document.querySelectorAll('[data-action="toggle-project-sel"]')
    ).map((el) => el.dataset.folder);
    const allSelected = visible.length > 0 && visible.every((f) => state.projectSelected.has(f));
    if (allSelected) for (const f of visible) state.projectSelected.delete(f);
    else for (const f of visible) state.projectSelected.add(f);
    persistSelections();
    Projects.render();
  },
  "clear-project-selection": (_e) => {
    if (state.projectSelected) state.projectSelected.clear();
    persistSelections();
    Projects.render();
  },
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
  "toggle-all-convos":(_e)     => Conversations.toggleAll(),
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
  "open-agent":       (_e, el) => navigate(`/p/${encodeURIComponent(state.folder)}/c/${encodeURIComponent(state.convoId)}/a/${encodeURIComponent(el.dataset.runId)}`),
  "open-agents-menu": (_e, el) => Messages.openAgentsMenu(el),
  "open-stats-modal": ()       => {
    if (state.view === "conversations") Conversations.showStats();
    else Messages.showStats();
  },
  "toggle-tool-group":(_e, el) => Messages.toggleToolGroup(el.dataset.groupId),
  "toggle-whitespace": ()      => Messages.toggleWhitespace(),
  "toggle-msg-sel":   (_e, el) => Messages.toggleMsgSel(el.dataset.uuid),
  "copy-msg":         (_e, el) => Messages.copyMsg(el.dataset.uuid),
  "delete-msg":       (_e, el) => Messages.deleteMsg(el.dataset.uuid),

  "transform-msg":    (_e, el) => Messages.transformMsg(el.dataset.uuid, el.dataset.kind || "scrub"),
  "open-transform-menu": (_e, el) => Messages.openTransformMenu(el.dataset.uuid, el),

  "edit-msg":         (_e, el) => Messages.editMsg(el.dataset.uuid),
  "save-edit-msg":    (_e, el) => Messages.saveEditMsg(el.dataset.uuid),
  "cancel-edit-msg":  (_e, el) => Messages.cancelEditMsg(el.dataset.uuid),
  "bulk-transform":   (_e, el) => Messages.bulkTransform(el.dataset.kind || "scrub"),
  "open-bulk-transform-menu": (_e, el) => Messages.openBulkTransformMenu(el),
  "toggle-all-msgs":  (_e, el) => Messages.toggleAllMsgs(),
  "open-select-scope-menu": (_e, el) => Messages.openSelectScopeMenu(el),
  "select-scope":     (_e, el) => Messages.selectByScope(el.dataset.scope),

  "open-word-lists":  ()       => Messages.openWordListsModal(),
  "toggle-preview":   (_e, el) => {
    // Flip the global preview setting in place. The menu stays open so the
    // user can still pick a transform; the item's label updates to reflect
    // the new state. Also exposed via the checkbox at the top of the
    // preview modal itself.
    const on = !state.previewEnabled;
    setPreviewEnabled(on);
    el.textContent = on ? "Turn off preview edits" : "Turn on preview edits";
  },

  "reveal-secret":    (_e, el) => Messages.revealSecret(el),

  "toggle-table-rows": (_e, el) => {
    const t = document.getElementById(el.dataset.target);
    if (!t) return;
    const expanded = t.classList.toggle("rows-expanded");
    el.textContent = expanded ? "show less" : (el.dataset.moreLabel || "show more");
  },

  "set-stats-tab":    (_e, el) => {
    const modal = el.closest(".modal");
    if (!modal) return;
    const t = el.dataset.tab;
    modal.querySelectorAll(".stats-tab").forEach((n) => {
      if (n.classList.contains(`stats-tab-${t}`)) n.removeAttribute("hidden");
      else n.setAttribute("hidden", "");
    });
    modal.querySelectorAll(".stats-tab-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === t);
    });
  },
  "copy-selected":    ()       => Messages.copySelected(),
  "copy-selected-jsonl":   ()       => Messages.copySelectedJsonl(),
  "download-selected-jsonl":()      => Messages.downloadSelectedJsonl(),
  "save-selected":    ()       => Messages.saveSelected(),
  "delete-selected":  ()       => Messages.deleteSelected(),
  "clear-selection":  ()       => Messages.clearSelection(),
  "open-export-menu": (_e, el) => Messages.openExportMenu(el),
  "open-jsonl-fields":()       => Messages.openJsonlFieldsModal(),
  "download-raw-convo":()      => Messages.downloadRawConvo(),
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
  "toggle-preview-position": () => Conversations.togglePreview(),

  // Tags (conversations view)
  "toggle-tag-filter":      (e, el) => { e.stopPropagation(); Tags.toggleTagFilter(scopeFor(el), parseInt(el.dataset.tag)); },
  "rename-tag-start":       (e, el) => { e.stopPropagation(); Tags.renameTag(scopeFor(el), parseInt(el.dataset.tag)); },
  "add-new-tag":            (e, el) => { e.stopPropagation(); Tags.addNewTag(scopeFor(el)); },
  "open-tag-assign-popup":  (e, el) => { e.stopPropagation(); Tags.openTagAssignPopup(scopeFor(el), el); },
  "apply-existing-tag":     (e, el) => { e.stopPropagation(); Tags.applyExistingTag(parseInt(el.dataset.tag)); },
  "create-and-assign-tag":  (e)     => { e.stopPropagation(); Tags.createAndAssignTag(); },

  // Smart-select
  "smart-select":      (e, el)  => { e.stopPropagation(); Conversations.toggleSmartSelect(el.dataset.preset); },
  "smart-threshold":   (e)      => Conversations.setSmartThreshold(parseFloat(e.target.value)),

  // Tags (messages view)
  "remove-convo-tag":  (e, el)  => { e.stopPropagation(); Messages.removeConvoTag(parseInt(el.dataset.tag)); },
  "open-tag-picker":   (e, el)  => { e.stopPropagation(); Messages.openTagPicker(el); },
  "pick-convo-tag":    (e, el)  => { e.stopPropagation(); Messages.pickConvoTag(parseInt(el.dataset.tag)); },
  "open-convo-tag-popup": (e, el) => {
    e.stopPropagation();
    if (!state.convoId) return;
    Tags.openTagAssignPopup(convoScope, el, {
      ids: [state.convoId],
      poolLabel: "this convo",
      onDone: () => Messages.renderBreadcrumb(),
    });
  },
  "open-project-tag-popup": (e, el) => {
    e.stopPropagation();
    if (!state.folder) return;
    Tags.openTagAssignPopup(projectScope, el, {
      ids: [state.folder],
      poolLabel: "this project",
      onDone: () => Conversations.renderBreadcrumb(),
    });
  },
  "remove-project-tag": (e, el) => {
    e.stopPropagation();
    if (!state.folder) return;
    Tags.toggleKeyTag(projectScope, state.folder, parseInt(el.dataset.tag));
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

// Range sliders fire `input` on every drag tick for real-time threshold
// updates (smart-select).
document.body.addEventListener("input", (e) => {
  const el = e.target;
  if (!el.matches || el.type !== "range" || !el.dataset.action) return;
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

document.body.addEventListener("click", (e) => {
  const code = e.target.closest(".stats-matrix code");
  if (!code || code.closest(".stats-note-row")) return;
  code.classList.toggle("expanded");
});
document.body.addEventListener("mouseover", (e) => {
  const code = e.target.closest && e.target.closest(".stats-matrix code");
  if (!code || code.closest(".stats-note-row")) return;
  if (!code.dataset.full && code.scrollWidth > code.clientWidth) {
    code.dataset.full = code.textContent;
  }
});

// Fetch the effective context-window plan once on boot. Sets state.planContextWindow
// which contextWindowFor uses as an override. Fire-and-forget; if this races with
// the first stats render, the next render will pick it up (re-renders are cheap).
api.contextWindow().then((r) => {
  if (r && typeof r.plan_window === "number") {
    state.planContextWindow = r.plan_window;
  }
}).catch(() => {});

initRouter();
