// Shared application state.

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
  filters: (() => {
    const f = {
      active:   localStorage.getItem("filter_active") !== "0",
      archived: localStorage.getItem("filter_archived") === "1",
      deleted: (localStorage.getItem("filter_deleted") === "1")
            || (localStorage.getItem("showDeleted") === "1"),
    };
    // Invariant: at least one source must be on. The toggle-filter handler
    // guards against unchecking the last-on, but doesn't fire on load — so
    // stale localStorage from before the guard could leave everything off
    // and render empty graphs/stats with no recovery path. Force active on.
    if (!f.active && !f.archived && !f.deleted) {
      f.active = true;
      localStorage.setItem("filter_active", "1");
    }
    return f;
  })(),

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
