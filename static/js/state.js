// Shared application state.

export const state = {
  view: "projects",
  folder: null,
  path: null,
  convoId: null,

  viewMode: localStorage.getItem("viewMode") || "list",
  theme: localStorage.getItem("theme") || "dark",

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
};

export function persist(key, value) {
  localStorage.setItem(key, value);
}

export function setViewMode(mode) {
  state.viewMode = mode;
  persist("viewMode", mode);
}

export function setTheme(theme) {
  state.theme = theme;
  persist("theme", theme);
}

export function invalidateProjectsCache() {
  state.projectsCache = null;
}
