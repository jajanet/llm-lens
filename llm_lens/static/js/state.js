// Shared application state.

// Pure filter-init read from a storage-shaped object (getItem/setItem). Kept
// separate from `state` so tests can pass a mock storage without a browser.
// Invariant: at least one of active/archived/deleted must be on. The toggle
// handler guards runtime unchecks; this guards load-time so stale storage
// from before the guard can't leave everything off (empty graphs/stats with
// no recovery path).
export function computeInitialFilters(storage) {
  const f = {
    active:   storage.getItem("filter_active") !== "0",
    archived: storage.getItem("filter_archived") === "1",
    deleted: (storage.getItem("filter_deleted") === "1")
          || (storage.getItem("showDeleted") === "1"),
  };
  if (!f.active && !f.archived && !f.deleted) {
    f.active = true;
    storage.setItem("filter_active", "1");
  }
  return f;
}

export const state = {
  view: "projects",
  folder: null,
  path: null,
  convoId: null,

  viewMode: localStorage.getItem("viewMode") || "list",
  theme: localStorage.getItem("theme") || "dark",

  // Global mode — pivots the LIST between live convos and archived. Unrelated
  // to stats inclusion (those are separate toggles on `filters`).
  mode: localStorage.getItem("mode") || "active",

  sort: "recent",
  desc: true,

  search: "",
  msgSearch: "",

  selected: new Set(),
  msgSelected: new Set(),

  editMode: false,
  showSide: false,

  convoOffset: 0,
  convoTotal: 0,
  convoItems: [],
  msgOffset: 0,
  msgTotal: 0,
  msgData: null,

  projectsCache: null,

  // Stat-inclusion toggles. Independent of `state.mode`. All three can be
  // flipped on or off independently. They appear both in toolbars (inline
  // stats strip) and inside the stats modal so you can dial numbers without
  // closing the modal.
  filters: computeInitialFilters(localStorage),

  overviewRange: "day",
  overviewOffset: 0,
  overviewMode: "tools",
  overviewSize: "compact",
  overviewGroupBy: "none",
  overviewScope: null,   // null = global; or project folder id
  overview: null,
};

export function persist(key, value) {
  localStorage.setItem(key, value);
}

export function setViewMode(mode) {
  state.viewMode = mode;
  persist("viewMode", mode);
}


export function setFilter(key, value) {
  // Three independent stat-inclusion toggles. No invariant — all can be off
  // if the user wants to see zero; the mode dropdown controls what's listed.
  state.filters[key] = !!value;
  localStorage.setItem(`filter_${key}`, value ? "1" : "0");
  if (key === "deleted") localStorage.removeItem("showDeleted");
}

export function setMode(m) {
  if (m !== "active" && m !== "archived") return;
  state.mode = m;
  localStorage.setItem("mode", m);
}

export function setTheme(theme) {
  state.theme = theme;
  persist("theme", theme);
}

export function invalidateProjectsCache() {
  state.projectsCache = null;
}
